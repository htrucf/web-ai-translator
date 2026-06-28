"""LLM-assisted translation review using Ollama (local, free).

Uses local LLMs via Ollama to evaluate translations with MQM-inspired
error categorisation. Intended as a qualitative review tool, NOT a
calibrated metric.

⚠ LIMITATION — MQM vs LLM-as-judge:
MQM (Multidimensional Quality Metrics) was designed for trained human
annotators with calibration sessions and inter-annotator agreement checks.
Using an LLM as a drop-in replacement introduces known biases:
  - Length bias: longer sentences are penalised unfairly
  - Position bias: higher scores for text appearing earlier
  - Self-inconsistency: same segment scores differently across runs
  - No calibration: no Vietnamese MQM gold standard exists to validate against

Use this module for:
  ✓ Spot-checking suspicious segments flagged by ChrF++ or heuristic QE
  ✓ Qualitative analysis / human-readable feedback
  ✗ NOT as a standalone quality score or thesis metric

In thesis: refer to this as "AI-assisted review" rather than "MQM evaluation."

Requirements:
  1. Install Ollama: https://ollama.com
  2. Pull a model (choose one):
       ollama pull qwen2.5:7b      # Best Vietnamese, recommended
       ollama pull gemma3:9b       # Google Gemma 3, strong multilingual
       ollama pull llama3.1:8b     # Meta Llama 3.1, good quality
       ollama pull mistral:7b      # Mistral 7B, fast
  3. Ollama runs automatically as a background service on Windows.

No API key needed. All local.
"""

import json
import logging
import os
import re
import time
import httpx

from app.audit import log_event

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = "qwen2.5:32b"  # Upgraded — Qwen2.5-32B is the new VI sweet spot

# Supported models with descriptions (in order of Vietnamese quality / size).
# Ranking based on VMLU (Vietnamese MMLU) leaderboard + SEACrowd evaluations
# as of 2025–2026. Qwen-family dominates the open-source VI tier because
# Alibaba's training corpus has heavier Asian-language coverage than Meta/
# Mistral/Microsoft equivalents.
SUPPORTED_MODELS = {
    # ── Tier 1 — Recommended for Judge (strong VI, fits commodity GPUs) ──
    "qwen2.5:32b": {
        "name": "Qwen 2.5 32B",
        "description": "★ Khuyến nghị — Qwen 2.5 32B, mạnh nhất cho tiếng Việt nhóm 30B",
        "pull_cmd": "ollama pull qwen2.5:32b",
        "size_gb": 19.9,
    },
    "qwen2.5:72b": {
        "name": "Qwen 2.5 72B",
        "description": "Chất lượng VI gần Gemini Pro, cần GPU 48GB+",
        "pull_cmd": "ollama pull qwen2.5:72b",
        "size_gb": 47.0,
    },
    "deepseek-r1:32b": {
        "name": "DeepSeek-R1 32B (Qwen distill)",
        "description": "Reasoning chain-of-thought mạnh, base Qwen → giải thích lỗi tốt",
        "pull_cmd": "ollama pull deepseek-r1:32b",
        "size_gb": 19.9,
    },
    "qwen3:32b": {
        "name": "Qwen 3 32B",
        "description": "Qwen thế hệ 3 (2025), multilingual cải thiện so với 2.5",
        "pull_cmd": "ollama pull qwen3:32b",
        "size_gb": 20.0,
    },
    # ── Tier 2 — Mid-size (mid-tier hardware) ──
    "qwen2.5:14b": {
        "name": "Qwen 2.5 14B",
        "description": "Cân bằng size/chất lượng VI, GPU 12GB chạy được",
        "pull_cmd": "ollama pull qwen2.5:14b",
        "size_gb": 9.0,
    },
    "deepseek-r1:14b": {
        "name": "DeepSeek-R1 14B (Qwen distill)",
        "description": "Reasoning + Qwen base, 14B size",
        "pull_cmd": "ollama pull deepseek-r1:14b",
        "size_gb": 9.0,
    },
    "phi4:14b": {
        "name": "Phi-4 14B",
        "description": "Microsoft Phi-4, mạnh về suy luận (VI trung bình)",
        "pull_cmd": "ollama pull phi4:14b",
        "size_gb": 9.1,
    },
    # ── Tier 3 — Light models (fallback / quick A/B) ──
    "qwen2.5:7b": {
        "name": "Qwen 2.5 7B",
        "description": "Nhẹ, VI tạm ổn — chỉ dùng khi không có hardware cho 14B+",
        "pull_cmd": "ollama pull qwen2.5:7b",
        "size_gb": 4.7,
    },
    "gemma3:9b": {
        "name": "Gemma 3 9B",
        "description": "Google Gemma 3, đa ngôn ngữ ổn",
        "pull_cmd": "ollama pull gemma3:9b",
        "size_gb": 5.8,
    },
    "gemma3:4b": {
        "name": "Gemma 3 4B",
        "description": "Google Gemma 3 nhỏ gọn, nhanh nhất",
        "pull_cmd": "ollama pull gemma3:4b",
        "size_gb": 3.0,
    },
    "llama3.1:8b": {
        "name": "Llama 3.1 8B",
        "description": "Meta Llama 3.1, VI yếu hơn Qwen cùng size",
        "pull_cmd": "ollama pull llama3.1:8b",
        "size_gb": 4.9,
    },
    "mistral:7b": {
        "name": "Mistral 7B",
        "description": "Nhanh, VI hạn chế",
        "pull_cmd": "ollama pull mistral:7b",
        "size_gb": 4.1,
    },
}

