"""Gemini-as-Judge — MQM-style translation review via Gemini web (Playwright).

Reuses the same Playwright/Gemini infrastructure as the translator pipeline,
so it works with the user's existing logged-in Gemini Pro session and
incurs no API cost.

⚠ CAVEAT — self-judging bias:
This judge uses the *same* Gemini that produced the translation. There is a
known "self-favoring" bias where an LLM rates its own outputs ~5-10% higher
than a 3rd-party model would. Mitigations applied here:

  1. Critic-mode prompt: model is told its job is to FIND faults, not summarise.
  2. MQM error categorisation forces structured fault-finding (vs vague rating).
  3. Recommend cross-checking against `llm_judge.py` (Ollama) for high-stakes
     calls — disagreement ≥ 15 points → manual review.

Use this judge for:
  ✓ Strongest available judge model (Gemini Pro >> any 32B local)
  ✓ Low setup cost — already have the Playwright session
  ✓ Vietnamese fluency far better than Qwen 7B-32B at calling out
    awkward / unnatural phrasing
  ✗ NOT as the sole quality oracle — combine with Ollama judge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from app.pdf.llm_judge import MQM_CATEGORIES, _compute_mqm_score, _parse_json_response
from app.services.translator import WebAITranslator
from app.audit import log_event

logger = logging.getLogger(__name__)


def _build_judge_prompt(source: str, translation: str) -> str:
    """Build a strict MQM-style critic prompt for Gemini.

    The phrasing leans hard on fault-finding to counter Gemini's tendency
    to rate its own output charitably.
    """
    return (
        "Bạn là CHUYÊN GIA HIỆU ĐÍNH bản dịch học thuật Anh-Việt — vai trò "
        "của bạn là TÌM LỖI, không phải khen ngợi. Một bản dịch không hoàn hảo "
        "luôn có ít nhất vài lỗi nhỏ; nếu bạn không tìm thấy lỗi nào, "
        "hãy đọc lại kỹ hơn.\n\n"
        "Đánh giá bản dịch sau theo khung MQM (Multidimensional Quality "
        "Metrics) một cách NGHIÊM KHẮC.\n\n"
        f"=== VĂN BẢN GỐC (EN) ===\n{source}\n\n"
        f"=== BẢN DỊCH (VI) ===\n{translation}\n\n"
        "=== KHUNG MQM ===\n"
        "Categories: accuracy (sai nghĩa, mất thông tin, dịch sót) | "
        "fluency (văn phong gượng, sai ngữ pháp tiếng Việt) | "
        "terminology (sai thuật ngữ chuyên ngành) | "
        "style (không phù hợp văn học thuật) | "
        "locale (định dạng số, dấu câu, viết hoa).\n"
        "Severity: minor (gây khó chịu nhẹ) | major (gây hiểu sai một phần) | "
        "critical (gây hiểu sai hoàn toàn / mất thông tin trọng yếu).\n\n"
        "=== YÊU CẦU OUTPUT ===\n"
        "Trả về CHỈ JSON (KHÔNG markdown, KHÔNG giải thích trước/sau JSON), "
        "đúng cấu trúc:\n"
        "```json\n"
        "{\n"
        '  "score": <0-100, 100 = không lỗi>,\n'
        '  "verdict": "<good|acceptable|poor>",\n'
        '  "errors": [\n'
        "    {\n"
        '      "category": "<accuracy|fluency|terminology|style|locale>",\n'
        '      "severity": "<minor|major|critical>",\n'
        '      "source_span": "<đoạn EN bị lỗi (≤80 ký tự), null nếu không xác định>",\n'
        '      "translation_span": "<đoạn VI bị lỗi (≤80 ký tự), null nếu không xác định>",\n'
        '      "description": "<lỗi cụ thể, 1 câu tiếng Việt>"\n'
        "    }\n"
        "  ],\n"
        '  "strengths": "<1 câu, hoặc null nếu không có>",\n'
        '  "suggestion": "<gợi ý sửa cụ thể, hoặc null>"\n'
        "}\n"
        "```\n\n"
        "Quy tắc CHẤM:\n"
        "- score = 100 nếu errors rỗng (cực hiếm — phải thật sự không tìm ra lỗi).\n"
        "- score = 70-90 nếu chỉ có minor errors.\n"
        "- score = 40-70 nếu có ≥1 major error.\n"
        "- score < 40 nếu có critical error hoặc bản dịch sai nghĩa căn bản.\n"
        "- KHÔNG đưa lỗi giả để làm đầy errors — chỉ liệt kê lỗi thật."
    )


def _extract_json_from_gemini_response(raw: str) -> dict | None:
    """Gemini wraps JSON in markdown code blocks more aggressively than Ollama.

    Try multiple extraction strategies before giving up.
    """
    if not raw:
        return None

    # Strategy 1: ```json ... ``` block (most common from Gemini)
    m = re.search(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # Strategy 2: ``` ... ``` block (no language tag)
    m = re.search(r"```\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # Strategy 3: shared parser handles bare {...} fallback
    return _parse_json_response(raw)


async def judge_segment(
    translator: WebAITranslator,
    page,
    source: str,
    translation: str,
) -> dict | None:
    """Judge a single source/translation pair via Gemini web.

    Caller is responsible for opening the browser/page (so multiple judge
    calls can share one session). Returns same dict shape as
    `llm_judge.judge_segment` so the UI renders both judges identically.
    """
    prompt = _build_judge_prompt(source, translation)
    started_at = time.time()
    try:
        raw = await translator._send_prompt_and_get_response(page, prompt)
    except Exception as e:
        logger.warning(f"[GeminiJudge] Send/scrape failed: {e}")
        log_event("judge.gemini_send_failed",
                  error_type=type(e).__name__, error=str(e)[:200],
                  latency_seconds=round(time.time() - started_at, 3))
        return None

    parsed = _extract_json_from_gemini_response(raw or "")
    if parsed is None:
        logger.warning(
            "[GeminiJudge] Could not parse JSON; first 200 chars: %s",
            (raw or "")[:200],
        )
        log_event("judge.gemini_parse_failed",
                  raw_preview=(raw or "")[:200],
                  latency_seconds=round(time.time() - started_at, 3))
        return None

    # Recompute MQM score from errors so Gemini's self-score doesn't dominate.
    # Authoritative score = penalty-derived; keep self-score for diagnostics.
    errors = parsed.get("errors") or []
    mqm = _compute_mqm_score(errors)
    parsed["llm_self_score"] = parsed.get("score")
    parsed["score"] = round(mqm, 1)
    parsed["mqm_score"] = round(mqm, 1)
    parsed["model"] = "gemini-web"
    log_event("judge.gemini_segment_done",
              mqm_score=parsed["mqm_score"],
              llm_self_score=parsed.get("llm_self_score"),
              error_count=len(errors),
              latency_seconds=round(time.time() - started_at, 3))
    return parsed


async def judge_segments_batch(
    pairs: list[dict],
    max_segments: int = 10,
    low_score_threshold: float = 70.0,
    new_session_every: int = 5,
) -> list[dict]:
    """Run Gemini judge over a batch of segments.

    Opens one browser session, reuses it across calls. Starts a new chat
    every `new_session_every` segments to avoid context bloat (Gemini
    starts to drift / refuse after ~10 long turns).

    pairs: list of {"src": str, "mt": str, "index": int, "score_pct": float}
    Returns: list of {"index", "src", "mt", "score_pct", "llm_result"}.
    """
    # Same prioritisation as llm_judge: low-quality first.
    low_quality = [p for p in pairs if p.get("score_pct", 100) < low_score_threshold]
    pool = low_quality if low_quality else pairs
    sorted_pairs = sorted(pool, key=lambda p: p.get("score_pct", 100))
    to_judge = sorted_pairs[:max_segments]

    if not to_judge:
        log_event("judge.gemini_batch_skipped", reason="empty_pool",
                  total_pairs=len(pairs))
        return []

    translator = WebAITranslator()
    results: list[dict] = []
    context = page = None
    started_at = time.time()
    log_event("judge.gemini_batch_started",
              total_pairs=len(pairs),
              low_quality_count=len(low_quality),
              max_segments=max_segments,
              low_score_threshold=low_score_threshold,
              new_session_every=new_session_every,
              selected_count=len(to_judge))

    try:
        context, page = await translator.launch_browser()
        await translator.start_new_chat(page)

        for i, p in enumerate(to_judge):
            # Periodic session refresh — keeps each judge call independent
            # and avoids Gemini coupling segment N's verdict to N-1.
            if i > 0 and i % new_session_every == 0:
                logger.info(f"[GeminiJudge] Refreshing session at segment {i}")
                log_event("judge.gemini_session_refresh", at_segment=i)
                try:
                    await translator.start_new_chat(page)
                except Exception as e:
                    logger.warning(f"[GeminiJudge] Could not refresh session: {e}")
                    log_event("judge.gemini_session_refresh_failed",
                              at_segment=i, error=str(e)[:200])

            logger.info(
                f"[GeminiJudge] Judging segment {p.get('index')} "
                f"(heuristic score={p.get('score_pct')}%) ..."
            )
            try:
                result = await judge_segment(translator, page, p["src"], p["mt"])
            except Exception as e:
                logger.error(f"[GeminiJudge] Judge call failed for segment {p.get('index')}: {e}")
                log_event("judge.gemini_segment_failed",
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
            # Polite pause — Gemini sometimes throttles on rapid-fire turns
            await asyncio.sleep(1.5)

    finally:
        try:
            await translator.cleanup()
        except Exception as e:
            logger.warning(f"[GeminiJudge] cleanup error: {e}")
        success = sum(1 for r in results if r.get("llm_result") is not None)
        log_event("judge.gemini_batch_done",
                  evaluated=len(results), succeeded=success,
                  failed=len(results) - success,
                  latency_seconds=round(time.time() - started_at, 3))

    return results
