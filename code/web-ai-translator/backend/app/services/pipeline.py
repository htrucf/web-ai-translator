"""Pipeline dich thuat: tach chunk -> dich tung phan qua Gemini -> ghep lai .tex -> compile PDF."""

import asyncio
import json
import os
import re
import shutil
import time

from app.services.latex_processor import extract_source, split_into_chunks, compile_to_pdf
from app.services.translator import WebAITranslator
from app.utils.safe_io import atomic_write_json, is_within_directory
from app.utils.translation_meta import build_meta, format_latex_indicator_block
from app import paths
from app.audit import AuditLogger, set_current, clear_current, write_env_snapshot
from app.audit.logger import (
    PHASE_INIT,
    PHASE_EXTRACTION,
    PHASE_CHUNKING,
    PHASE_TRANSLATING,
    PHASE_REBUILDING,
    PHASE_VALIDATION,
    PHASE_FINISHED,
)


# Packages that re-bind \rmdefault / \ttdefault / math fonts to a Type-1
# PostScript family. They conflict with fontspec's \setmainfont and cause
# XeLaTeX to fail with "font cannot be found" even when the requested font
# IS installed. Stripped before we inject the fontspec block.
_INCOMPATIBLE_FONT_PACKAGES: tuple[str, ...] = (
    "helvet",
    # Times clones
    "newtxtext", "newtxmath", "newtxsf",
    "mathptmx", "mathptm", "times", "mathtime",
    # Palatino clones
    "newpxtext", "newpxmath", "mathpazo", "palatino", "pxfonts",
    # Charter / Libertine / TeX Gyre
    "charter", "libertine", "libertinus",
    "tgtermes", "tgpagella", "tgheros", "tgcursor",
    # Mono replacements that touch \ttdefault
    "courier", "beramono", "inconsolata",
)


def _strip_incompatible_font_packages(preamble: str) -> str:
    """Remove the Type-1 font packages listed in
    ``_INCOMPATIBLE_FONT_PACKAGES`` from *preamble*.

    Handles both forms:

      * ``\\usepackage[opt]{newtxtext}`` (whole line removed)
      * ``\\usepackage{newtxtext,newtxmath,bm}`` (only the matching tokens
        are removed; siblings like ``bm`` are kept).
    """
    targets = set(_INCOMPATIBLE_FONT_PACKAGES)

    def _filter_pkglist(match: "re.Match[str]") -> str:
        opts = match.group(1) or ""
        names = [n.strip() for n in match.group(2).split(",")]
        kept = [n for n in names if n and n not in targets]
        if not kept:
            return ""  # all targets — drop the whole \usepackage line
        if kept == names:
            return match.group(0)  # nothing to remove
        return f"\\usepackage{opts}{{{','.join(kept)}}}"

    pattern = re.compile(
        r"\\usepackage\s*(\[[^\]]*\])?\s*\{([^}]*)\}[^\n]*\n?",
        re.MULTILINE,
    )
    return pattern.sub(_filter_pkglist, preamble)


