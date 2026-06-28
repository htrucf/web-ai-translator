"""JudgeAgent — chấm chất lượng dịch bằng web AI hoặc COMETKiwi.

Agent này là "người phản biện" độc lập trong kiến trúc multi-agent:
  - web/<vendor>: dùng một web AI khác model dịch để giảm thiên lệch tự chấm.
  - cometkiwi/wmt22-cometkiwi-da: dùng QE model COMETKiwi không cần reference.

Ollama không nằm trong JudgeAgent. Nếu cần đường Ollama cũ, dùng module
`app.pdf.llm_judge` riêng; phần Ollama/VLM không bị buộc vào kiến trúc agent.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.eval_adapters import (
    build_batch_judge_prompt,
    parse_batch_judge_response,
)
from app.pdf.processor import chunk_to_text


DEFAULT_COMETKIWI_MODEL = "Unbabel/wmt22-cometkiwi-da"
COMETKIWI_XL_MODEL = "Unbabel/wmt23-cometkiwi-da-xl"
_COMET_MODELS: dict[str, object] = {}

# wmt22 (≈565M) — bản gốc, nhẹ.
_COMET_BACKENDS = {
    "cometkiwi",
    "comet-kiwi",
    "wmt22-cometkiwi",
    "wmt22-cometkiwi-da",
    "unbabel/wmt22-cometkiwi-da",
}
# wmt23-xl (≈3.5B) — bản XL, chấm tốt hơn, nặng hơn; reference-free, KHÔNG gated.
_COMET_XL_BACKENDS = {
    "cometkiwi-xl",
    "comet-kiwi-xl",
    "wmt23-cometkiwi",
    "wmt23-cometkiwi-da-xl",
    "unbabel/wmt23-cometkiwi-da-xl",
}

# Map backend chuẩn hóa → model id để tải/chấm.
_COMET_FAMILY = {"cometkiwi", "cometkiwi-xl"}
_COMET_MODEL_BY_BACKEND = {
    "cometkiwi": DEFAULT_COMETKIWI_MODEL,
    "cometkiwi-xl": COMETKIWI_XL_MODEL,
}


def normalize_judge_backend(judge_backend: str | None) -> str | None:
    """Chuẩn hóa tên backend JudgeAgent.

    None/""/"off" tắt judge. "ollama" bị loại khỏi JudgeAgent theo chủ đích;
    đường Ollama cũ vẫn nằm ở module `app.pdf.llm_judge`.
    """
    if judge_backend is None:
        return None
    jb = judge_backend.strip().lower()
    if jb in ("", "off", "none", "false"):
        return None
    if jb == "ollama":
        raise ValueError(
            "JudgeAgent không chạy Ollama; dùng module app.pdf.llm_judge nếu cần."
        )
    if jb in _COMET_XL_BACKENDS:
        return "cometkiwi-xl"
    if jb in _COMET_BACKENDS:
        return "cometkiwi"
    return jb


def comet_model_for_backend(judge_backend: str | None) -> str:
    """Backend (vd 'cometkiwi-xl') → HuggingFace model id để tải/nạp."""
    try:
        canon = normalize_judge_backend(judge_backend)
    except ValueError:
        canon = None
    return _COMET_MODEL_BY_BACKEND.get(canon or "", DEFAULT_COMETKIWI_MODEL)


class JudgeAgent(BaseAgent):
    name = "JudgeAgent"

    def __init__(
        self,
        judge_backend: str | None = "web",
        max_segments: int = 10,
        low_score_threshold: float = 70.0,
        comet_model: str | None = None,
        comet_batch_size: int = 8,
    ):
        self.judge_backend = judge_backend
        self.max_segments = max_segments
        self.low_score_threshold = low_score_threshold
        # comet_model derive theo backend (wmt22 vs wmt23-xl) nếu caller không ép.
        self.comet_model = comet_model or comet_model_for_backend(judge_backend)
        self.comet_batch_size = comet_batch_size
        self._web_translator = None
        self._web_page = None
        self._web_lock = asyncio.Lock()
        self._resolved_web_backend: Optional[str] = None

    async def run(self, ctx: AgentContext) -> AgentResult:
        if ctx.is_cancelled():
            return AgentResult.fail("Cancelled", recoverable=True)

        try:
            backend = normalize_judge_backend(self.judge_backend)
        except ValueError as e:
            return AgentResult.fail(str(e), recoverable=True)
        if backend is None:
            return AgentResult.ok(data={"enabled": False}, judge_backend=None)

        pairs = self._build_pairs(ctx)
        if not pairs:
            return AgentResult.fail("No pairs to judge", recoverable=False)

        translator_backend = getattr(ctx.translator, "backend_name", "gemini")
        self.log(
            f"Judge: backend={backend}, translator={translator_backend}, "
            f"max_segments={self.max_segments}"
        )

        try:
            if backend in _COMET_FAMILY:
                report = await self._run_cometkiwi_report(pairs)
            else:
                report = await self._run_web_report(
                    pairs, translator_backend=translator_backend
                )
        except Exception as e:
            return AgentResult.fail(f"Judge failed: {e}", recoverable=True)

        ctx.progress["judge"] = report
        if report.get("kind") == "web":
            ctx.progress["web_judge"] = report
        elif report.get("kind") == "cometkiwi":
            ctx.progress["cometkiwi_judge"] = report
        ctx.save_progress()

        avg = report.get("avg_score")
        self.log(
            f"Judge {report.get('judge_backend', '?')} avg={avg}, "
            f"judged={report.get('num_judged', 0)}"
        )
        return AgentResult.ok(
            data=report,
            judge_backend=report.get("judge_backend"),
            num_judged=report.get("num_judged", 0),
            avg_score=avg,
        )

    async def judge_batch(
        self,
        batch: list[tuple[int, str, str]],
        *,
        translator_backend: str | None = None,
    ) -> dict[int, Optional[float]]:
        """Chấm batch cho EvalPipeline: trả {index: score 0..100 | None}."""
        backend = normalize_judge_backend(self.judge_backend)
        indices = [i for i, _, _ in batch]
        if backend is None:
            return {i: None for i in indices}
        if backend in _COMET_FAMILY:
            return await asyncio.to_thread(
                self._score_cometkiwi_batch, batch
            )
        return await self._judge_web_batch(
            batch, translator_backend=translator_backend
        )

    async def cleanup(self):
        if self._web_translator is not None:
            try:
                await self._web_translator.cleanup()
            except Exception:
                pass
        self._web_translator = None
        self._web_page = None
        self._resolved_web_backend = None

    async def _run_web_report(
        self, pairs: list[dict], *, translator_backend: str | None
    ) -> dict:
        from app.pdf import web_judge

        preferred = None if (self.judge_backend or "web").lower() == "web" else self.judge_backend
        report = await web_judge.judge_segments_batch(
            pairs,
            judge_backend=preferred,
            translator_backend=translator_backend,
            max_segments=self.max_segments,
            low_score_threshold=self.low_score_threshold,
        )
        report["kind"] = "web"
        return report

    async def _judge_web_batch(
        self,
        batch: list[tuple[int, str, str]],
        *,
        translator_backend: str | None,
    ) -> dict[int, Optional[float]]:
        from app.pdf.web_judge import pick_judge_backend
        from app.services.translator import WebAITranslator

        indices = [i for i, _, _ in batch]
        preferred = None if (self.judge_backend or "web").lower() == "web" else self.judge_backend
        resolved = pick_judge_backend(translator_backend, preferred)

        async with self._web_lock:
            if self._web_page is None or self._resolved_web_backend != resolved:
                await self.cleanup()
                tr = WebAITranslator(backend=resolved)
                _ctx, page = await tr.launch_browser()
                self._web_translator = tr
                self._web_page = page
                self._resolved_web_backend = resolved

            prompt = build_batch_judge_prompt(batch)
            raw = await self._web_translator._send_prompt_and_get_response(
                self._web_page, prompt
            )
        return parse_batch_judge_response(raw, indices)

    async def _run_cometkiwi_report(self, pairs: list[dict]) -> dict:
        selected = self._select_pairs(pairs)
        batch = [
            (int(p.get("index", i)), p.get("src", ""), p.get("mt", ""))
            for i, p in enumerate(selected)
        ]
        scores = await asyncio.to_thread(self._score_cometkiwi_batch, batch)

        results = []
        for p in selected:
            idx = int(p.get("index", -1))
            score = scores.get(idx)
            qe_result = None
            if score is not None:
                qe_result = {
                    "score": round(score, 1),
                    "mqm_score": round(score, 1),
                    "model": self.comet_model,
                    "backend": "cometkiwi",
                }
            results.append({
                "index": idx,
                "src": p.get("src"),
                "mt": p.get("mt"),
                "score_pct": p.get("score_pct"),
                "qe_result": qe_result,
            })

        judged = [r for r in results if r.get("qe_result")]
        avg_score = (
            round(sum(r["qe_result"]["score"] for r in judged) / len(judged))
            if judged else None
        )
        return {
            "kind": "cometkiwi",
            "judge_backend": "cometkiwi",
            "model": self.comet_model,
            "num_judged": len(judged),
            "avg_score": avg_score,
            "error_counts": {},
            "results": results,
        }

    def _score_cometkiwi_batch(
        self, batch: list[tuple[int, str, str]]
    ) -> dict[int, Optional[float]]:
        model = _load_comet_model(self.comet_model)
        data = [{"src": src or "", "mt": mt or ""} for _, src, mt in batch]
        kwargs = {"batch_size": self.comet_batch_size}
        kwargs["gpus"] = _detect_cuda_gpus()
        prediction = model.predict(data, **kwargs)
        raw_scores = _prediction_scores(prediction)

        out: dict[int, Optional[float]] = {}
        for (idx, _src, _mt), raw in zip(batch, raw_scores):
            out[idx] = _normalize_comet_score(raw)
        for idx, _src, _mt in batch:
            out.setdefault(idx, None)
        return out

    def _select_pairs(self, pairs: list[dict]) -> list[dict]:
        low_quality = [
            p for p in pairs
            if p.get("score_pct", 100) < self.low_score_threshold
        ]
        pool = low_quality if low_quality else pairs
        return sorted(pool, key=lambda p: p.get("score_pct", 100))[
            : self.max_segments
        ]

    def _build_pairs(self, ctx: AgentContext) -> list[dict]:
        final = ctx.progress.get("translated_chunks", {})
        pairs = []
        for idx in range(len(ctx.chunks)):
            sidx = str(idx)
            mt = final.get(sidx, "")
            if not mt:
                continue
            src = chunk_to_text(ctx.chunks[idx])
            if not src:
                continue
            pairs.append({
                "src": src,
                "mt": mt,
                "index": idx,
                "score_pct": 75.0,
            })
        return pairs


def _load_comet_model(model_name: str):
    if model_name in _COMET_MODELS:
        return _COMET_MODELS[model_name]
    try:
        from comet import download_model, load_from_checkpoint
    except ImportError as e:
        raise RuntimeError(
            "COMETKiwi cần gói `unbabel-comet`. Cài thêm rồi chạy lại: "
            "pip install unbabel-comet"
        ) from e

    checkpoint = download_model(model_name)
    model = load_from_checkpoint(checkpoint)
    _COMET_MODELS[model_name] = model
    return model


def _prediction_scores(prediction) -> list:
    scores = getattr(prediction, "scores", None)
    if scores is not None:
        return list(scores)
    if isinstance(prediction, dict) and "scores" in prediction:
        return list(prediction["scores"])
    if isinstance(prediction, (list, tuple)):
        return list(prediction)
    return []


def _normalize_comet_score(raw) -> Optional[float]:
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return None
    if score <= 1.0:
        score *= 100.0
    return max(0.0, min(100.0, score))


def _detect_cuda_gpus() -> int:
    try:
        import torch
        return 1 if torch.cuda.is_available() else 0
    except Exception:
        return 0
