"""MultiAgentCoordinator — Nhạc trưởng cho chain agent end-to-end.

Vai trò:
  Coordinator KHÔNG tự dịch / sửa / chấm — mọi việc đó giao cho agent
  chuyên biệt. Nhiệm vụ duy nhất: khởi tạo AgentContext, gọi tuần tự các
  agent theo workflow, xử lý kết quả + persist progress, quản lý vòng đời
  browser cho các phase cần dùng chung và đóng nó lại trước eval-loop để
  tránh xung đột với các browser riêng trong vòng dịch.

Workflow (theo CLAUDE.md):
  ┌─ Extract       (ExtractorAgent)        — trích text blocks
  ├─ Plan          (PlannerAgent)          — sinh chunks + sections
  ├─ Glossary      (GlossaryAgent)         — cần browser chính
  ├─ StyleAnchor   (StyleAnchorAgent)      — cần browser chính
  ├─ EvalLoop                              — dịch ∥ đánh giá ∥ sửa
  ├─ Rebuild       (RebuilderAgent)        — chèn text dịch vào PDF
  ├─ Proofread     (ProofreaderAgent)
  └─ Report        (ReportAgent)           — chốt done / done_with_warnings

Vòng khép kín (eval-loop) là đường mặc định: Translate → panel judge
(LocalJudge per-chunk + GlossaryJudge/JudgeAgent gộp batch) → Critic-hub gom
lỗi → thang sửa (refine → đổi model → đa ứng viên). Standalone Critic đã bỏ.

Resume:
  - progress["translated_chunks"] là bản tốt nhất do eval-loop chốt.
  - Sau Extract+Plan, _apply_cached_translations() apply cache ngược lại
    blocks để các phase downstream (Critic, Rebuild) thấy đúng nội dung.
"""

from __future__ import annotations

import json
import os
import time

from app.pdf.agents.base import AgentContext
from app.pdf.agents.extractor_agent import ExtractorAgent
from app.pdf.agents.glossary_agent import GlossaryAgent
from app.pdf.agents.planner import PlannerAgent
from app.pdf.agents.proofreader_agent import ProofreaderAgent
from app.pdf.agents.rebuilder_agent import RebuilderAgent
from app.pdf.agents.report_agent import ReportAgent
from app.pdf.agents.style_anchor_agent import StyleAnchorAgent

from app.pdf.context_memory import ContextMemory
from app.pdf.processor import parse_translated_chunk
from app.services.translator import WebAITranslator
from app.utils.safe_io import atomic_write_json
from app import paths


# ── Mode-specific settings ────────────────────────────────────────────────────

MODE_SETTINGS = {
    "standard": {
        "chunks_per_session": 10,
        "delay_between_chunks": 2,
        "max_retries": 2,
        "base_backoff": 5,
    },
    "book": {
        "chunks_per_session": 5,
        "delay_between_chunks": 8,
        "max_retries": 4,
        "base_backoff": 15,
    },
}