# MQM error categories and penalty weights
MQM_CATEGORIES = {
    "accuracy":    {"minor": 1,   "major": 5,  "critical": 25},
    "fluency":     {"minor": 0.1, "major": 1,  "critical": 5},
    "terminology": {"minor": 0.1, "major": 1,  "critical": 5},
    "style":       {"minor": 0.1, "major": 0.5,"critical": 2},
    "locale":      {"minor": 0.1, "major": 0.5,"critical": 2},
}


def is_available(model: str = DEFAULT_MODEL) -> bool:
    """Check if Ollama is running and model is available."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        if not r.is_success:
            return False
        models = [m["name"] for m in r.json().get("models", [])]
        # Exact match first
        if model in models:
            return True
        # Prefix match on base+tag to handle quantization suffixes
        # e.g. "qwen2.5:7b" matches "qwen2.5:7b-instruct-q4_K_M" but NOT "qwen2.5:14b"
        tag = model.split(":", 1)[1] if ":" in model else ""
        base = model.split(":")[0]
        prefix = f"{base}:{tag}" if tag else base
        return any(m.startswith(prefix) for m in models)
    except Exception:
        return False


def list_models() -> list[str]:
    """Return available Ollama models."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def list_available_models() -> list[dict]:
    """Return supported models annotated with availability status.

    Each entry: {id, name, description, available, pull_cmd, size_gb}
    """
    installed = list_models()

    result = []
    for model_id, info in SUPPORTED_MODELS.items():
        base = model_id.split(":")[0]
        available = any(m.startswith(base) for m in installed)
        # Find the exact installed name if available
        installed_name = next((m for m in installed if m.startswith(base)), None)
        result.append({
            "id": model_id,
            "installed_id": installed_name,
            "name": info["name"],
            "description": info["description"],
            "available": available,
            "pull_cmd": info["pull_cmd"],
            "size_gb": info["size_gb"],
        })

    # Also include any installed Ollama models not in our list
    known_bases = {mid.split(":")[0] for mid in SUPPORTED_MODELS}
    for m in installed:
        base = m.split(":")[0]
        if base not in known_bases:
            result.append({
                "id": m,
                "installed_id": m,
                "name": m,
                "description": "Installed Ollama model",
                "available": True,
                "pull_cmd": None,
                "size_gb": None,
            })

    return result


def _build_prompt(source: str, translation: str) -> str:
    return f"""Bạn là chuyên gia đánh giá chất lượng dịch thuật học thuật Anh-Việt.

Hãy đánh giá bản dịch sau theo khung MQM (Multidimensional Quality Metrics).

=== VĂN BẢN GỐC (Tiếng Anh) ===
{source}

=== BẢN DỊCH (Tiếng Việt) ===
{translation}

=== YÊU CẦU ===
Trả về JSON với cấu trúc sau (KHÔNG có bất kỳ text nào ngoài JSON):
{{
  "score": <số 0-100, 100 là hoàn hảo>,
  "verdict": "<good|acceptable|poor>",
  "errors": [
    {{
      "category": "<accuracy|fluency|terminology|style|locale>",
      "severity": "<minor|major|critical>",
      "source_span": "<đoạn gốc bị lỗi, hoặc null>",
      "translation_span": "<đoạn dịch bị lỗi, hoặc null>",
      "description": "<mô tả lỗi ngắn gọn bằng tiếng Việt>"
    }}
  ],
  "strengths": "<điểm tốt của bản dịch, 1-2 câu>",
  "suggestion": "<gợi ý cải thiện nếu có, hoặc null>"
}}"""


