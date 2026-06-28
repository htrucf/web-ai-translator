"""Pipeline dịch .docx in-place qua Playwright Gemini.

Luồng:
    extract paragraphs → chunk (~1500 chars) → Gemini translate w/ [N] format
    → parse response → inject back vào doc → save .docx
    → LibreOffice render preview.pdf (best-effort)

Gói .docx gốc được copy nguyên byte (zipfile), chỉ thay node <w:t> đã dịch; bold/
italic inline được giữ qua thẻ [[#k]] cho paragraph đơn giản — xem docx_processor.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Literal

from app.utils.safe_io import atomic_write_json
from app.services.translator import WebAITranslator
from app import paths

from app.office._common import (
    split_into_chunks,
    chunk_to_numbered_text,
    parse_numbered_response,
    build_translation_prompt,
    clean_response,
)
from app.office import docx_processor
from app.office.preview import build_preview_pdf, is_available as preview_available


FileKind = Literal["docx"]


class OfficeTranslationPipeline:
    """Translate a .docx in-place via the shared Playwright Gemini backend.

    Single instance per job — kept simple. No glossary, no critic, no
    quality auto-fix (the PDF pipeline owns those; office files almost never
    need them). Session is rotated every CHUNKS_PER_SESSION to dodge Gemini's
    context window growth.
    """

    CHUNKS_PER_SESSION = 10
    DELAY_BETWEEN_CHUNKS = 2   # seconds

    def __init__(self, work_dir: str | None = None):
        self.work_dir = work_dir or paths.workspace_dir()
        self.translator = WebAITranslator()
        self.progress_file = ""
        self._cancelled = False
        self._page = None
        self._context = None

    # ── Progress / chunk persistence ────────────────────────────

    def _job_dir(self, job_id: str) -> str:
        d = os.path.join(self.work_dir, "jobs", job_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _load_progress(self, job_id: str) -> dict:
        self.progress_file = os.path.join(self._job_dir(job_id), "progress.json")
        if os.path.exists(self.progress_file):
            with open(self.progress_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "translated_chunks": {},
            "status": "pending",
            "source_type": "office",
        }

    def _save(self, progress: dict) -> None:
        atomic_write_json(self.progress_file, progress)

    def _save_chunk_files(self, job_id: str, idx: int,
                          original: str, translated: str) -> None:
        """Persist per-chunk original/translated text — used by judges + resume."""
        chunk_dir = os.path.join(self._job_dir(job_id), "chunks")
        os.makedirs(chunk_dir, exist_ok=True)
        with open(os.path.join(chunk_dir, f"chunk_{idx:03d}_original.txt"),
                  "w", encoding="utf-8") as f:
            f.write(original)
        with open(os.path.join(chunk_dir, f"chunk_{idx:03d}_translated.txt"),
                  "w", encoding="utf-8") as f:
            f.write(translated)

    def cancel(self) -> None:
        self._cancelled = True

    # ── Browser lifecycle ───────────────────────────────────────

    async def _ensure_page(self):
        if self._page is None:
            self._context, self._page = await self.translator.launch_browser()
        return self._page

    # ── Main entrypoint ─────────────────────────────────────────

    async def run(self, file_path: str, job_id: str, kind: FileKind) -> str:
        """Translate one office file. Returns the path to the translated file."""
        progress = self._load_progress(job_id)
        progress["status"] = "extracting"
        progress["kind"] = kind
        self._save(progress)

        # ── 1. Extract paragraphs ──────────────────────────────
        if kind == "docx":
            blocks, doc_obj = docx_processor.extract_blocks(file_path)
        else:
            raise ValueError(f"Unsupported office kind: {kind!r}")

        if not blocks:
            progress["status"] = "error: file không có text dịch được"
            self._save(progress)
            return ""

        chunks = split_into_chunks(blocks, max_chars=1500)
        progress["block_count"] = len(blocks)
        progress["total_chunks"] = len(chunks)
        self._save(progress)

        print(f"[OfficePipeline] {kind} '{job_id}' — {len(blocks)} blocks → "
              f"{len(chunks)} chunks")

        # ── 2. Translate — vòng khép kín dùng chung (panel judge + Critic + thang sửa) ─
        try:
            await self._translate_eval_loop(chunks, progress, job_id)
        except Exception as e:
            print(f"[OfficePipeline] eval-loop failed ({e}) — fallback legacy loop")
            await self._translate_legacy(chunks, progress, job_id)

        if self._cancelled:
            return ""

        # ── 3. Inject translations + save ──────────────────────
        progress["status"] = "rebuilding"
        self._save(progress)

        out_dir = os.path.join(self._job_dir(job_id), "output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"translated.{kind}")

        applied = docx_processor.inject_translations(doc_obj, blocks)
        docx_processor.save_docx(doc_obj, out_path)

        progress["applied_blocks"] = applied
        print(f"[OfficePipeline] injected {applied}/{len(blocks)} blocks → {out_path}")

        # ── 4. Preview PDF (best-effort) ───────────────────────
        if preview_available():
            progress["status"] = "rendering preview"
            self._save(progress)
            try:
                preview_path = os.path.join(out_dir, "preview.pdf")
                build_preview_pdf(out_path, preview_path)
                progress["has_preview"] = True
                progress.pop("preview_error", None)
                print(f"[OfficePipeline] preview rendered → {preview_path}")
            except Exception as e:
                msg = str(e)[:200]
                print(f"[OfficePipeline] preview failed: {msg}")
                progress["has_preview"] = False
                progress["preview_error"] = msg
        else:
            progress["has_preview"] = False
            progress["preview_error"] = "LibreOffice chưa được cài"

        progress["status"] = "done"
        self._save(progress)
        return out_path

    # ── Translate strategies ────────────────────────────────────

    async def _translate_eval_loop(self, chunks: list, progress: dict, job_id: str) -> None:
        """Dịch qua VÒNG KHÉP KÍN dùng chung: LocalJudge gate + Critic refine + thang sửa.

        Dùng OfficeEvalCodec (render/apply/chấm theo block office) + factory dịch
        generic. run_eval_loop tự quản browser; codec.apply ghi translated_text vào
        chính các block office → bước inject sau đọc được. judge_backend='off' (chỉ
        gate local + refine) cho nhẹ; đổi 'web' nếu muốn thêm MQM cross-model.
        """
        from types import SimpleNamespace
        from app.pdf.eval_adapters import run_eval_loop, make_generic_translate_factory
        from app.office.eval_codec import OfficeEvalCodec

        codec = OfficeEvalCodec()
        ctx = SimpleNamespace(
            chunks=chunks, glossary={}, glossary_enabled=False, locked_terms=[],
            progress=progress,
            save_progress=lambda: self._save(progress),
            is_cancelled=lambda: self._cancelled,
        )
        await run_eval_loop(
            ctx, models=["gemini"], judge_backend="off",
            codec=codec,
            translate_one_factory=make_generic_translate_factory(codec),
        )
        # Lưu file chunk (cho judge/resume); block.translated_text đã được codec.apply gán.
        finals = progress.get("translated_chunks", {})
        for ci, chunk in enumerate(chunks):
            txt = finals.get(str(ci))
            if txt:
                self._save_chunk_files(job_id, ci, codec.to_source_text(chunk), txt)

    async def _translate_legacy(self, chunks: list, progress: dict, job_id: str) -> None:
        """Vòng dịch tuần tự CŨ — fallback nếu eval-loop lỗi."""
        translated_chunks: dict = progress.get("translated_chunks", {}) or {}
        try:
            page = await self._ensure_page()
            await self.translator.start_new_chat(page)
            for ci, chunk in enumerate(chunks):
                if self._cancelled:
                    progress["status"] = "cancelled"
                    self._save(progress)
                    break
                key = str(ci)
                progress["status"] = f"translating {ci + 1}/{len(chunks)}"
                self._save(progress)
                if key in translated_chunks:
                    parse_numbered_response(translated_chunks[key], chunk)
                    continue
                original_text = chunk_to_numbered_text(chunk)
                prompt = build_translation_prompt(original_text)
                try:
                    raw = await self.translator._send_prompt_and_get_response(page, prompt)
                except Exception:
                    try:
                        await self.translator.start_new_chat(page)
                        raw = await self.translator._send_prompt_and_get_response(page, prompt)
                    except Exception:
                        raw = ""
                translated_text = clean_response(raw)
                parse_numbered_response(translated_text, chunk)
                translated_chunks[key] = translated_text
                self._save_chunk_files(job_id, ci, original_text, translated_text)
                progress["translated_chunks"] = translated_chunks
                self._save(progress)
                if (ci + 1) % self.CHUNKS_PER_SESSION == 0 and ci + 1 < len(chunks):
                    try:
                        await self.translator.start_new_chat(page)
                    except Exception:
                        pass
                if ci + 1 < len(chunks):
                    await asyncio.sleep(self.DELAY_BETWEEN_CHUNKS)
        finally:
            try:
                await self.translator.cleanup()
            except Exception:
                pass
            self._page = None
            self._context = None
