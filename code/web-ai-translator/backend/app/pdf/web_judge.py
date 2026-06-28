"""Cross-model web judge — MQM evaluation by a web AI DIFFERENT from the translator.

Motivation: `gemini_judge` has self-judging bias (same Gemini translates AND grades,
rating its own output ~5-10% higher). This module drives a DIFFERENT free web AI
(ChatGPT / DeepSeek) via Playwright to grade the translation — an independent
cross-model signal WITHOUT requiring Ollama.

The judge backend is ALWAYS forced to differ from the translation backend
(`pick_judge_backend`). Reuses the MQM prompt + scoring from `gemini_judge` /
`llm_judge` so the frontend renders all judges identically.

⚠ Vẫn là LLM-as-judge: dùng để soi định tính + cross-check, không phải metric
hiệu chuẩn. Kết hợp với ChrF++ (reference-based) cho điểm số luận văn.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import settings
from app.pdf.llm_judge import _compute_mqm_score
from app.pdf.gemini_judge import (
    _build_judge_prompt,
    _extract_json_from_gemini_response,
)
from app.services.translator import WebAITranslator, _BACKENDS
from app.audit import log_event

logger = logging.getLogger(__name__)

# Backend names hợp lệ (đồng bộ với translator._BACKENDS).
KNOWN_BACKENDS = tuple(_BACKENDS.keys())

# Thứ tự ưu tiên khi tự chọn judge ≠ translator: ưu tiên backend ổn định trước,
# các backend mới làm fallback phía sau.
_JUDGE_PRIORITY = (
    "chatgpt", "deepseek", "aistudio", "grok", "copilot", "gemini",
)


def pick_judge_backend(
    translator_backend: str | None,
    preferred: str | None = None,
) -> str:
    """Chọn backend judge KHÁC backend đã dịch (tránh self-judging bias).

    - `preferred` hợp lệ và khác translator → dùng nó.
    - `preferred` trùng translator (hoặc None/không hợp lệ) → tự chọn theo
      `_JUDGE_PRIORITY`, bỏ qua backend trùng translator.
    """
    tb = (translator_backend or settings.AI_BACKEND or "gemini").lower()
    if preferred:
        pb = preferred.lower()
        if pb in KNOWN_BACKENDS and pb != tb:
            return pb
        logger.info(
            "[WebJudge] preferred=%s không hợp lệ hoặc trùng translator=%s → auto-pick",
            pb, tb,
        )
    for c in _JUDGE_PRIORITY:
        if c != tb and c in KNOWN_BACKENDS:
            return c
    return "chatgpt"


async def judge_segment(
    translator: WebAITranslator,
    page,
    source: str,
    translation: str,
    model_label: str,
) -> dict | None:
    """Judge one (source, translation) pair via the judge's web session.

    Same dict shape as `llm_judge.judge_segment` / `gemini_judge.judge_segment`
    so the UI renders every judge identically. Authoritative score = penalty-
    derived MQM (not the model's self-score) to dampen self-favoring bias.
    """
    prompt = _build_judge_prompt(source, translation)
    started_at = time.time()
    try:
        raw = await translator._send_prompt_and_get_response(page, prompt)
    except Exception as e:
        logger.warning(f"[WebJudge] Send/scrape failed: {e}")
        log_event("judge.web_send_failed",
                  model_label=model_label,
                  error_type=type(e).__name__, error=str(e)[:200],
                  latency_seconds=round(time.time() - started_at, 3))
        return None

    parsed = _extract_json_from_gemini_response(raw or "")
    if parsed is None:
        logger.warning(
            "[WebJudge] Could not parse JSON; first 200 chars: %s",
            (raw or "")[:200],
        )
        log_event("judge.web_parse_failed",
                  model_label=model_label,
                  raw_preview=(raw or "")[:200],
                  latency_seconds=round(time.time() - started_at, 3))
        return None

    errors = parsed.get("errors") or []
    mqm = _compute_mqm_score(errors)
    parsed["llm_self_score"] = parsed.get("score")
    parsed["score"] = round(mqm, 1)
    parsed["mqm_score"] = round(mqm, 1)
    parsed["model"] = model_label
    log_event("judge.web_segment_done",
              model_label=model_label,
              mqm_score=parsed["mqm_score"],
              llm_self_score=parsed.get("llm_self_score"),
              error_count=len(errors),
              latency_seconds=round(time.time() - started_at, 3))
    return parsed


async def judge_segments_batch(
    pairs: list[dict],
    judge_backend: str | None = None,
    translator_backend: str | None = None,
    max_segments: int = 10,
    low_score_threshold: float = 70.0,
    new_session_every: int = 5,
) -> dict:
    """Run a cross-model web judge over a batch of segments.

    pairs: list of {"src", "mt", "index", "score_pct"}.
    Returns a report dict: {judge_backend, translator_backend, model,
    num_judged, avg_score, error_counts, results}.
    """
    tb = (translator_backend or settings.AI_BACKEND or "gemini").lower()
    jb = pick_judge_backend(tb, judge_backend)
    model_label = f"{jb}-web"

    report: dict = {
        "judge_backend": jb,
        "translator_backend": tb,
        "model": model_label,
        "num_judged": 0,
        "avg_score": None,
        "error_counts": {},
        "results": [],
    }

    # Low-quality first (same prioritisation as the other judges).
    low_quality = [p for p in pairs if p.get("score_pct", 100) < low_score_threshold]
    pool = low_quality if low_quality else pairs
    to_judge = sorted(pool, key=lambda p: p.get("score_pct", 100))[:max_segments]
    if not to_judge:
        log_event("judge.web_batch_skipped",
                  judge_backend=jb, translator_backend=tb,
                  reason="empty_pool", total_pairs=len(pairs))
        return report

    translator = WebAITranslator(backend=jb)
    results: list[dict] = []
    context = page = None
    started_at = time.time()
    log_event("judge.web_batch_started",
              judge_backend=jb, translator_backend=tb,
              model_label=model_label,
              total_pairs=len(pairs),
              low_quality_count=len(low_quality),
              max_segments=max_segments,
              low_score_threshold=low_score_threshold,
              new_session_every=new_session_every,
              selected_count=len(to_judge))

    try:
        context, page = await translator.launch_browser()

        for i, p in enumerate(to_judge):
            # Periodic session refresh — keeps each verdict independent.
            if i > 0 and i % new_session_every == 0:
                logger.info(f"[WebJudge] Refreshing {jb} session at segment {i}")
                log_event("judge.web_session_refresh",
                          judge_backend=jb, at_segment=i)
                try:
                    await translator.start_new_chat(page)
                except Exception as e:
                    logger.warning(f"[WebJudge] Could not refresh session: {e}")
                    log_event("judge.web_session_refresh_failed",
                              judge_backend=jb, at_segment=i,
                              error=str(e)[:200])

            logger.info(
                f"[WebJudge] {jb} judging segment {p.get('index')} "
                f"(heuristic score={p.get('score_pct')}%) ..."
            )
            try:
                result = await judge_segment(
                    translator, page, p["src"], p["mt"], model_label,
                )
            except Exception as e:
                logger.error(
                    f"[WebJudge] Judge call failed for segment {p.get('index')}: {e}"
                )
                log_event("judge.web_segment_failed",
                          judge_backend=jb,
                          segment_index=p.get("index"),
                          error_type=type(e).__name__, error=str(e)[:200])
                result = None

            results.append({
                "index": p.get("index"),
                "src": p.get("src"),
                "mt": p.get("mt"),
                "score_pct": p.get("score_pct"),
                "llm_result": result,
            })
            await asyncio.sleep(1.5)

    finally:
        try:
            await translator.cleanup()
        except Exception as e:
            logger.warning(f"[WebJudge] cleanup error: {e}")

    judged = [r for r in results if r.get("llm_result")]
    avg_score = (
        round(sum(r["llm_result"].get("mqm_score", r["llm_result"]["score"])
                  for r in judged) / len(judged))
        if judged else None
    )
    error_counts: dict[str, int] = {}
    for r in judged:
        for e in (r["llm_result"].get("errors") or []):
            cat = e.get("category", "other")
            error_counts[cat] = error_counts.get(cat, 0) + 1

    report.update({
        "num_judged": len(judged),
        "avg_score": avg_score,
        "error_counts": error_counts,
        "results": results,
    })
    log_event("judge.web_batch_done",
              judge_backend=jb, translator_backend=tb,
              evaluated=len(results), succeeded=len(judged),
              failed=len(results) - len(judged),
              avg_score=avg_score,
              error_categories=list(error_counts.keys()),
              latency_seconds=round(time.time() - started_at, 3))
    return report