def _compute_mqm_score(errors: list[dict]) -> float:
    """Compute MQM penalty score (100 = perfect, lower = worse errors)."""
    penalty = 0.0
    for e in errors:
        cat = e.get("category", "accuracy")
        sev = e.get("severity", "minor")
        weights = MQM_CATEGORIES.get(cat, MQM_CATEGORIES["accuracy"])
        penalty += weights.get(sev, 1)
    return max(0.0, 100.0 - penalty)


def judge_segment(
    source: str,
    translation: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 180.0,
) -> dict | None:
    """Evaluate a single source/translation pair with LLM.

    Returns dict with score, verdict, errors, strengths, suggestion.
    Returns None if Ollama unavailable or request failed.
    """
    prompt = _build_prompt(source, translation)
    started_at = time.time()
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.1, "num_predict": 2048}},
            timeout=timeout,
        )
        if not r.is_success:
            logger.warning(f"[LLMJudge] Ollama error: {r.status_code}")
            log_event("judge.ollama_http_error",
                      model=model, status_code=r.status_code,
                      latency_seconds=round(time.time() - started_at, 3))
            return None

        raw = r.json().get("response", "")
        # Extract JSON from response
        result = _parse_json_response(raw)
        if result is None:
            logger.warning(f"[LLMJudge] Could not parse JSON: {raw[:200]}")
            log_event("judge.ollama_parse_failed",
                      model=model, raw_preview=raw[:200],
                      latency_seconds=round(time.time() - started_at, 3))
            return None

        # Recompute MQM score from errors as ground truth; override LLM self-score
        errors = result.get("errors", [])
        mqm_score = _compute_mqm_score(errors)
        result["llm_self_score"] = result.get("score")   # preserve original for reference
        result["score"] = round(mqm_score, 1)            # authoritative: penalty-computed
        result["mqm_score"] = round(mqm_score, 1)
        result["model"] = model
        log_event("judge.ollama_segment_done",
                  model=model, mqm_score=result["mqm_score"],
                  llm_self_score=result.get("llm_self_score"),
                  error_count=len(errors),
                  latency_seconds=round(time.time() - started_at, 3))
        return result

    except httpx.TimeoutException:
        logger.warning(f"[LLMJudge] Timeout after {timeout}s")
        log_event("judge.ollama_timeout",
                  model=model, timeout_seconds=timeout,
                  latency_seconds=round(time.time() - started_at, 3))
        return None
    except Exception as e:
        logger.error(f"[LLMJudge] Error: {e}")
        log_event("judge.ollama_error",
                  model=model, error_type=type(e).__name__,
                  error=str(e)[:200],
                  latency_seconds=round(time.time() - started_at, 3))
        return None


def judge_segments_batch(
    pairs: list[dict],
    model: str = DEFAULT_MODEL,
    max_segments: int = 20,
    low_score_threshold: float = 70.0,
) -> list[dict]:
    """Evaluate multiple segments. Prioritises low-scoring ones below threshold.

    pairs: list of {"src": str, "mt": str, "index": int, "score_pct": float}
    Returns list of {"index": int, "src": str, "mt": str, "score_pct": float, "llm_result": dict}
    """
    # Filter to low-quality segments first; fall back to all pairs if nothing qualifies
    low_quality = [p for p in pairs if p.get("score_pct", 100) < low_score_threshold]
    pool = low_quality if low_quality else pairs

    # Sort worst-first, then limit count
    sorted_pairs = sorted(pool, key=lambda p: p.get("score_pct", 100))
    to_judge = sorted_pairs[:max_segments]

    started_at = time.time()
    log_event("judge.ollama_batch_started",
              model=model, total_pairs=len(pairs),
              low_quality_count=len(low_quality),
              max_segments=max_segments,
              low_score_threshold=low_score_threshold,
              selected_count=len(to_judge))

    results = []
    for p in to_judge:
        logger.info(f"[LLMJudge] Judging segment {p.get('index')} (score={p.get('score_pct')}%) ...")
        result = judge_segment(p["src"], p["mt"], model=model)
        results.append({
            "index": p.get("index"),
            "src": p.get("src"),
            "mt": p.get("mt"),
            "score_pct": p.get("score_pct"),
            "llm_result": result,
        })

    success = sum(1 for r in results if r.get("llm_result") is not None)
    log_event("judge.ollama_batch_done",
              model=model, evaluated=len(results),
              succeeded=success, failed=len(results) - success,
              latency_seconds=round(time.time() - started_at, 3))
    return results


def _parse_json_response(text: str) -> dict | None:
    """Extract and parse JSON from LLM response."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    # Extract JSON block
    for pattern in [
        r"```json\s*(.*?)\s*```",
        r"```\s*(.*?)\s*```",
        r"(\{.*\})",
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue

    return None