class MultiAgentCoordinator:
    """Orchestrate end-to-end PDF translation qua chain các agent.

    Tham số:
      models             — preference user cho eval-loop,
                           default ["gemini", "chatgpt"].
      num_tabs           — số worker song song trong eval-loop.
      enable_normalizer  — legacy, không còn chạy trong coordinator.
      enable_judges      — legacy, không còn chạy trong coordinator.
      judge_model        — legacy, không dùng trong coordinator.
      enable_eval_loop   — legacy, không còn fallback pipeline cũ.
      judge_backend      — gate đánh giá trong loop: "web" (cross-model) |
                           "cometkiwi" | None/"off". Mặc định "web".
    """

    def __init__(
        self,
        work_dir: str | None = None,
        mode: str = "standard",
        models: list[str] | None = None,
        num_tabs: int = 2,
        enable_normalizer: bool = True,
        enable_judges: bool = False,
        judge_model: str = "qwen2.5:7b",
        enable_eval_loop: bool = True,
        judge_backend: str | None = "web",
    ):
        self.work_dir = work_dir or paths.workspace_dir()
        self.mode = mode if mode in MODE_SETTINGS else "standard"
        self.settings = MODE_SETTINGS[self.mode]
        from app.pdf.model_preference import (
            expand_model_execution_order,
            normalize_model_preference,
        )
        self.model_preference = normalize_model_preference(
            models or ["gemini", "chatgpt"]
        )
        self.models = expand_model_execution_order(self.model_preference)
        self.num_tabs = max(1, num_tabs)
        # Vòng dịch ∥ đánh giá ∥ sửa khép kín là đường dịch duy nhất của agentic.
        # Tham số enable_eval_loop chỉ còn để không phá caller cũ, không còn
        # bật/tắt fallback cũ.
        _ = (enable_normalizer, enable_judges, judge_model)
        if enable_eval_loop is False:
            print(
                "[Coordinator] enable_eval_loop=False is deprecated; "
                "agentic pipeline always uses eval-loop."
            )
        self.judge_backend = judge_backend

        # Translator chính (glossary / style anchor dùng chung).
        # Backend mặc định = model đứng đầu priority — để judge có hint
        # đúng cho "model dịch chính" khi pick_judge_backend chọn judge.
        primary_model = self.models[0] if self.models else "gemini"
        self.translator = WebAITranslator(backend=primary_model)

        # Persistent state
        self.progress_file = ""
        self._cancelled = False
        self._page = None
        self._context = None

        # Agent instances (idempotent — tạo 1 lần dùng nhiều)
        self.extractor = ExtractorAgent()
        self.planner = PlannerAgent()
        self.glossary_agent = GlossaryAgent()
        self.style_anchor_agent = StyleAnchorAgent()
        self.rebuilder = RebuilderAgent()
        self.proofreader = ProofreaderAgent()
        self.report_agent = ReportAgent()

    def cancel(self):
        self._cancelled = True

    # ── Persistence helpers ────────────────────────────────────────────────

    def _job_dir(self, job_id: str) -> str:
        d = os.path.join(self.work_dir, "jobs", job_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _load_progress(self, job_id: str) -> dict:
        self.progress_file = os.path.join(self._job_dir(job_id), "progress.json")
        if os.path.exists(self.progress_file):
            with open(self.progress_file, encoding="utf-8") as f:
                return json.load(f)
        return {
            "translated_chunks": {},
            "status": "pending",
            "source_type": "pdf_only",
            "agentic": True,
        }

    def _save_progress(self, progress: dict):
        # Atomic — readers (status polling) không bao giờ thấy JSON dở.
        atomic_write_json(self.progress_file, progress)

    def _record_timeline(
        self,
        progress: dict,
        phase: str,
        *,
        label: str,
        description: str = "",
        duration_seconds: float = 0.0,
        status: str = "done",
    ) -> None:
        timeline = progress.setdefault("phase_timeline", [])
        entry = {
            "phase": phase,
            "label": label,
            "description": description,
            "duration_seconds": round(max(0.0, float(duration_seconds or 0.0)), 3),
            "status": status,
            "finished_at": time.time(),
        }
        for i, old in enumerate(timeline):
            if old.get("phase") == phase:
                timeline[i] = entry
                break
        else:
            timeline.append(entry)

    @staticmethod
    def _phase_label(name: str) -> str:
        return {
            "extract": "Trích xuất (Extraction)",
            "plan": "Lập kế hoạch",
            "glossary": "Thuật ngữ",
            "style_anchor": "Neo văn phong",
            "rebuild": "Xây dựng lại (Rebuild)",
            "proofread": "Kiểm tra",
            "report": "Báo cáo",
        }.get(name, name)

    def _external_stop_requested(self, progress: dict) -> bool:
        """Read soft pause/cancel flags written by API routes."""
        if self._cancelled:
            return True
        try:
            if not self.progress_file or not os.path.exists(self.progress_file):
                return False
            with open(self.progress_file, "r", encoding="utf-8") as f:
                disk_progress = json.load(f)
        except Exception:
            return False

        status = str(disk_progress.get("status", "")).strip().lower()
        if disk_progress.get("pause_requested") or status in ("paused", "pausing"):
            progress["pause_requested"] = True
            progress["paused_at"] = disk_progress.get("paused_at", time.time())
            progress["status"] = "paused"
            return True
        if status == "cancelled" or disk_progress.get("cancel_requested"):
            self._cancelled = True
            progress["status"] = "cancelled"
            return True
        return False

    # ── Browser lifecycle (Glossary / StyleAnchor / Critic) ────────────────

    async def _ensure_page(self):
        """Return live page; relaunch nếu chết. Eval-loop KHÔNG dùng cái này."""
        from playwright._impl._errors import (
            Error as PlaywrightError,
            TargetClosedError,
        )

        if self._page is not None:
            try:
                await self._page.evaluate("1")
                return self._page
            except (TargetClosedError, PlaywrightError, Exception):
                print("[Coordinator] Browser closed — relaunching...")
                try:
                    await self.translator.cleanup()
                except Exception:
                    pass
                self._page = None
                self._context = None

        print(
            f"[Coordinator] Launching browser "
            f"(backend={self.translator.backend_name})..."
        )
        self._context, self._page = await self.translator.launch_browser()
        return self._page

    async def _close_browser(self):
        if self._page is None and self._context is None:
            return
        try:
            await self.translator.cleanup()
        except Exception as e:
            print(f"[Coordinator] Browser cleanup error: {e}")
        self._page = None
        self._context = None

    # ── Main entry ─────────────────────────────────────────────────────────

    async def run(self, pdf_path: str, job_id: str) -> str:
        """End-to-end pipeline. Trả về đường dẫn PDF dịch."""
        self._cancelled = False
        job_dir = self._job_dir(job_id)
        progress = self._load_progress(job_id)
        progress["agentic"] = True
        progress["mode"] = self.mode
        progress["model_preference"] = self.model_preference
        progress["models"] = self.models
        progress["num_tabs"] = self.num_tabs
        progress["started_at"] = time.time()   # benchmark: tabs vs duration

        print(f"[Coordinator] Starting agentic job {job_id}")
        print(f"[Coordinator] PDF: {pdf_path}")
        print(
            f"[Coordinator] Models: {self.models} "
            f"(×{self.num_tabs} tabs each)"
        )

        memory = ContextMemory()
        memory.load_from_progress(progress)

        ctx = AgentContext(
            job_id=job_id,
            job_dir=job_dir,
            pdf_path=pdf_path,
            mode=self.mode,
            blocks=[],
            chunks=[],
            plan=None,
            glossary=progress.get("glossary", {}).get("terms", {}),
            glossary_enabled=progress.get("glossary", {}).get("enabled", True),
            locked_terms=progress.get("glossary", {}).get("locked", []),
            memory=memory,
            translator=self.translator,
            page=None,
            context=None,
            progress=progress,
            save_progress=lambda: self._save_progress(progress),
            is_cancelled=lambda: self._external_stop_requested(progress),
            ensure_page=self._ensure_page,
            settings=self.settings,
        )

        try:
            # ── 1: Extract ───────────────────────────────────────────────
            await self._run_phase(
                "extract", "extracting text blocks",
                self.extractor, ctx, progress, abort_on_fail=True,
            )

            # ── 2: Plan ──────────────────────────────────────────────────
            await self._run_phase(
                "plan", "planning chunks",
                self.planner, ctx, progress, abort_on_fail=True,
            )
            total_chunks = len(ctx.chunks)
            progress["total_chunks"] = total_chunks   # DB sync + benchmark
            print(f"[Coordinator] {total_chunks} chunks planned")

            # Restore memory + apply cached translations (resume support)
            if progress.get("translated_chunks"):
                self._rebuild_memory_from_disk(memory, job_dir, progress)
                self._apply_cached_translations(ctx)

            # ── 3: Glossary (cần browser chính) ──────────────────────────
            done_any = bool(progress.get("translated_chunks"))
            if not ctx.glossary and not done_any:
                await self._run_phase(
                    "glossary", "extracting glossary",
                    self.glossary_agent, ctx, progress, abort_on_fail=False,
                )
            else:
                why = "glossary cached" if ctx.glossary else "resuming"
                print(f"[Coordinator] Skipping glossary extraction ({why})")

            glossary_state = progress.get("glossary", {}) or {}
            glossary_extraction = glossary_state.get("extraction", {}) or {}
            glossary_has_document_terms = bool(glossary_state.get("document_terms"))
            translated_any = bool(
                progress.get("translated_chunks")
                or progress.get("translation_provenance")
                or progress.get("translation_attempts")
            )
            if (
                glossary_state.get("terms")
                and glossary_extraction.get("attempted")
                and not glossary_extraction.get("ok")
                and not glossary_has_document_terms
                and not translated_any
                and not progress.get("style_anchor")
            ):
                err = glossary_extraction.get("error") or "Không trích được thuật ngữ mới từ tài liệu"
                progress["status"] = f"error in glossary: {err[:160]}"
                progress["phase"] = "glossary"
                self._save_progress(progress)
                await self._close_browser()
                raise RuntimeError(progress["status"])

            needs_glossary_review = (
                glossary_state.get("terms")
                and glossary_has_document_terms
                and not glossary_state.get("approved", False)
                and not translated_any
                and not progress.get("style_anchor")
            )
            if needs_glossary_review:
                if not glossary_state.get("awaiting_review", False):
                    glossary_state["awaiting_review"] = True
                    progress["glossary"] = glossary_state
                    self._save_progress(progress)
                    print(
                        "[Coordinator] Glossary ready; continuing to style anchor "
                        f"before review ({len(glossary_state.get('terms') or {})} terms)"
                    )

            # ── 4: StyleAnchor (cần browser chính) ───────────────────────
            await self._run_phase(
                "style_anchor", "creating style anchor",
                self.style_anchor_agent, ctx, progress, abort_on_fail=False,
            )

            style_state = progress.get("style_anchor", {}) or {}
            translated_any = bool(
                progress.get("translated_chunks")
                or progress.get("translation_provenance")
                or progress.get("translation_attempts")
            )
            needs_style_review = (
                style_state.get("en")
                and style_state.get("vi")
                and not style_state.get("approved", False)
                and not translated_any
            )
            if needs_style_review and not style_state.get("awaiting_review", False):
                style_state["awaiting_review"] = True
                progress["style_anchor"] = style_state
                print("[Coordinator] Style anchor ready; waiting for review")

            needs_glossary_review = (
                glossary_state.get("awaiting_review")
                and not glossary_state.get("approved", False)
            )
            needs_style_review = (
                style_state.get("awaiting_review")
                and not style_state.get("approved", False)
            )
            if needs_glossary_review or needs_style_review:
                if needs_style_review:
                    progress["status"] = "awaiting_style_review"
                    progress["phase"] = "style_anchor_review"
                else:
                    progress["status"] = "awaiting_glossary_review"
                    progress["phase"] = "glossary_review"
                self._save_progress(progress)
                await self._close_browser()
                return ""

            # Đóng browser chính — eval-loop tự quản browser riêng
            await self._close_browser()

            # ── 5: Dịch + đánh giá + sửa khép kín ────────────────────────
            await self._run_eval_loop_phase(ctx, progress)
            if progress.get("pause_requested") or progress.get("status") == "paused":
                progress["status"] = "paused"
                self._save_progress(progress)
                return ""
            if self._cancelled:
                progress["status"] = "cancelled"
                self._save_progress(progress)
                return ""

            # Critic + Refine giờ nằm TRONG vòng khép kín (thang sửa lần 1) —
            # không còn là phase độc lập sau merge.

            # Đóng browser trước rebuild
            await self._close_browser()
            if self._cancelled:
                progress["status"] = "cancelled"
                self._save_progress(progress)
                return ""

            # ── 6: Rebuild PDF ───────────────────────────────────────────
            await self._run_phase(
                "rebuild", "rebuilding PDF",
                self.rebuilder, ctx, progress, abort_on_fail=True,
            )

            # ── 7: Proofread ─────────────────────────────────────────────
            await self._run_phase(
                "proofread", "proofreading PDF",
                self.proofreader, ctx, progress, abort_on_fail=False,
            )

            # ── Merge glossary toàn cục ──────────────────────────────────
            final_glossary = progress.get("glossary", {}).get("terms", {})
            final_fields = progress.get("glossary", {}).get("fields", {})
            if final_glossary:
                try:
                    from app.database import merge_job_glossary_to_global
                    merge_job_glossary_to_global(job_id, final_glossary, fields=final_fields)
                    print(
                        f"[Coordinator] Merged {len(final_glossary)} "
                        f"global terms ({len(final_fields or {})} có lĩnh vực)"
                    )
                except Exception as e:
                    print(f"[Coordinator] Global merge failed: {e}")

            # ── 8: Report — chốt trạng thái cuối (ReportAgent) ───────────
            await self._run_phase(
                "report", "finalizing report",
                self.report_agent, ctx, progress, abort_on_fail=False,
            )
            progress["status"] = progress.get("report", {}).get("final_status", "done")
            _started = progress.get("started_at")
            if _started:
                progress["duration_seconds"] = round(time.time() - _started, 1)
            progress["phase"] = "done"
            self._save_progress(progress)

            output_path = progress.get("output_path", "")
            print(f"[Coordinator] Done! Output: {output_path}")
            return output_path

        finally:
            await self._close_browser()

    # ── Sub-runners ────────────────────────────────────────────────────────

    async def _run_phase(
        self,
        name: str,
        status_msg: str,
        agent,
        ctx: AgentContext,
        progress: dict,
        *,
        abort_on_fail: bool = False,
    ):
        """Chạy 1 agent + log/status chuẩn.

        abort_on_fail=True và result.recoverable=False → raise.
        Mọi trường hợp khác chỉ log + tiếp tục (best-effort).
        """
        if self._cancelled:
            return
        t0 = time.time()
        progress["status"] = status_msg
        progress["phase"] = name
        self._save_progress(progress)
        print(f"[Coordinator] ── Phase {name}: {status_msg} ──")

        result = await agent.execute(ctx)
        elapsed = time.time() - t0

        if not result.success:
            errs = ", ".join(result.errors) if result.errors else "unknown"
            self._record_timeline(
                progress,
                name,
                label=self._phase_label(name),
                description=errs,
                duration_seconds=elapsed,
                status="failed",
            )
            self._save_progress(progress)
            if abort_on_fail and not result.recoverable:
                progress["status"] = f"error: {name}: {errs}"
                self._save_progress(progress)
                raise RuntimeError(f"{name} failed: {errs}")
            print(f"[Coordinator] Phase {name} non-fatal: {errs}")
        else:
            self._record_timeline(
                progress,
                name,
                label=self._phase_label(name),
                description=f"{self._phase_label(name)} hoàn tất.",
                duration_seconds=elapsed,
                status="done",
            )
            self._save_progress(progress)

    async def _run_eval_loop_phase(self, ctx: AgentContext, progress: dict):
        """Vòng dịch ∥ đánh giá ∥ sửa khép kín.

        Dùng thứ tự self.models làm bậc thang dịch lại + judge backend user chọn
        (≠ model dịch). run_eval_loop tự quản browser, ghi best-so-far vào
        progress["translated_chunks"] + report vào progress["eval_loop"].
        """
        from app.pdf.eval_adapters import run_eval_loop
        from app.pdf.eval_pipeline import EvalConfig

        jb = self.judge_backend
        progress["status"] = f"eval-loop (judge={jb or 'off'})"
        progress["phase"] = "eval_loop"
        self._save_progress(progress)
        print(
            f"[Coordinator] ── Eval-loop: models={self.models}, "
            f"judge={jb or 'off'} ──"
        )
        cfg = EvalConfig(num_workers=max(1, self.num_tabs), judge_batch_size=5)
        t0 = time.time()
        report = await run_eval_loop(ctx, self.models, jb, cfg)
        elapsed = time.time() - t0
        self._record_timeline(
            progress,
            "eval_loop",
            label="Vòng lặp Dịch/Review/Sửa",
            description=(
                f"{len(ctx.chunks)} chunks xử lý qua "
                f"{', '.join(self.models) if self.models else 'model dịch'}."
            ),
            duration_seconds=elapsed,
            status="done" if not report.cancelled else "cancelled",
        )
        self._save_progress(progress)
        print(
            f"[Coordinator] Eval-loop done: {len(report.passed)} passed, "
            f"{len(report.flagged)} flagged, "
            f"{report.total_translations} translations, "
            f"{report.total_judge_calls} judge calls"
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _apply_cached_translations(self, ctx: AgentContext):
        """Sau resume, apply progress["translated_chunks"] vào ctx.blocks.

        ExtractorAgent vừa trả về blocks "trắng" (chưa có translated_text).
        Rebuild đọc trực tiếp block.translated_text — cần khôi phục bản dịch
        đã có trước đó để phase này thấy đúng.
        """
        cached = ctx.progress.get("translated_chunks", {})
        if not cached:
            return
        applied = 0
        for ci, chunk in enumerate(ctx.chunks):
            saved = cached.get(str(ci), "")
            if not saved:
                continue
            try:
                parse_translated_chunk(saved, chunk)
                applied += 1
            except Exception as e:
                print(
                    f"[Coordinator] parse_translated_chunk @ {ci}: {e}"
                )
        if applied:
            print(
                f"[Coordinator] Restored {applied} chunks to blocks "
                f"from cache"
            )

    @staticmethod
    def _rebuild_memory_from_disk(
        memory: ContextMemory, job_dir: str, progress: dict
    ) -> int:
        """Resume support: load chunk text files vào ContextMemory."""
        chunks_dir = os.path.join(job_dir, "chunks")
        if not os.path.isdir(chunks_dir):
            return 0
        translated_chunks = progress.get("translated_chunks", {})
        if not translated_chunks:
            return 0

        added = 0
        for chunk_key in sorted(translated_chunks.keys(), key=lambda k: int(k)):
            try:
                idx = int(chunk_key)
            except ValueError:
                continue
            orig_path = os.path.join(
                chunks_dir, f"chunk_{idx:03d}_original.txt"
            )
            trans_path = os.path.join(
                chunks_dir, f"chunk_{idx:03d}_translated.txt"
            )
            if not (os.path.isfile(orig_path) and os.path.isfile(trans_path)):
                continue
            try:
                with open(orig_path, encoding="utf-8") as f:
                    original = f.read()
                with open(trans_path, encoding="utf-8") as f:
                    translated = f.read()
                if original.strip() and translated.strip():
                    memory.add(idx, original, translated)
                    added += 1
            except Exception:
                pass

        if added:
            print(
                f"[Coordinator] Rebuilt memory from disk: {added} chunks"
            )
        return added