class TranslationPipeline:
    """Pipeline toan bo: tu file LaTeX goc -> PDF da dich tieng Viet."""

    # So chunk toi da trong 1 session Gemini truoc khi mo chat moi.
    # Gemini context window bi day dan theo conversation — sau ~10 chunk,
    # chat luong dich bat dau giam (response bi cut, mat LaTeX structure).
    CHUNKS_PER_SESSION = 10

    def __init__(self, work_dir: str | None = None):
        # When run as a subprocess from main.py the caller always passes an
        # absolute work_dir; the None fallback covers ad-hoc usage in dev.
        self.work_dir = work_dir or paths.workspace_dir()
        self.translator = WebAITranslator()
        self.progress_file = ""  # Duong dan file luu tien trinh
        self._audit: AuditLogger | None = None
        self._audit_token = None
        self._job_started_at: float = 0.0

    def _finalize_audit(self, status: str, **extra) -> None:
        """Flush + close audit logger. An toàn gọi nhiều lần."""
        if self._audit is None:
            return
        try:
            self._audit.set_phase(PHASE_FINISHED)
            self._audit.log(
                "job.finished",
                status=status,
                total_duration_seconds=round(time.time() - self._job_started_at, 3),
                **extra,
            )
        except Exception:
            pass
        try:
            self._audit.close()
        except Exception:
            pass
        try:
            clear_current(self._audit_token)
        except Exception:
            pass
        self._audit = None
        self._audit_token = None

    def _get_progress_dir(self, job_id: str) -> str:
        """Thu muc luu tien trinh dich cua mot job."""
        d = os.path.join(self.work_dir, "jobs", job_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _load_progress(self, job_id: str) -> dict:
        """Doc tien trinh da luu (de resume neu bi gian doan)."""
        self.progress_file = os.path.join(self._get_progress_dir(job_id), "progress.json")
        if os.path.exists(self.progress_file):
            with open(self.progress_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"translated_chunks": {}, "glossary": {}, "status": "pending"}

    def _save_progress(self, progress: dict):
        """Luu tien trinh xuong file. Atomic — readers never see torn JSON."""
        atomic_write_json(self.progress_file, progress)

    def _save_chunk(self, job_id: str, chunk_index: int, original: str, translated: str):
        """Luu tung chunk da dich ra file rieng."""
        chunk_dir = os.path.join(self._get_progress_dir(job_id), "chunks")
        os.makedirs(chunk_dir, exist_ok=True)

        with open(os.path.join(chunk_dir, f"chunk_{chunk_index:03d}_original.tex"), "w", encoding="utf-8") as f:
            f.write(original)
        with open(os.path.join(chunk_dir, f"chunk_{chunk_index:03d}_translated.tex"), "w", encoding="utf-8") as f:
            f.write(translated)

    def _extract_latex_from_response(self, response: str) -> str:
        """Trich xuat noi dung LaTeX tu response cua AI (bo phan giai thich, code fence...)."""
        # Tim block ```latex ... ``` hoac ``` ... ```
        blocks = re.findall(r'```(?:latex)?\s*\n(.*?)\n```', response, re.DOTALL)
        if blocks:
            text = "\n".join(b.strip() for b in blocks)
        else:
            # Khong co code fence -> loai bo chatbot artifacts
            text = response.strip()

        if text.lower().startswith("code snippet"):
            text = text.split("\n", 1)[-1].strip() if "\n" in text else text

        # Bo cac cau hoi/ghi chu cuoi response tu chatbot
        # Bao gom ca prompt markers khi Gemini echo lai prompt dich
        # Apply to ALL extracted text (including from code blocks)
        lines = text.split("\n")
        clean_lines = []
        for line in lines:
            stripped = line.strip()
            if re.match(r'^(Bạn có muốn|Lưu ý|Note:|Chú ý:|Would you|Let me know|Nếu bạn cần|Hy vọng)', stripped, re.IGNORECASE):
                break
            # Detect prompt leakage — AI echoing back the translation prompt
            if re.match(r'^(===\s*(QUY TẮC|NỘI DUNG CẦN DỊCH|VÍ DỤ|BẢNG THUẬT NGỮ)|Dịch nội dung LaTeX sau sang tiếng Việt)', stripped):
                break
            # Also detect mid-line prompt leakage (e.g. "...text. Dịch nội dung LaTeX sau sang tiếng Việt.")
            leak_match = re.search(r'Dịch nội dung LaTeX sau sang tiếng Việt', line)
            if leak_match:
                # Keep only the part before the leakage
                before = line[:leak_match.start()].rstrip()
                if before:
                    clean_lines.append(before)
                break
            clean_lines.append(line)
        while clean_lines and not clean_lines[-1].strip():
            clean_lines.pop()
        return "\n".join(clean_lines)

    @staticmethod
    def _is_response_truncated(original: str, translated: str) -> bool:
        """Kiem tra xem response co bi cat ngan khong.

        Gemini co the bi cat response khi context day. Dau hieu:
        - Output ngan hon 30% so voi input (tieng Viet thuong dai hon tieng Anh)
        - Output rong hoac chi co vai dong
        """
        if not translated or not translated.strip():
            return True

        orig_len = len(original.strip())
        trans_len = len(translated.strip())

        # Neu input du dai (>200 chars) ma output < 30% input => bi cat
        if orig_len > 200 and trans_len < orig_len * 0.3:
            print(f"  [Pipeline] Response co the bi cut: {trans_len} chars vs input {orig_len} chars "
                  f"({round(trans_len/orig_len*100)}%)")
            return True

        return False

    async def run(
        self,
        tex_path: str,
        job_id: str,
        source_dir: str | None = None,
    ) -> str:
        """
        Chay pipeline dich thuat day du.

        Args:
            tex_path: Duong dan file .tex goc
            job_id: ID cua job (de luu tien trinh)
            source_dir: Thu muc chua source goc (de copy .sty, .bst, hinh anh...)

        Returns:
            Duong dan file PDF da dich
        """
        progress = self._load_progress(job_id)
        job_dir = self._get_progress_dir(job_id)

        # ── Audit bootstrap ──────────────────────────────────────────
        self._job_started_at = time.time()
        self._audit = AuditLogger.open(job_id, job_dir)
        self._audit_token = set_current(self._audit)
        # Translator dùng cùng audit logger để ghi prompt/response file
        self.translator.audit = self._audit
        try:
            write_env_snapshot(job_id, job_dir, extra={
                "pipeline": "latex",
                "tex_path": tex_path,
                "source_dir": source_dir or "",
            })
        except Exception as e:
            print(f"[Pipeline] env_snapshot failed (non-fatal): {e}")
        self._audit.set_phase(PHASE_INIT)
        self._audit.log(
            "job.started",
            pipeline="latex",
            tex_path=tex_path,
            source_dir=source_dir or "",
            resume=bool(progress.get("translated_chunks")),
            existing_chunks=len(progress.get("translated_chunks") or {}),
            chunks_per_session=self.CHUNKS_PER_SESSION,
            backend=getattr(self.translator, "backend_name", "gemini"),
        )

        # 1. Doc file LaTeX goc
        print(f"[Pipeline] Doc file: {tex_path}")
        with open(tex_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # 2. Tach phan preamble (truoc \begin{document}) va body
        self._audit.set_phase(PHASE_EXTRACTION)
        preamble, body = self._split_preamble_body(content)
        self._audit.log(
            "latex.preamble_split",
            preamble_chars=len(preamble),
            body_chars=len(body),
            total_chars=len(content),
        )

        # 3. Tach body thanh cac chunk
        self._audit.set_phase(PHASE_CHUNKING)
        chunks = split_into_chunks(body)
        total = len(chunks)
        print(f"[Pipeline] Tach thanh {total} chunk")

        # 3b. Dem tong so chunk (bao gom ca input files) de bao tien trinh chinh xac
        input_files = []
        if source_dir:
            input_files = self._find_input_files(content, source_dir)
        total_global = total  # Bat dau voi so chunk cua body chinh
        input_chunk_counts = {}
        for input_rel, input_abs in input_files:
            with open(input_abs, "r", encoding="utf-8", errors="ignore") as f:
                inp_content = f.read()
            inp_chunks = split_into_chunks(inp_content)
            input_chunk_counts[input_rel] = len(inp_chunks)
            total_global += len(inp_chunks)
        print(f"[Pipeline] Tong cong: {total_global} chunk ({total} body + {total_global - total} input files)")
        self._audit.log(
            "latex.chunks_split",
            body_chunks=total,
            input_files=len(input_files),
            input_chunk_counts=input_chunk_counts,
            total_chunks=total_global,
        )

        # Cap nhat trang thai truoc khi mo browser de frontend biet pipeline dang chay
        progress["status"] = f"preparing: {total_global} chunks"
        self._save_progress(progress)

        # Load user-maintained glossary into translator (Phase A)
        gloss_block = progress.get("glossary") or {}
        self.translator.glossary = dict(gloss_block.get("terms") or {})
        self.translator.locked_terms = list(gloss_block.get("locked") or [])
        if self.translator.glossary:
            print(f"[Pipeline] Loaded {len(self.translator.glossary)} glossary terms "
                  f"({len(self.translator.locked_terms)} locked)")
        self._audit.log(
            "glossary.loaded",
            term_count=len(self.translator.glossary),
            locked_count=len(self.translator.locked_terms),
        )

        # 4. Mo browser
        self._audit.set_phase(PHASE_TRANSLATING)
        print("[Pipeline] Mo browser Gemini...")
        context, page = await self.translator.launch_browser()
        global_done = 0  # Dem so chunk da dich xong (dung cho progress)

        try:
            # 5. Dich tung chunk
            translated_chunks = progress.get("translated_chunks", {})
            chunks_since_new_chat = 0  # Dem so chunk da dich tu lan new chat cuoi

            for i, chunk in enumerate(chunks):
                chunk_key = str(i)

                # Bo qua chunk da dich (resume) - nhung chi khi co noi dung
                if chunk_key in translated_chunks and translated_chunks[chunk_key].strip():
                    global_done += 1
                    print(f"[Pipeline] Chunk {i+1}/{total} - da dich truoc do, bo qua")
                    self._audit.log("chunk.skipped_resume", chunk_idx=i, scope="body")
                    continue

                # Strip comment-only lines truoc khi gui dich (tiet kiem token)
                chunk_to_send = self._strip_comments(chunk)

                # Neu sau khi strip chi con toan whitespace, giu nguyen chunk goc
                if not chunk_to_send.strip():
                    translated_chunks[chunk_key] = chunk
                    global_done += 1
                    progress["translated_chunks"] = translated_chunks
                    progress["status"] = f"translating {global_done}/{total_global}"
                    self._save_progress(progress)
                    print(f"[Pipeline] Chunk {i+1}/{total} - chi co comment, bo qua dich")
                    self._audit.log("chunk.skipped_comment_only", chunk_idx=i, scope="body")
                    continue

                # Thu dich inline (chi co heading/section commands) -- khong can Gemini
                inline_result = self._translate_inline(chunk_to_send)
                if inline_result is not None:
                    translated_chunks[chunk_key] = inline_result
                    global_done += 1
                    progress["translated_chunks"] = translated_chunks
                    progress["status"] = f"translating {global_done}/{total_global}"
                    self._save_progress(progress)
                    print(f"[Pipeline] Chunk {i+1}/{total} - dich inline (structural), bo qua Gemini")
                    self._audit.log(
                        "chunk.translated_inline",
                        chunk_idx=i,
                        scope="body",
                        original_chars=len(chunk),
                        translated_chars=len(inline_result),
                    )
                    continue

                # Session rotation: mo chat moi sau moi N chunk de giu chat luong
                if chunks_since_new_chat >= self.CHUNKS_PER_SESSION:
                    print(f"[Pipeline] Dat gioi han {self.CHUNKS_PER_SESSION} chunk/session, mo chat moi...")
                    self._audit.log(
                        "session.rotated",
                        reason="chunk_limit",
                        chunks_in_session=chunks_since_new_chat,
                        limit=self.CHUNKS_PER_SESSION,
                        at_chunk=i,
                        scope="body",
                    )
                    try:
                        await self.translator.start_new_chat(page)
                    except Exception as e:
                        self._audit.log("session.rotation_failed",
                                        error=str(e)[:200], at_chunk=i, scope="body")
                    chunks_since_new_chat = 0
                    await asyncio.sleep(2)

                print(f"[Pipeline] Dich chunk {i+1}/{total} ({len(chunk_to_send)} ky tu, goc {len(chunk)})...")
                chunk_started_at = time.time()
                self._audit.log(
                    "chunk.translate_started",
                    chunk_idx=i,
                    scope="body",
                    original_chars=len(chunk),
                    send_chars=len(chunk_to_send),
                )

                # Gui dich (retry toi da 3 lan, relaunch browser neu can)
                translated = ""
                attempts_used = 0
                for attempt in range(3):
                    attempts_used = attempt + 1
                    try:
                        translated = await self.translator.translate_chunk(page, chunk_to_send)
                        if translated.strip():
                            # Kiem tra chat luong: neu output qua ngan so voi input => co the bi cat
                            if self._is_response_truncated(chunk_to_send, translated):
                                print(f"[Pipeline] Response bi cut ngan, mo chat moi va thu lai...")
                                self._audit.log(
                                    "chunk.truncated_detected",
                                    chunk_idx=i,
                                    scope="body",
                                    attempt=attempt + 1,
                                    original_chars=len(chunk_to_send),
                                    translated_chars=len(translated),
                                    ratio=round(len(translated) / max(1, len(chunk_to_send)), 3),
                                )
                                await self.translator.start_new_chat(page)
                                chunks_since_new_chat = 0
                                await asyncio.sleep(2)
                                continue  # Retry trong chat moi
                            break
                    except TimeoutError as e:
                        print(f"[Pipeline] Gemini timeout chunk {i+1} (lan {attempt+1}): {e}")
                        self._audit.log(
                            "chunk.attempt_failed",
                            chunk_idx=i,
                            scope="body",
                            attempt=attempt + 1,
                            error_type="timeout",
                            error=str(e)[:200],
                        )
                        # Mo chat hoan toan moi thay vi chi reload
                        try:
                            await self.translator.start_new_chat(page)
                        except Exception:
                            context, page = await self._recover_browser(context, page)
                        chunks_since_new_chat = 0
                        await asyncio.sleep(5)

                    except Exception as e:
                        print(f"[Pipeline] Loi chunk {i+1} (lan {attempt+1}): {e}")
                        self._audit.log(
                            "chunk.attempt_failed",
                            chunk_idx=i,
                            scope="body",
                            attempt=attempt + 1,
                            error_type=type(e).__name__,
                            error=str(e)[:200],
                        )

                    # Relaunch browser neu page/context da chet
                    context, page = await self._recover_browser(context, page)
                    chunks_since_new_chat = 0
                    await asyncio.sleep(2)

                translated_clean = self._extract_latex_from_response(translated)

                # Neu extract ra rong, giu nguyen raw response
                used_raw = False
                if not translated_clean.strip():
                    print(f"[Pipeline] CANH BAO: extract rong, dung raw response")
                    translated_clean = translated
                    used_raw = True

                # Luu chunk
                translated_chunks[chunk_key] = translated_clean
                self._save_chunk(job_id, i, chunk, translated_clean)
                chunks_since_new_chat += 1

                # Luu tien trinh
                global_done += 1
                progress["translated_chunks"] = translated_chunks
                progress["status"] = f"translating {global_done}/{total_global}"
                self._save_progress(progress)

                print(f"[Pipeline] Chunk {global_done}/{total_global} - xong ({len(translated_clean)} ky tu)")
                self._audit.log(
                    "chunk.translate_done",
                    chunk_idx=i,
                    scope="body",
                    attempts=attempts_used,
                    raw_chars=len(translated),
                    translated_chars=len(translated_clean),
                    used_raw_fallback=used_raw,
                    latency_seconds=round(time.time() - chunk_started_at, 3),
                    global_progress=f"{global_done}/{total_global}",
                )

                # Nghi ngan giua cac chunk
                await asyncio.sleep(1)

            # 5b. Dich cac file \input{...} trong body
            if source_dir:
                for input_rel, input_abs in input_files:
                    print(f"[Pipeline] Dich file input: {input_rel}")
                    self._audit.log(
                        "latex.input_file_started",
                        input_rel=input_rel,
                        expected_chunks=input_chunk_counts.get(input_rel, 0),
                    )
                    context, page, chunks_since_new_chat, global_done = await self._translate_input_file(
                        input_abs, input_rel, job_id, page, context, progress,
                        chunks_since_new_chat=chunks_since_new_chat,
                        global_done=global_done,
                        total_global=total_global,
                    )
                    self._audit.log(
                        "latex.input_file_done",
                        input_rel=input_rel,
                        global_progress=f"{global_done}/{total_global}",
                    )

        except Exception as e:
            # Audit unexpected failure trước khi cleanup chạy ở finally, rồi finalize.
            if self._audit is not None:
                try:
                    self._audit.log(
                        "error.unexpected",
                        phase=self._audit.phase,
                        exc_type=type(e).__name__,
                        message=str(e)[:500],
                    )
                except Exception:
                    pass
            self._finalize_audit(status="error", error=type(e).__name__,
                                 error_message=str(e)[:200])
            raise
        finally:
            try:
                await context.close()
            except Exception as e:
                print(f"[Pipeline] Warning: context.close() failed: {e}")
            await self.translator.cleanup()

        # 6. Ghep lai thanh file .tex moi
        self._audit.set_phase(PHASE_REBUILDING)
        print("[Pipeline] Ghep cac chunk da dich...")
        rebuild_started_at = time.time()
        translated_body = ""
        for i in range(total):
            translated_body += translated_chunks.get(str(i), chunks[i])

        # Them package tieng Viet vao preamble
        translated_preamble = self._add_vietnamese_support(preamble)

        # Stamp provenance metadata: PDF hypersetup (Tier 1) + fancyhdr footer (Tier 2).
        # job_id của LaTeX flow chính là arxiv_id (có thể có '_' thay cho '/').
        try:
            acct_info = self.translator.get_account_info()
        except Exception as e:
            print(f"[Pipeline] get_account_info() failed (non-fatal): {e}")
            acct_info = {"backend": "gemini", "account_email": ""}
        arxiv_id_normalized = job_id.replace("_", "/")
        translation_meta = build_meta(
            job_id=job_id,
            source_kind="arxiv",
            source_label=arxiv_id_normalized,
            source_url=f"https://arxiv.org/abs/{arxiv_id_normalized}",
            translator_backend=acct_info.get("backend", "gemini"),
            account_email=acct_info.get("account_email", ""),
        )
        progress["translation_meta"] = translation_meta
        self._save_progress(progress)
        translated_preamble = self._inject_translation_indicator(
            translated_preamble, translation_meta
        )
        self._audit.log(
            "latex.indicator_stamped",
            backend=translation_meta["translator_backend"],
            account=translation_meta["account_email"] or "default",
            source_url=translation_meta["source_url"],
        )

        # Dam bao text duoc justified (ragged2e + xelatex co the lam mat justify)
        translated_body = self._ensure_justified(translated_body)

        # Sua cac loi cau truc LaTeX do AI merge dong
        translated_body = self._fix_latex_structure(translated_body)

        translated_content = translated_preamble + "\n" + translated_body

        # Luu file .tex da dich
        output_tex_dir = os.path.join(job_dir, "output")
        os.makedirs(output_tex_dir, exist_ok=True)

        # Copy cac file phu tro (.sty, .bst, hinh anh...) tu source goc
        if source_dir:
            self._copy_support_files(source_dir, output_tex_dir, main_tex=tex_path)

        # Luu cac file input da dich vao output dir
        if source_dir:
            self._save_translated_input_files(job_id, input_files, output_tex_dir)

        translated_tex = os.path.join(output_tex_dir, "translated.tex")
        with open(translated_tex, "w", encoding="utf-8") as f:
            f.write(translated_content)
        print(f"[Pipeline] Da luu: {translated_tex}")
        self._audit.log(
            "latex.merged",
            translated_tex=translated_tex,
            preamble_chars=len(translated_preamble),
            body_chars=len(translated_body),
            total_chars=len(translated_content),
            input_files=len(input_files),
            merge_seconds=round(time.time() - rebuild_started_at, 3),
        )

        # 7. Compile PDF
        print("[Pipeline] Compile PDF...")
        progress["status"] = "compiling"
        self._save_progress(progress)
        self._audit.log("latex.compile_started", translated_tex=translated_tex)
        compile_started_at = time.time()
        try:
            pdf_path = compile_to_pdf(translated_tex, output_tex_dir)
        except RuntimeError as e:
            progress["status"] = f"compile_error: {e}"
            self._save_progress(progress)
            print(f"[Pipeline] Loi compile: {e}")
            self._audit.log(
                "latex.compile_failed",
                error=str(e)[:500],
                compile_seconds=round(time.time() - compile_started_at, 3),
            )
            self._finalize_audit(status="error", error="compile_failed",
                                 error_message=str(e)[:200])
            raise
        self._audit.log(
            "latex.compile_done",
            pdf_path=pdf_path,
            compile_seconds=round(time.time() - compile_started_at, 3),
            pdf_size_bytes=(os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0),
        )

        # 8. Validate PDF output
        self._audit.set_phase(PHASE_VALIDATION)
        original_pdf = os.path.join(job_dir, "original.pdf")
        validation = self._validate_pdf(pdf_path, original_pdf)
        progress["pdf_path"] = pdf_path
        progress["validation"] = validation
        self._audit.log(
            "pdf.validation",
            status=validation.get("status"),
            translated_pages=validation.get("translated_pages"),
            original_pages=validation.get("original_pages"),
            page_ratio=validation.get("page_ratio"),
            warnings=validation.get("warnings") or [],
        )

        if validation["status"] == "ok":
            progress["status"] = "done"
            print(f"[Pipeline] Thanh cong! PDF: {pdf_path}")
        else:
            progress["status"] = f"done_with_warnings"
            print(f"[Pipeline] PDF tao xong nhung co van de: {validation['warnings']}")

        self._save_progress(progress)
        self._finalize_audit(
            status=progress["status"],
            pdf_path=pdf_path,
            validation_status=validation.get("status"),
            warnings_count=len(validation.get("warnings") or []),
        )
        return pdf_path

    @staticmethod
    def _get_pdf_page_count(pdf_path: str) -> int:
        """Dem so trang cua file PDF."""
        # Try fitz (PyMuPDF) first — always installed
        try:
            try:
                import fitz
            except ImportError:
                import pymupdf as fitz
            doc = fitz.open(pdf_path)
            count = len(doc)
            doc.close()
            return count
        except Exception:
            pass
        # Fallback: PyPDF2
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(pdf_path)
            return len(reader.pages)
        except Exception:
            return -1

    @staticmethod
    def _validate_pdf(translated_pdf: str, original_pdf: str) -> dict:
        """Kiem tra chat luong PDF da dich so voi ban goc.

        Returns:
            dict voi keys:
            - status: "ok" | "warning"
            - translated_pages: int
            - original_pages: int
            - page_ratio: float (translated/original)
            - warnings: list[str]
        """
        translated_pages = TranslationPipeline._get_pdf_page_count(translated_pdf)
        original_pages = TranslationPipeline._get_pdf_page_count(original_pdf)

        result = {
            "status": "ok",
            "translated_pages": translated_pages,
            "original_pages": original_pages,
            "page_ratio": 0,
            "warnings": [],
        }

        if translated_pages <= 0 or original_pages <= 0:
            result["status"] = "warning"
            result["warnings"].append("Khong the dem so trang PDF")
            return result

        ratio = translated_pages / original_pages
        result["page_ratio"] = round(ratio, 2)

        # Canh bao neu so trang dich < 50% ban goc
        if ratio < 0.5:
            result["status"] = "warning"
            result["warnings"].append(
                f"Ban dich chi co {translated_pages} trang, ban goc co {original_pages} trang "
                f"({round(ratio * 100)}%). Noi dung co the bi mat."
            )

        # Canh bao neu chi co 1-2 trang (compile loi)
        if translated_pages <= 2 and original_pages > 5:
            result["status"] = "warning"
            result["warnings"].append(
                f"Ban dich chi co {translated_pages} trang — co the compile bi loi nghiem trong."
            )

        # Kiem tra kich thuoc file
        translated_size = os.path.getsize(translated_pdf)
        original_size = os.path.getsize(original_pdf)
        if original_size > 0:
            size_ratio = translated_size / original_size
            if size_ratio < 0.2:
                result["status"] = "warning"
                result["warnings"].append(
                    f"File PDF dich ({translated_size // 1024}KB) nho hon nhieu so voi ban goc "
                    f"({original_size // 1024}KB). Co the thieu noi dung."
                )

        print(f"[Validate] Pages: {translated_pages}/{original_pages} (ratio={result['page_ratio']}), "
              f"Size: {translated_size // 1024}KB/{original_size // 1024}KB, "
              f"Status: {result['status']}")
        if result["warnings"]:
            for w in result["warnings"]:
                print(f"[Validate] WARNING: {w}")

        return result

    async def _recover_browser(self, context, page):
        """Thu dong browser hien tai va mo lai browser moi.

        Goi khi page/context da chet (user dong browser, crash, v.v.).
        Tra ve (context_moi, page_moi).
        """
        print("[Pipeline] Browser da mat ket noi, dang khoi dong lai...")
        if self._audit is not None:
            self._audit.log("browser.recover_started")
        recover_started_at = time.time()
        # Cleanup browser cu (ignore errors vi no da chet)
        try:
            await context.close()
        except Exception:
            pass
        await self.translator.cleanup()

        # Mo browser moi
        context, page = await self.translator.launch_browser()
        print("[Pipeline] Browser da khoi dong lai thanh cong!")
        if self._audit is not None:
            self._audit.log(
                "browser.recover_done",
                duration_seconds=round(time.time() - recover_started_at, 3),
            )
        return context, page

    # Maps lowercase English → Vietnamese for the most common academic headings.
    _HEADING_MAP = {
        "abstract": "Tóm tắt", "introduction": "Giới thiệu",
        "related work": "Công trình liên quan", "related works": "Công trình liên quan",
        "background": "Nền tảng", "preliminaries": "Kiến thức nền",
        "method": "Phương pháp", "methods": "Phương pháp",
        "methodology": "Phương pháp nghiên cứu",
        "approach": "Phương pháp tiếp cận",
        "model": "Mô hình", "models": "Mô hình",
        "architecture": "Kiến trúc",
        "experiment": "Thực nghiệm", "experiments": "Thực nghiệm",
        "experimental setup": "Thiết lập thực nghiệm",
        "experimental results": "Kết quả thực nghiệm",
        "results": "Kết quả", "result": "Kết quả",
        "evaluation": "Đánh giá",
        "discussion": "Thảo luận",
        "analysis": "Phân tích",
        "ablation": "Nghiên cứu ablation",
        "ablation study": "Nghiên cứu ablation",
        "conclusion": "Kết luận", "conclusions": "Kết luận",
        "future work": "Hướng nghiên cứu tương lai",
        "limitations": "Hạn chế",
        "acknowledgment": "Lời cảm ơn", "acknowledgments": "Lời cảm ơn",
        "acknowledgement": "Lời cảm ơn", "acknowledgements": "Lời cảm ơn",
        "references": "Tài liệu tham khảo",
        "appendix": "Phụ lục",
        "supplementary": "Tài liệu bổ sung",
        "overview": "Tổng quan",
        "framework": "Khung làm việc",
        "training": "Huấn luyện",
        "inference": "Suy luận",
        "dataset": "Bộ dữ liệu", "datasets": "Bộ dữ liệu",
        "baseline": "Phương pháp cơ sở", "baselines": "Phương pháp cơ sở",
        "comparison": "So sánh",
        "notation": "Ký hiệu",
        "problem formulation": "Phát biểu bài toán",
        "problem statement": "Phát biểu bài toán",
        "task definition": "Định nghĩa bài toán",
        "implementation details": "Chi tiết cài đặt",
        "implementation": "Cài đặt",
        "setup": "Thiết lập",
        "main results": "Kết quả chính",
        "summary": "Tổng kết",
    }

    @staticmethod
    def _translate_inline(chunk: str) -> str | None:
        """Translate trivial structural chunks locally without Gemini.

        Returns translated string if handled, None if must go to Gemini.

        Cases handled inline (no Gemini needed):
        1. Chunks with zero translatable plain text (only LaTeX commands/envs)
        2. Chunks whose every line is a known section/heading command
        """
        import re as _re

        lines = [l for l in chunk.strip().splitlines() if l.strip() and not l.strip().startswith('%')]
        if not lines:
            return chunk

        # ── Case 1: No translatable plain text at all ─────────────────
        # Strip ALL LaTeX commands, environments, math, comments, braces
        plain = _re.sub(r'%.*', '', chunk)                          # strip comments
        plain = _re.sub(r'\\begin\{[^}]*\}|\\end\{[^}]*\}', ' ', plain)  # \begin/\end
        plain = _re.sub(r'\\[a-zA-Z]+\*?(?:\[[^\]]*\])*(?:\{[^}]*\})*', ' ', plain)  # commands
        plain = _re.sub(r'[${}()\[\]\\]', ' ', plain)              # special chars
        plain = _re.sub(r'\s+', ' ', plain).strip()
        # If essentially no plain text remains, return chunk unchanged
        if len(plain) < 10:
            return chunk

        # ── Case 2: Every line is a translatable heading command ──────
        heading_pattern = _re.compile(
            r'^\\(section|subsection|subsubsection|paragraph|subparagraph|chapter|part'
            r'|caption|captionof|footnote|title|author|date|thanks'
            r'|textbf|textit|emph)\*?\{([^}]*)\}[\s%]*$'
        )
        result_lines = []
        for line in lines:
            m = heading_pattern.match(line.strip())
            if not m:
                return None  # Has non-structural content → needs Gemini
            text = m.group(2).strip()
            vi = TranslationPipeline._HEADING_MAP.get(text.lower())
            if vi:
                result_lines.append(line.replace(text, vi))
            else:
                result_lines.append(line)

        return '\n'.join(result_lines)

    @staticmethod
    def _get_translatable_text_length(chunk: str) -> int:
        """Estimate length of actual translatable plain text in a chunk (no LaTeX commands)."""
        import re as _re
        text = _re.sub(r'\\[a-zA-Z]+\*?(\{[^}]*\})*', ' ', chunk)
        text = _re.sub(r'[$%\\{}\[\]]', ' ', text)
        return len(text.strip())

    @staticmethod
    def _strip_comments(chunk: str) -> str:
        """Loai bo cac dong comment-only (bat dau bang %) khoi chunk truoc khi gui dich.

        Giu lai:
        - Comment cuoi dong (vi du: \\section{Title} % note) -> giu nguyen dong
        - Dong trong
        - Tat ca noi dung khong phai comment
        Bo:
        - Dong chi chua comment (bat dau bang % sau khoang trang)
        """
        lines = chunk.split('\n')
        result = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('%'):
                # Dong comment-only -> bo
                continue
            result.append(line)
        return '\n'.join(result)

    def _split_preamble_body(self, content: str) -> tuple[str, str]:
        """Tach preamble (truoc \\begin{document}) va body."""
        match = re.search(r'(\\begin\{document\})', content)
        if match:
            preamble = content[:match.start()]
            body = content[match.start():]
            return preamble, body
        return "", content

    def _add_vietnamese_support(self, preamble: str) -> str:
        """Them ho tro tieng Viet vao preamble cho XeLaTeX.

        XeLaTeX ho tro UTF-8 native nen chi can:
        - Xoa inputenc (khong tuong thich voi xelatex)
        - Xoa fontenc (khong can thiet voi xelatex)
        - Xoa cac lenh pdfTeX-only (pdfminorversion, pdfobjcompresslevel)
        - Them fontspec voi font ho tro tieng Viet (Times New Roman)
        - Override \\secfnt, \\subsecfnt de heading hien thi tieng Viet dung
        """
        # Xoa inputenc (khong tuong thich voi xelatex)
        preamble = re.sub(r'\\usepackage\[[^\]]*\]\{inputenc\}\s*\n?', '', preamble)
        preamble = re.sub(r'\\usepackage\{inputenc\}\s*\n?', '', preamble)

        # Xoa fontenc (xelatex dung fontspec thay the)
        preamble = re.sub(r'\\usepackage\[[^\]]*\]\{fontenc\}\s*\n?', '', preamble)
        preamble = re.sub(r'\\usepackage\{fontenc\}\s*\n?', '', preamble)

        # Xoa cac package thay font Type1/PostScript khong tuong thich voi
        # fontspec/xelatex. newtxtext/newtxmath/mathptmx/mathpazo/times/...
        # load font Times-clone qua co che .fd file rieng, dan den xung dot
        # khi \setmainfont{Times New Roman} cua fontspec chay sau do
        # (xetex bao "font Times New Roman cannot be found" du font Windows
        # van ton tai). Phai xoa truoc khi inject fontspec block.
        preamble = _strip_incompatible_font_packages(preamble)

        # Xoa cac lenh chi co trong pdfTeX (gay loi/warning voi xelatex)
        preamble = re.sub(r'\\pdfminorversion\s*=\s*\d+\s*\n?', '', preamble)
        preamble = re.sub(r'\\pdfobjcompresslevel\s*=\s*\d+\s*\n?', '', preamble)

        # Neu da co fontspec thi chi can override heading fonts va them hyperref
        if "\\usepackage{fontspec}" in preamble:
            if "\\renewcommand{\\secfnt}" not in preamble:
                preamble = self._add_heading_font_overrides(preamble)
            preamble = self._ensure_hyperref(preamble)
            return preamble

        # Them fontspec voi font ho tro tieng Viet day du
        # Times New Roman cho serif, Arial cho sans-serif — ca 2 deu ho tro Vietnamese
        #
        # [no-math]: fontspec mac dinh "Adjusting the maths setup" tuc la
        # redeclare cac math accent + math symbol. Doi voi journal class co
        # math redefinitions rieng (vd jfm.cls: \def\Gamma{\varGamma}), buoc
        # nay gay loi "Command `\Gamma' already defined". Tat math adjust de
        # math giu nguyen tu class file, fontspec chi quan li text font.
        viet_packages = (
            "\n% XeLaTeX Vietnamese support\n"
            "\\usepackage[no-math]{fontspec}\n"
            "\\setmainfont{Times New Roman}\n"
            "\\setsansfont{Arial}\n"
            "\\setmonofont{Consolas}\n"
        )

        # Tim vi tri usepackage cuoi cung trong preamble
        last_usepackage = None
        for m in re.finditer(r'\\usepackage[^\n]*\n', preamble):
            last_usepackage = m

        if last_usepackage:
            insert_pos = last_usepackage.end()
            preamble = preamble[:insert_pos] + viet_packages + preamble[insert_pos:]
        else:
            preamble = preamble.rstrip() + "\n" + viet_packages

        # Override heading fonts cua class file (nhieu class dung \newfont voi T1 fonts
        # khong ho tro Vietnamese — can override bang fontspec fonts)
        preamble = self._add_heading_font_overrides(preamble)

        # Them hyperref de citation va ref clickable
        preamble = self._ensure_hyperref(preamble)

        return preamble

    @staticmethod
    def _ensure_hyperref(preamble: str) -> str:
        """Them hyperref neu chua co de citation va ref clickable trong PDF."""
        if "hyperref" in preamble:
            return preamble

        # implicit=false: khong redefine \cite, \ref, etc. — tranh xung dot voi
        # cac class co citation sorting rieng (vd: ipsj, elsarticle, v.v.)
        hyperref_line = "\\usepackage[colorlinks=true, allcolors=blue, breaklinks=true, implicit=false]{hyperref}\n"

        # Chen truoc \begin{document} (hyperref nen load sau hau het packages khac)
        doc_match = re.search(r'\\begin\{document\}', preamble)
        if doc_match:
            preamble = preamble[:doc_match.start()] + hyperref_line + "\n" + preamble[doc_match.start():]
        else:
            preamble = preamble.rstrip() + "\n" + hyperref_line

        return preamble

    @staticmethod
    def _inject_translation_indicator(preamble: str, meta: dict) -> str:
        """Chen hypersetup + fancyhdr footer block vao cuoi preamble.

        Block phai nam SAU hyperref (de hypersetup chay duoc) va TRUOC
        \\begin{document}. Vi `_split_preamble_body` cat `\\begin{document}` ra body,
        nen append vao cuoi preamble la dung vi tri.
        """
        indicator = format_latex_indicator_block(meta)
        doc_match = re.search(r'\\begin\{document\}', preamble)
        if doc_match:
            return preamble[:doc_match.start()] + indicator + preamble[doc_match.start():]
        return preamble.rstrip() + "\n" + indicator

    def _add_heading_font_overrides(self, preamble: str) -> str:
        """Override cac font heading hardcoded trong class file.

        Nhieu class file (sig-alternate, acmart, v.v.) dung \\newfont de dinh nghia
        \\secfnt, \\subsecfnt bang Type 1 fonts (ptmb8t, ptmri8t) khong ho tro Vietnamese.
        Can override bang font tuong duong qua fontspec.

        Chi override khi class thuc su dinh nghia cac command nay
        (dung \\@ifundefined de kiem tra an toan, tranh loi voi cac class khac).
        """
        overrides = (
            "\n% Override heading fonts for Vietnamese support (only if defined by class)\n"
            "\\makeatletter\n"
            "\\@ifundefined{secfnt}{}{\\renewcommand{\\secfnt}{\\fontsize{12pt}{14pt}\\fontspec{Times New Roman}[Bold]\\bfseries}}\n"
            "\\@ifundefined{subsecfnt}{}{\\renewcommand{\\subsecfnt}{\\fontsize{11pt}{13pt}\\fontspec{Times New Roman}[Italic]\\itshape}}\n"
            "\\makeatother\n"
        )

        # Chen truoc \begin{document}
        doc_match = re.search(r'\\begin\{document\}', preamble)
        if doc_match:
            preamble = preamble[:doc_match.start()] + overrides + preamble[doc_match.start():]
        else:
            preamble = preamble.rstrip() + "\n" + overrides

        return preamble

    def _ensure_justified(self, body: str) -> str:
        """Dam bao text duoc justified — nhung chi khi ragged2e da duoc load.

        \\justifying chi hoat dong khi ragged2e package co mat.
        Hau het cac class file da justify mac dinh, nen khong can chen them.
        """
        # Khong chen \justifying nua — no gay loi voi cac class khong co ragged2e.
        # XeLaTeX + hau het document classes da justify mac dinh.
        return body

    @staticmethod
    def _fix_latex_structure(text: str) -> str:
        """Sua cac loi cau truc LaTeX thuong gap do AI dich bi merge dong.

        Cac van de thuong gap:
        1. \\begin{env} bi dính vao cuoi dong truoc (bi comment hoa boi %)
        2. \\end{env} bi dinh vao dau dong tiep theo (mat newline)
        3. \\section{...} bi merge voi \\begin{env} tren cung mot dong
        4. AI bo comment % o \\begin{env} nhung giu % o \\end{env} -> env khong dong
        5. \\begin{equation*} bi dat truoc paragraph text (khong co noi dung phuong trinh)
        """
        # 1. Tach \begin{env} bi dinh vao cuoi dong text/comment
        text = re.sub(
            r'(%[^\n]*)\\begin\{([^}]+)\}',
            r'\1\n\n\\begin{\2}',
            text
        )

        # 2. Tach \begin{env} bi dinh vao cuoi text (khong co %)
        text = re.sub(
            r'([.!?;:)\]}])\\begin\{([^}]+)\}',
            r'\1\n\n\\begin{\2}',
            text
        )

        # 3. Tach \end{env} bi dinh vao dau text tiep theo
        text = re.sub(
            r'(\\end\{[^}]+\})([A-ZĐa-zđÀ-ỹ\\])',
            r'\1\n\n\2',
            text
        )

        # 4. Separate merged section commands
        # e.g., "\subsection{X}\paragraph{Y}" -> "\subsection{X}\n\n\paragraph{Y}"
        # e.g., "text.\subsection{X}" -> "text.\n\n\subsection{X}"
        section_cmds = r'(\\(?:section|subsection|subsubsection|paragraph|subparagraph))'
        # 5a. Section command stuck after closing brace or text
        text = re.sub(
            r'(\})\s*' + section_cmds + r'\{',
            r'\1\n\n\2{',
            text
        )
        # 5b. Section command stuck after punctuation or text
        text = re.sub(
            r'([.!?;])\s*' + section_cmds + r'\{',
            r'\1\n\n\2{',
            text
        )

        # 6. Fix orphaned \begin{env} whose \end{env} is commented out or missing.
        text = TranslationPipeline._fix_orphaned_environments(text)

        # 7. Fix \begin{equation*} placed before paragraph text with no equation content.
        text = re.sub(
            r'\\begin\{equation\*?\}\s*(\\paragraph|\\subsection|\\section)',
            r'\1',
            text
        )
        text = re.sub(
            r'\\begin\{equation\*?\}\s*\n\s*(\\paragraph|\\subsection|\\section)',
            r'\1',
            text
        )

        return text

    @staticmethod
    def _fix_orphaned_environments(text: str) -> str:
        """Detect and fix unbalanced \\begin/\\end environments.

        Common AI translation errors:
        1. Removes % from \\begin{env} but keeps % on \\end{env}
        2. Keeps \\begin{env} but drops \\end{env} entirely
        Both leave unclosed environments that break compilation.
        """
        lines = text.split('\n')
        env_stack = []  # (env_name, line_index, is_commented)

        # Environments that are block-level and commonly broken
        block_envs = {'equation', 'equation*', 'align', 'align*', 'table',
                      'table*', 'figure', 'figure*', 'tabular', 'tabular*',
                      'center', 'minipage', 'itemize', 'enumerate'}

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            is_commented = stripped.startswith('%')

            # Find \begin{...} on this line
            for m in re.finditer(r'\\begin\{([^}]+)\}', line):
                env_name = m.group(1)
                if env_name in block_envs:
                    env_stack.append((env_name, i, is_commented))

            # Find \end{...} on this line
            for m in re.finditer(r'\\end\{([^}]+)\}', line):
                env_name = m.group(1)
                if env_name in block_envs and env_stack:
                    # Find matching begin
                    for j in range(len(env_stack) - 1, -1, -1):
                        if env_stack[j][0] == env_name:
                            begin_env, begin_line, begin_commented = env_stack[j]
                            # Mismatch: begin uncommented, end commented
                            if not begin_commented and is_commented:
                                lines[begin_line] = '%' + lines[begin_line]
                            # Mismatch: begin commented, end uncommented
                            elif begin_commented and not is_commented:
                                lines[i] = '%' + lines[i]
                            env_stack.pop(j)
                            break

        # Any remaining items in env_stack are orphaned \begin{} with no \end{}
        for env_name, line_idx, is_commented in env_stack:
            if not is_commented:
                # Remove the orphaned \begin{env}[...] from the line instead of
                # commenting out the entire line (which would hide text after it).
                lines[line_idx] = re.sub(
                    r'\\begin\{' + re.escape(env_name) + r'\}(\[[^\]]*\])?',
                    '',
                    lines[line_idx],
                    count=1
                )

        return '\n'.join(lines)

    def _find_input_files(self, content: str, source_dir: str) -> list[tuple[str, str]]:
        r"""Tim tat ca file duoc \input{...} trong noi dung LaTeX.

        Returns:
            List cac tuple (relative_path, absolute_path) cua cac file ton tai.

        Bo qua cac path khong an toan: tuyet doi, chua "..", hoac escape source_dir
        (CVE-style path traversal qua \input{../../etc/passwd}).
        """
        results = []
        # Match \input{path} - path co the co hoac khong co .tex
        for m in re.finditer(r'\\input\{([^}]+)\}', content):
            rel_path = m.group(1).strip()
            if not rel_path:
                continue
            # Reject absolute paths and any traversal attempt before touching FS
            if os.path.isabs(rel_path) or rel_path.startswith(("/", "\\")):
                print(f"[Pipeline] Bo qua \\input absolute path: {rel_path!r}")
                continue
            norm_parts = rel_path.replace("\\", "/").split("/")
            if ".." in norm_parts:
                print(f"[Pipeline] Bo qua \\input chua '..': {rel_path!r}")
                continue

            # Thu voi va khong co .tex extension
            candidates = [rel_path]
            if not rel_path.endswith('.tex'):
                candidates.append(rel_path + '.tex')

            for candidate in candidates:
                abs_path = os.path.join(source_dir, candidate)
                if not os.path.isfile(abs_path):
                    continue
                # Defence in depth: realpath check sau khi resolve symlink
                if not is_within_directory(source_dir, abs_path):
                    print(f"[Pipeline] Bo qua \\input escape source_dir: {rel_path!r}")
                    break
                results.append((rel_path, abs_path))
                break
        return results

    async def _translate_input_file(
        self,
        input_abs: str,
        input_rel: str,
        job_id: str,
        page,
        context,
        progress: dict,
        chunks_since_new_chat: int = 0,
        global_done: int = 0,
        total_global: int = 0,
    ):
        """Dich mot file \\input{...} tuong tu nhu dich body chinh.

        Returns:
            (context, page, chunks_since_new_chat, global_done)
        """
        with open(input_abs, "r", encoding="utf-8", errors="ignore") as f:
            input_content = f.read()

        input_chunks = split_into_chunks(input_content)
        total = len(input_chunks)
        print(f"[Pipeline]   File '{input_rel}': {total} chunk")

        # Key luu tien trinh rieng cho moi input file
        input_progress_key = f"input_chunks:{input_rel}"
        translated_chunks = progress.get(input_progress_key, {})

        for i, chunk in enumerate(input_chunks):
            chunk_key = str(i)

            if chunk_key in translated_chunks and translated_chunks[chunk_key].strip():
                global_done += 1
                print(f"[Pipeline]   {input_rel} chunk {i+1}/{total} - da dich, bo qua")
                if self._audit is not None:
                    self._audit.log("chunk.skipped_resume", chunk_idx=i,
                                    scope="input_file", input_rel=input_rel)
                continue

            chunk_to_send = self._strip_comments(chunk)

            if not chunk_to_send.strip():
                translated_chunks[chunk_key] = chunk
                global_done += 1
                progress[input_progress_key] = translated_chunks
                progress["status"] = f"translating {global_done}/{total_global}"
                self._save_progress(progress)
                print(f"[Pipeline]   {input_rel} chunk {i+1}/{total} - chi co comment, bo qua")
                if self._audit is not None:
                    self._audit.log("chunk.skipped_comment_only", chunk_idx=i,
                                    scope="input_file", input_rel=input_rel)
                continue

            # Inline translation for structural-only chunks (avoid Gemini for single-line headings)
            inline_result = self._translate_inline(chunk_to_send)
            if inline_result is not None:
                translated_chunks[chunk_key] = inline_result
                global_done += 1
                progress[input_progress_key] = translated_chunks
                progress["status"] = f"translating {global_done}/{total_global}"
                self._save_progress(progress)
                print(f"[Pipeline]   {input_rel} chunk {i+1}/{total} - dich inline (structural), bo qua Gemini")
                if self._audit is not None:
                    self._audit.log(
                        "chunk.translated_inline",
                        chunk_idx=i,
                        scope="input_file",
                        input_rel=input_rel,
                        original_chars=len(chunk),
                        translated_chars=len(inline_result),
                    )
                continue

            # Session rotation
            if chunks_since_new_chat >= self.CHUNKS_PER_SESSION:
                print(f"[Pipeline]   Dat gioi han {self.CHUNKS_PER_SESSION} chunk/session, mo chat moi...")
                if self._audit is not None:
                    self._audit.log(
                        "session.rotated",
                        reason="chunk_limit",
                        chunks_in_session=chunks_since_new_chat,
                        limit=self.CHUNKS_PER_SESSION,
                        at_chunk=i,
                        scope="input_file",
                        input_rel=input_rel,
                    )
                try:
                    await self.translator.start_new_chat(page)
                except Exception as e:
                    if self._audit is not None:
                        self._audit.log("session.rotation_failed",
                                        error=str(e)[:200],
                                        at_chunk=i, scope="input_file",
                                        input_rel=input_rel)
                chunks_since_new_chat = 0
                await asyncio.sleep(2)

            print(f"[Pipeline]   {input_rel} chunk {i+1}/{total} ({len(chunk_to_send)} ky tu)...")
            chunk_started_at = time.time()
            if self._audit is not None:
                self._audit.log(
                    "chunk.translate_started",
                    chunk_idx=i,
                    scope="input_file",
                    input_rel=input_rel,
                    original_chars=len(chunk),
                    send_chars=len(chunk_to_send),
                )

            translated = ""
            attempts_used = 0
            for attempt in range(3):
                attempts_used = attempt + 1
                try:
                    translated = await self.translator.translate_chunk(page, chunk_to_send)
                    if translated.strip():
                        if self._is_response_truncated(chunk_to_send, translated):
                            print(f"[Pipeline]   Response bi cut ngan, mo chat moi va thu lai...")
                            if self._audit is not None:
                                self._audit.log(
                                    "chunk.truncated_detected",
                                    chunk_idx=i,
                                    scope="input_file",
                                    input_rel=input_rel,
                                    attempt=attempt + 1,
                                    original_chars=len(chunk_to_send),
                                    translated_chars=len(translated),
                                    ratio=round(len(translated) / max(1, len(chunk_to_send)), 3),
                                )
                            await self.translator.start_new_chat(page)
                            chunks_since_new_chat = 0
                            await asyncio.sleep(2)
                            continue
                        break
                except TimeoutError as e:
                    print(f"[Pipeline]   Gemini timeout {input_rel} chunk {i+1} (lan {attempt+1}): {e}")
                    if self._audit is not None:
                        self._audit.log(
                            "chunk.attempt_failed",
                            chunk_idx=i,
                            scope="input_file",
                            input_rel=input_rel,
                            attempt=attempt + 1,
                            error_type="timeout",
                            error=str(e)[:200],
                        )
                    try:
                        await self.translator.start_new_chat(page)
                    except Exception:
                        context, page = await self._recover_browser(context, page)
                    chunks_since_new_chat = 0
                    await asyncio.sleep(5)

                except Exception as e:
                    print(f"[Pipeline]   Loi {input_rel} chunk {i+1} (lan {attempt+1}): {e}")
                    if self._audit is not None:
                        self._audit.log(
                            "chunk.attempt_failed",
                            chunk_idx=i,
                            scope="input_file",
                            input_rel=input_rel,
                            attempt=attempt + 1,
                            error_type=type(e).__name__,
                            error=str(e)[:200],
                        )

                # Relaunch browser neu page/context da chet
                context, page = await self._recover_browser(context, page)
                chunks_since_new_chat = 0
                await asyncio.sleep(2)

            translated_clean = self._extract_latex_from_response(translated)
            used_raw = False
            if not translated_clean.strip():
                translated_clean = translated
                used_raw = True

            translated_chunks[chunk_key] = translated_clean
            chunks_since_new_chat += 1
            global_done += 1

            # Luu tien trinh
            progress[input_progress_key] = translated_chunks
            progress["status"] = f"translating {global_done}/{total_global}"
            self._save_progress(progress)

            print(f"[Pipeline]   {input_rel} chunk {i+1}/{total} ({global_done}/{total_global} tong) - xong")
            if self._audit is not None:
                self._audit.log(
                    "chunk.translate_done",
                    chunk_idx=i,
                    scope="input_file",
                    input_rel=input_rel,
                    attempts=attempts_used,
                    raw_chars=len(translated),
                    translated_chars=len(translated_clean),
                    used_raw_fallback=used_raw,
                    latency_seconds=round(time.time() - chunk_started_at, 3),
                    global_progress=f"{global_done}/{total_global}",
                )
            await asyncio.sleep(1)

        return context, page, chunks_since_new_chat, global_done

    def _save_translated_input_files(
        self,
        job_id: str,
        input_files: list[tuple[str, str]],
        output_dir: str,
    ):
        """Ghep cac chunk da dich cua input files va luu vao output dir."""
        progress = self._load_progress(job_id)

        for input_rel, input_abs in input_files:
            input_progress_key = f"input_chunks:{input_rel}"
            translated_chunks = progress.get(input_progress_key, {})

            if not translated_chunks:
                continue

            # Doc file goc de biet so chunk
            with open(input_abs, "r", encoding="utf-8", errors="ignore") as f:
                original_content = f.read()
            original_chunks = split_into_chunks(original_content)

            # Ghep cac chunk da dich
            translated_content = ""
            for i in range(len(original_chunks)):
                translated_content += translated_chunks.get(str(i), original_chunks[i])

            # Sua cac loi cau truc LaTeX do AI merge dong
            translated_content = self._fix_latex_structure(translated_content)

            # Xac dinh duong dan output (giu nguyen cau truc thu muc)
            # input_rel co the la "Sections/forget_loss" hoac "Sections/forget_loss.tex"
            out_rel = input_rel if input_rel.endswith('.tex') else input_rel + '.tex'
            out_path = os.path.join(output_dir, out_rel)

            # Path-traversal guard: input_rel co the do attacker kiem soat (qua
            # \\input{...} trong source LaTeX upload). Validate truoc khi mkdir
            # de tranh tao thu muc ngoai output_dir.
            os.makedirs(output_dir, exist_ok=True)
            if not is_within_directory(output_dir, out_path):
                print(f"[Pipeline] Bo qua input file escape output_dir: {input_rel!r}")
                continue
            parent = os.path.dirname(out_path) or output_dir
            os.makedirs(parent, exist_ok=True)

            with open(out_path, "w", encoding="utf-8") as f:
                f.write(translated_content)
            print(f"[Pipeline] Da luu input file da dich: {out_path}")

    def _copy_support_files(self, source_dir: str, output_dir: str, main_tex: str = ""):
        """Copy cac file phu tro tu source goc sang thu muc output."""
        main_tex_name = os.path.basename(main_tex) if main_tex else ""
        for item in os.listdir(source_dir):
            # Bo qua file .tex chinh (da co translated.tex thay the)
            if item == main_tex_name:
                continue
            src = os.path.join(source_dir, item)
            dst = os.path.join(output_dir, item)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
            elif os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
