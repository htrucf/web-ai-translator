"""eval_pipeline.py — Vòng lặp dịch–đánh giá ĐỒNG THỜI (concurrent quality loop).

Thiết kế quality-driven: dịch và đánh giá chạy SONG SONG theo producer–consumer.

  ┌─ K translate worker ─────────────────────────────────────────────┐
  │  (dịch / dịch lại) 1 chunk → chấm HEURISTIC ngay (rẻ, local):     │
  │     • heuristic tệ  → đẩy lại hàng đợi dịch lại LUÔN (sửa sớm)     │
  │     • heuristic ổn  → gom vào buffer chờ judge                     │
  └───────────────────────────────────────────────────────────────────┘
  ┌─ 1 judge worker ─────────────────────────────────────────────────┐
  │  gom đủ judge_batch_size (2–3) chunk "sơ bộ ổn" → chấm 1 LƯỢT     │
  │  (judge KHÁC model dịch, backend do caller chọn). MQM thấp →       │
  │  đẩy lại hàng đợi dịch lại. MQM đạt → chốt.                        │
  └───────────────────────────────────────────────────────────────────┘

Lặp đến khi mọi chunk đạt ngưỡng, hoặc cạn `max_attempts` → giữ BEST-SO-FAR
(vì web AI phi xác định: dịch lại có thể tệ hơn, không bao giờ để thụt lùi).

Module THUẦN async — KHÔNG import Playwright/httpx. Mọi tác vụ đời thực được
tiêm qua callable, nên test được mà không cần browser; coordinator nối callable
thật ở tầng trên (translate_chunk, quality heuristic, llm_judge/web_judge).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


# ── Hợp đồng callable (caller tiêm vào) ───────────────────────────────────────
# translate_fn(index) -> (text, ok): (dịch lại) 1 chunk → bản dịch + cờ thành công
TranslateFn = Callable[[int], Awaitable[tuple[str, bool]]]
# heuristic_fn(index, text) -> điểm 0..100 (local, không gọi AI)
HeuristicFn = Callable[[int, str], float]
# source_fn(index) -> text nguồn (EN) để judge so sánh
SourceFn = Callable[[int], str]
# judge_fn(batch) -> {index: mqm}; batch = list[(index, source, translation)]
#   → caller tự gộp 2–3 đoạn vào 1 prompt cho tiết kiệm token nếu muốn.
JudgeFn = Callable[[list[tuple[int, str, str]]], Awaitable[dict[int, Optional[float]]]]
ProgressFn = Callable[[int, str, str, float, Any], None]


@dataclass
class EvalConfig:
    """Tham số vòng lặp — đều là hyperparameter để báo cáo trong luận văn."""
    heuristic_threshold: float = 60.0   # < ngưỡng → dịch lại ngay (gate rẻ)
    judge_threshold: float = 70.0       # MQM < ngưỡng → dịch lại (gate đắt)
    judge_batch_size: int = 3           # gom 2–3 chunk / lượt judge (token-efficient)
    max_attempts: int = 3               # trần số lần dịch / chunk (chống loop vô hạn)
    num_workers: int = 2                # số translate worker song song
    judge_enabled: bool = True          # tắt → chỉ chạy gate heuristic


@dataclass
class _ChunkState:
    index: int
    text: str = ""                      # bản dịch của lần thử gần nhất
    heuristic: float = 0.0
    mqm: Optional[float] = None
    attempts: int = 0
    status: str = "pending"             # pending | buffered | passed | flagged
    best_text: str = ""                 # best-so-far (điểm cao nhất từng thấy)
    best_score: float = -1.0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "attempts": self.attempts,
            "status": self.status,
            "heuristic": round(self.heuristic, 1),
            "mqm": round(self.mqm, 1) if self.mqm is not None else None,
            "best_score": round(self.best_score, 1),
        }


@dataclass
class EvalReport:
    chunks: dict = field(default_factory=dict)
    rounds: list = field(default_factory=list)   # nhật ký từng quyết định pass/retry/flag
    passed: list = field(default_factory=list)
    flagged: list = field(default_factory=list)
    total_translations: int = 0
    total_judge_calls: int = 0
    duration_seconds: float = 0.0
    cancelled: bool = False

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "flagged": self.flagged,
            "total_translations": self.total_translations,
            "total_judge_calls": self.total_judge_calls,
            "duration_seconds": round(self.duration_seconds, 2),
            "cancelled": self.cancelled,
            "chunks": {str(i): c for i, c in self.chunks.items()},
            "rounds": self.rounds,
        }


class EvalPipeline:
    """Điều phối dịch ∥ đánh giá đồng thời cho 1 tập chunk index."""

    def __init__(
        self,
        indices: list[int],
        translate_fn: TranslateFn,
        heuristic_fn: HeuristicFn,
        source_fn: SourceFn,
        judge_fn: Optional[JudgeFn] = None,
        config: Optional[EvalConfig] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        progress_fn: Optional[ProgressFn] = None,
    ):
        self.cfg = config or EvalConfig()
        self._translate = translate_fn
        self._heuristic = heuristic_fn
        self._source = source_fn
        self._judge = judge_fn
        self._is_cancelled = is_cancelled or (lambda: False)
        self._progress_fn = progress_fn
        # judge tắt nếu không có judge_fn
        self._judge_on = self.cfg.judge_enabled and judge_fn is not None

        # Shared state — chỉ chạm dưới self._cond
        self._cond = asyncio.Condition()
        self._tq: list[int] = list(indices)       # hàng đợi (dịch lại)
        self._buffer: list[int] = []              # chunk sơ bộ ổn, chờ judge
        self._pending: set[int] = set(indices)    # chưa chốt
        self._inflight = 0                         # số chunk đang dịch dở
        self._states: dict[int, _ChunkState] = {i: _ChunkState(i) for i in indices}
        self._rounds: list[dict] = []
        self._total_tx = 0
        self._total_judge = 0
        self._start = 0.0

    # ── API ──────────────────────────────────────────────────────────────────

    async def run(self) -> EvalReport:
        self._start = time.time()
        if not self._states:
            return EvalReport(duration_seconds=0.0)

        tasks = [
            asyncio.create_task(self._translate_worker(w))
            for w in range(max(1, self.cfg.num_workers))
        ]
        if self._judge_on:
            tasks.append(asyncio.create_task(self._judge_worker()))

        await asyncio.gather(*tasks, return_exceptions=True)
        return self._build_report()

    def final_translations(self) -> dict[int, str]:
        """Bản dịch tốt nhất (best-so-far) cho mỗi chunk."""
        return {
            i: (s.best_text or s.text) for i, s in self._states.items()
        }

    # ── Translate worker ─────────────────────────────────────────────────────

    async def _translate_worker(self, wid: int):
        while True:
            idx: Optional[int] = None
            async with self._cond:
                while True:
                    if self._is_cancelled() or not self._pending:
                        return
                    if self._tq:
                        idx = self._tq.pop(0)
                        self._inflight += 1
                        break
                    await self._cond.wait()   # chờ requeue hoặc kết thúc

            text, ok = await self._safe_translate(idx)

            async with self._cond:
                self._inflight -= 1
                if self._is_cancelled():
                    self._cond.notify_all()
                    return
                st = self._states[idx]
                st.attempts += 1
                self._total_tx += 1
                if not ok or not text:
                    self._after_failed_translation(idx)
                else:
                    st.text = text
                    h = self._safe_heuristic(idx, text)
                    st.heuristic = h
                    self._update_best(idx, text, h)
                    self._route_after_heuristic(idx, h)
                self._cond.notify_all()

    # ── Judge worker (1 cái, gom batch) ──────────────────────────────────────

    async def _judge_worker(self):
        while True:
            batch_idx: list[int] = []
            async with self._cond:
                while True:
                    if self._is_cancelled() or not self._pending:
                        return
                    ready = len(self._buffer) >= self.cfg.judge_batch_size
                    tx_done = (not self._tq) and self._inflight == 0
                    if self._buffer and (ready or tx_done):
                        n = self.cfg.judge_batch_size
                        batch_idx = self._buffer[:n]
                        del self._buffer[:n]
                        break
                    await self._cond.wait()
                batch = [
                    (i, self._safe_source(i), self._states[i].text)
                    for i in batch_idx
                ]

            scores = await self._safe_judge(batch)

            async with self._cond:
                self._total_judge += 1
                for i in batch_idx:
                    self._route_after_judge(i, scores.get(i))
                self._cond.notify_all()

    # ── Routing (gọi DƯỚI lock) ──────────────────────────────────────────────

    def _route_after_heuristic(self, idx: int, h: float):
        st = self._states[idx]
        if h >= self.cfg.heuristic_threshold:
            if self._judge_on:
                st.status = "buffered"
                self._buffer.append(idx)
                self._log(idx, "heuristic", h, "to_judge")
            else:
                self._finalize(idx, "passed", "heuristic", h)
        elif st.attempts < self.cfg.max_attempts:
            self._log(idx, "heuristic", h, "retry")
            self._tq.append(idx)                 # sửa lại NGAY
        else:
            self._finalize(idx, "flagged", "heuristic", h)

    def _route_after_judge(self, idx: int, mqm: Optional[float]):
        st = self._states[idx]
        if mqm is None:
            # judge lỗi → không loop vô hạn vì lỗi hạ tầng; chấp nhận theo heuristic
            self._finalize(idx, "passed", "judge", st.heuristic)
            return
        st.mqm = mqm
        self._update_best(idx, st.text, mqm)
        if mqm >= self.cfg.judge_threshold:
            self._finalize(idx, "passed", "judge", mqm)
        elif st.attempts < self.cfg.max_attempts:
            self._log(idx, "judge", mqm, "retry")
            self._tq.append(idx)
        else:
            self._finalize(idx, "flagged", "judge", mqm)

    def _after_failed_translation(self, idx: int):
        st = self._states[idx]
        if st.attempts < self.cfg.max_attempts:
            self._log(idx, "translate", 0.0, "retry")
            self._tq.append(idx)
        else:
            self._finalize(idx, "flagged", "translate", 0.0)

    def _finalize(self, idx: int, status: str, stage: str, score: float):
        self._states[idx].status = status
        self._pending.discard(idx)
        self._log(idx, stage, score, status)
        if self._progress_fn:
            try:
                self._progress_fn(idx, status, stage, score, self._states[idx])
            except Exception:
                pass

    def _update_best(self, idx: int, text: str, score: float):
        st = self._states[idx]
        if score > st.best_score:
            st.best_score = score
            st.best_text = text

    def _log(self, idx: int, stage: str, score: Optional[float], decision: str):
        self._rounds.append({
            "index": idx,
            "attempt": self._states[idx].attempts,
            "stage": stage,
            "score": round(score, 1) if score is not None else None,
            "decision": decision,
            "t": round(time.time() - self._start, 3),
        })

    # ── Safe wrappers ────────────────────────────────────────────────────────

    async def _safe_translate(self, idx: int) -> tuple[str, bool]:
        try:
            return await self._translate(idx)
        except Exception:
            return "", False

    def _safe_heuristic(self, idx: int, text: str) -> float:
        try:
            return float(self._heuristic(idx, text))
        except Exception:
            return 0.0

    def _safe_source(self, idx: int) -> str:
        try:
            return self._source(idx)
        except Exception:
            return ""

    async def _safe_judge(
        self, batch: list[tuple[int, str, str]]
    ) -> dict[int, Optional[float]]:
        try:
            return await self._judge(batch)
        except Exception:
            return {i: None for i, _, _ in batch}

    # ── Report ───────────────────────────────────────────────────────────────

    def _build_report(self) -> EvalReport:
        return EvalReport(
            chunks={i: s.to_dict() for i, s in self._states.items()},
            rounds=self._rounds,
            passed=sorted(i for i, s in self._states.items() if s.status == "passed"),
            flagged=sorted(i for i, s in self._states.items() if s.status == "flagged"),
            total_translations=self._total_tx,
            total_judge_calls=self._total_judge,
            duration_seconds=time.time() - self._start,
            cancelled=self._is_cancelled(),
        )
