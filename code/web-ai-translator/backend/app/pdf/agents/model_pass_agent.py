"""ModelPassAgent — Dịch toàn bộ tài liệu bằng 1 model, multi-tab song song.

Vai trò (con người tương ứng): 1 ĐỘI dịch trong nhóm — tất cả thành viên
cùng dùng 1 "phong cách nhà cung cấp" (vd. tất cả đều dùng Gemini), chia
việc theo K bàn (K tab) chạy song song. Khi đội down (rate limit, CAPTCHA)
→ trưởng nhóm dừng đội này, chuyển công việc còn lại sang đội khác.

Vai trò kỹ thuật:
  - Khởi tạo WebAITranslator riêng cho model_name (vd. "gemini")
  - Launch browser context, mở `num_tabs` page (tab) độc lập trong CÙNG context
  - Mỗi tab = 1 worker, chạy song song qua asyncio.gather
  - Worker kéo chunk từ asyncio.Queue chung
  - Worker dùng TranslatorAgent.translate_chunk(worker_page=tab) → không
    đụng ctx.page chính của coordinator
  - Lưu kết quả vào progress[output_key], file vào job_dir/chunks_{model}/
  - Phát hiện "model down" qua đếm consecutive failures → drain queue,
    return AgentResult với model_down=True để coordinator failover

Resume:
  Mỗi lần chạy, skip chunks đã có trong progress[output_key]. Worker dừng
  tự nhiên khi queue trống.

Multi-tab note:
  CÙNG 1 BrowserContext (cùng login session) → mở K tab qua context.new_page().
  Browser duy nhất, account duy nhất. Rate limit dùng chung → khuyến nghị
  num_tabs ≤ 3 để tránh CAPTCHA trigger.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.agents.translator_agent import TranslateRequest, TranslatorAgent
from app.services.translator import WebAITranslator


# Số lỗi liên tiếp toàn đội trước khi tuyên bố model down
FAILOVER_THRESHOLD = 3

# Trễ giữa các lần khởi tạo tab (tránh đụng nhau khi load page)
TAB_OPEN_DELAY = 2.0


@dataclass(frozen=True)
class ModelAttemptPlan:
    """Kế hoạch cho 1 attempt dịch/refine của một chunk."""

    chunk_index: int
    attempt_no: int
    strategy: str
    model: str
    candidate_models: tuple[str, ...]
    reason: str
    model_order: tuple[str, ...]
    available_model_order: tuple[str, ...] = ()
    unavailable_models: tuple[str, ...] = ()

    def provenance(self, *, selected_model: str | None = None) -> dict:
        return {
            "chunk_index": self.chunk_index,
            "attempt": self.attempt_no,
            "strategy": self.strategy,
            "model": self.model,
            "candidate_models": list(self.candidate_models),
            "selected_model": selected_model or self.model,
            "reason": self.reason,
            "model_order": list(self.model_order),
            "available_model_order": list(self.available_model_order),
            "unavailable_models": list(self.unavailable_models),
        }


class ModelAttemptScheduler:
    """Cấp model + provenance cho từng attempt theo thứ tự user chọn.

    Đây là phần cấp tài nguyên model dùng chung cho eval-loop. CriticAgent
    quyết định repair policy (refine/đổi model/ensemble/stop); scheduler này
    chỉ giữ thứ tự model, số attempt và dữ liệu provenance.
    """

    def __init__(self, models: list[str]):
        self.models = self.normalize_models(models)
        self._calls: dict[int, int] = {}
        self._last_model_by_chunk: dict[int, str] = {}
        self._down_models: dict[str, str] = {}

    @staticmethod
    def normalize_models(models: list[str] | None) -> list[str]:
        cleaned = [
            m.strip().lower()
            for m in (models or [])
            if m and m.strip()
        ]
        return cleaned or ["gemini"]

    def next_attempt(self, idx: int) -> ModelAttemptPlan:
        n = self._calls.get(idx, 0)
        self._calls[idx] = n + 1

        active = self.available_models()
        m0 = active[0]
        m1 = active[min(1, len(active) - 1)]
        previous_model = self._last_model_by_chunk.get(idx)
        if n > 0 and previous_model in self._down_models and previous_model != m0:
            return self._remember(self._plan(
                idx, n, "initial", m0, (m0,),
                f"model_failover_from_{previous_model}",
                active,
            ))
        if n == 0:
            return self._remember(
                self._plan(idx, n, "initial", m0, (m0,), "first_pass", active)
            )
        if n == 1:
            return self._remember(
                self._plan(idx, n, "refine", m0, (m0,), "critic_refine", active)
            )
        if n == 2:
            return self._remember(
                self._plan(idx, n, "escalate", m1, (m1,), "judge_retry", active)
            )
        return self._remember(self._plan(
            idx, n, "ensemble", m0, tuple(dict.fromkeys((m0, m1))),
            "best_of_candidates", active,
        ))

    def mark_model_down(self, model: str, reason: str = "consecutive_failures"):
        model = (model or "").strip().lower()
        if model and model in self.models:
            self._down_models[model] = reason

    def mark_model_healthy(self, model: str):
        model = (model or "").strip().lower()
        self._down_models.pop(model, None)

    def is_model_down(self, model: str) -> bool:
        return (model or "").strip().lower() in self._down_models

    def unavailable_models(self) -> dict[str, str]:
        return dict(self._down_models)

    def available_models(self) -> list[str]:
        active = [m for m in self.models if m not in self._down_models]
        return active or list(self.models)

    def _plan(
        self,
        idx: int,
        zero_based_attempt: int,
        strategy: str,
        model: str,
        candidates: tuple[str, ...],
        reason: str,
        active_models: list[str],
    ) -> ModelAttemptPlan:
        return ModelAttemptPlan(
            chunk_index=idx,
            attempt_no=zero_based_attempt + 1,
            strategy=strategy,
            model=model,
            candidate_models=candidates,
            reason=reason,
            model_order=tuple(self.models),
            available_model_order=tuple(active_models),
            unavailable_models=tuple(self._down_models.keys()),
        )

    def _remember(self, plan: ModelAttemptPlan) -> ModelAttemptPlan:
        self._last_model_by_chunk[plan.chunk_index] = plan.model
        return plan


class ModelPassAgent(BaseAgent):
    """1 pass dịch bằng 1 model, multi-tab song song.

    Coordinator chạy ModelPassAgent("gemini") rồi ModelPassAgent("chatgpt")
    nối tiếp. Mỗi agent độc lập về browser/account/login.

    Output:
      progress[output_key] = {chunk_idx_str: translated_text, ...}
      file: job_dir/chunks_{model}/chunk_XXX_original.txt + _translated.txt
    """

    def __init__(
        self,
        model_name: str,
        num_tabs: int = 2,
        output_key: Optional[str] = None,
    ):
        self.model_name = model_name.lower()
        self.num_tabs = max(1, num_tabs)
        self.output_key = output_key or f"translated_chunks_{self.model_name}"
        self.name = f"ModelPassAgent[{self.model_name}]"
        self.translator_agent = TranslatorAgent()

    @staticmethod
    def create_attempt_scheduler(models: list[str] | None) -> ModelAttemptScheduler:
        return ModelAttemptScheduler(models)

    async def run(self, ctx: AgentContext) -> AgentResult:
        if ctx.is_cancelled():
            return AgentResult.fail("Cancelled before start", recoverable=True)

        if not ctx.chunks:
            return AgentResult.fail(
                "No chunks to translate — run PlannerAgent first",
                recoverable=False,
            )

        # Skip-check: đã làm hết
        existing = ctx.progress.get(self.output_key, {})
        pending = [
            idx for idx in range(len(ctx.chunks)) if str(idx) not in existing
        ]
        if not pending:
            self.log(
                f"All {len(ctx.chunks)} chunks already done by {self.model_name}"
            )
            return AgentResult.ok(
                data={"output_key": self.output_key, "done_count": len(existing)},
                model=self.model_name,
                model_down=False,
                chunks_done=len(existing),
                chunks_total=len(ctx.chunks),
                skipped_resume=True,
            )

        # ── Launch browser + open K tabs ────────────────────────────────
        local_translator = WebAITranslator(backend=self.model_name)
        try:
            context, main_page = await local_translator.launch_browser()
        except Exception as e:
            return AgentResult.fail(
                f"Browser launch failed for {self.model_name}: {e}",
                recoverable=True,
            )

        tabs = [main_page]
        for i in range(self.num_tabs - 1):
            try:
                tab = await context.new_page()
                await asyncio.sleep(TAB_OPEN_DELAY)
                await local_translator._backend.start_new_chat(tab)
                tabs.append(tab)
                self.log(f"Opened tab {i + 2}/{self.num_tabs}")
            except Exception as e:
                self.log(f"Failed to open tab {i + 2}: {e}", "warn")

        self.log(
            f"Ready: {len(tabs)} tabs × {self.model_name}, "
            f"{len(pending)} pending / {len(ctx.chunks)} total"
        )

        # ── Build queue + shared state ──────────────────────────────────
        queue: asyncio.Queue = asyncio.Queue()
        for idx in pending:
            queue.put_nowait(idx)

        # Shared mutable state across workers (use list as ref)
        consecutive_failures = [0]
        model_down = [False]
        first_failure_chunk = [None]
        save_lock = asyncio.Lock()

        style_anchor = ctx.progress.get("style_anchor")
        total_chunks = len(ctx.chunks)

        # ── Worker coroutine ────────────────────────────────────────────
        async def worker(wid: int, tab):
            while True:
                if ctx.is_cancelled() or model_down[0]:
                    return
                try:
                    idx = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                # Build per-worker context — share most but override page
                worker_ctx = AgentContext(
                    job_id=ctx.job_id,
                    job_dir=ctx.job_dir,
                    pdf_path=ctx.pdf_path,
                    mode=ctx.mode,
                    blocks=ctx.blocks,
                    chunks=ctx.chunks,
                    plan=ctx.plan,
                    glossary=ctx.glossary,
                    glossary_enabled=ctx.glossary_enabled,
                    locked_terms=ctx.locked_terms,
                    memory=None,                          # disable cross-chunk memory in parallel
                    translator=local_translator,
                    page=tab,
                    context=context,
                    progress=ctx.progress,
                    save_progress=ctx.save_progress,
                    is_cancelled=lambda: (
                        ctx.is_cancelled() or model_down[0]
                    ),
                    ensure_page=(lambda t=tab: _identity_page(t)),
                    settings=ctx.settings,
                )

                section_hint = ""
                if ctx.plan is not None:
                    sec = ctx.plan.section_for_chunk(idx)
                    if sec:
                        section_hint = sec.title

                request = TranslateRequest(
                    chunk_index=idx,
                    chunk=ctx.chunks[idx],
                    section_hint=section_hint,
                    max_retries=ctx.settings.get("max_retries", 2),
                    base_backoff=ctx.settings.get("base_backoff", 5),
                    style_anchor=style_anchor,
                    anti_hallucination=True,
                    worker_page=tab,
                )

                self.log(f"[w{wid}] chunk {idx + 1}/{total_chunks}")
                result = await self.translator_agent.translate_chunk(
                    worker_ctx, request
                )
                data = result.data or {}
                translated = data.get("translated", "")
                original = data.get("original", "")

                async with save_lock:
                    if result.success and translated:
                        consecutive_failures[0] = 0
                        done = ctx.progress.setdefault(self.output_key, {})
                        done[str(idx)] = translated
                        self._save_chunk(ctx.job_dir, idx, original, translated)
                        # Track failed chunks per model
                        failed_key = f"failed_chunks_{self.model_name}"
                        failed_list = ctx.progress.get(failed_key, [])
                        if idx in failed_list:
                            failed_list.remove(idx)
                            ctx.progress[failed_key] = failed_list
                        ctx.progress["status"] = (
                            f"{self.model_name}: "
                            f"{len(done)}/{total_chunks}"
                        )
                        ctx.save_progress()
                    else:
                        consecutive_failures[0] += 1
                        if first_failure_chunk[0] is None:
                            first_failure_chunk[0] = idx
                        failed_key = f"failed_chunks_{self.model_name}"
                        failed_list = ctx.progress.setdefault(failed_key, [])
                        if idx not in failed_list:
                            failed_list.append(idx)
                        ctx.save_progress()
                        self.log(
                            f"[w{wid}] chunk {idx + 1} FAILED "
                            f"(consecutive: {consecutive_failures[0]}/"
                            f"{FAILOVER_THRESHOLD})",
                            "warn",
                        )
                        if consecutive_failures[0] >= FAILOVER_THRESHOLD:
                            model_down[0] = True
                            self.log(
                                f"MODEL {self.model_name.upper()} DOWN — "
                                f"failover triggered at chunk {idx + 1}",
                                "error",
                            )
                            # Drain queue so other workers stop pulling
                            while not queue.empty():
                                try:
                                    queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break

        # ── Run K workers in parallel ───────────────────────────────────
        try:
            await asyncio.gather(
                *[worker(i, tab) for i, tab in enumerate(tabs)],
                return_exceptions=True,
            )
        finally:
            try:
                await local_translator.cleanup()
            except Exception as e:
                self.log(f"Cleanup error: {e}", "warn")

        # ── Report ──────────────────────────────────────────────────────
        done = ctx.progress.get(self.output_key, {})
        done_count = len([k for k, v in done.items() if v])

        metrics = {
            "model": self.model_name,
            "tabs": len(tabs),
            "chunks_done": done_count,
            "chunks_total": total_chunks,
            "model_down": model_down[0],
            "first_failure_chunk": first_failure_chunk[0],
        }

        if model_down[0]:
            return AgentResult(
                success=False,
                data={"output_key": self.output_key, "done_count": done_count},
                errors=[
                    f"Model {self.model_name} declared down after "
                    f"{FAILOVER_THRESHOLD} consecutive failures starting "
                    f"at chunk {first_failure_chunk[0]}"
                ],
                recoverable=True,
                metrics=metrics,
            )

        return AgentResult.ok(
            data={"output_key": self.output_key, "done_count": done_count},
            **metrics,
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    def _save_chunk(
        self, job_dir: str, idx: int, original: str, translated: str
    ):
        chunks_dir = os.path.join(job_dir, f"chunks_{self.model_name}")
        os.makedirs(chunks_dir, exist_ok=True)
        with open(
            os.path.join(chunks_dir, f"chunk_{idx:03d}_original.txt"),
            "w", encoding="utf-8",
        ) as f:
            f.write(original)
        with open(
            os.path.join(chunks_dir, f"chunk_{idx:03d}_translated.txt"),
            "w", encoding="utf-8",
        ) as f:
            f.write(translated)


# ── Helpers tách rời (vì lambda với await không gọn) ─────────────────────

async def _identity_page(tab):
    """ensure_page replacement cho worker tab — không relaunch, trả tab gốc.

    Nếu tab chết, _translate_with_retry sẽ bắt TargetClosedError và
    return "" → ModelPassAgent đếm failure → có thể trigger failover.
    """
    return tab
