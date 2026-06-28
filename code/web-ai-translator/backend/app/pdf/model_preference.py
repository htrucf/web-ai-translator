"""Model preference helpers cho agentic PDF pipeline.

Nguyên tắc: user chọn thứ tự model nào thì pipeline ưu tiên đúng thứ tự đó.
Nếu user chỉ chọn 1 model, hệ thống có thể thêm fallback khẩn cấp phía sau để
job không chết cứng khi model đầu không mở/chạy được.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

from app.services.translator import _BACKENDS
from app.user_paths import safe_username


DEFAULT_MODEL_PREFERENCE = ["gemini", "chatgpt"]
KNOWN_TRANSLATOR_MODELS = tuple(_BACKENDS.keys())


def parse_model_preference(value: Any) -> list[str]:
    """Parse preference từ JSON list, CSV string, hoặc list thô."""
    if value is None:
        return DEFAULT_MODEL_PREFERENCE[:]
    raw = value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return DEFAULT_MODEL_PREFERENCE[:]
        try:
            raw = json.loads(s)
        except Exception:
            raw = re.split(r"[,;\s]+", s)
    if not isinstance(raw, (list, tuple)):
        return DEFAULT_MODEL_PREFERENCE[:]
    return normalize_model_preference(raw)


def normalize_model_preference(models: list[Any]) -> list[str]:
    """Giữ đúng thứ tự user chọn, bỏ model không hỗ trợ và trùng lặp."""
    out: list[str] = []
    for item in models:
        m = str(item or "").strip().lower()
        if not m or m not in KNOWN_TRANSLATOR_MODELS or m in out:
            continue
        out.append(m)
    return out or DEFAULT_MODEL_PREFERENCE[:]


def expand_model_execution_order(models: list[Any]) -> list[str]:
    """Tạo thứ tự chạy thực tế từ preference của user.

    - User chọn nhiều model → tôn trọng đúng danh sách đó.
    - User chỉ chọn 1 model → thêm các backend còn lại phía sau làm fallback.
    """
    preferred = normalize_model_preference(models)
    if len(preferred) > 1:
        return preferred
    out = preferred[:]
    for model in KNOWN_TRANSLATOR_MODELS:
        if model not in out:
            out.append(model)
    return out


def model_preference_advice(
    workspace: str,
    owner: str,
    models: list[str],
    *,
    limit: int = 8,
) -> dict:
    """Đọc các job PDF gần đây của user và trả cảnh báo cho preference.

    Warning chỉ dùng để UI gợi ý. Pipeline vẫn dùng `models` nguyên thứ tự.
    """
    models = normalize_model_preference(models)
    execution_order = expand_model_execution_order(models)
    fallback_models = [m for m in execution_order if m not in models]
    primary = models[0]
    recent = _recent_progress_files(workspace, owner, limit=limit)

    usage = Counter()
    failures = Counter()
    selected_jobs = 0
    inspected_jobs = 0

    for path in recent:
        progress = _load_json(path)
        if not progress:
            continue
        inspected_jobs += 1
        job_models = normalize_model_preference(progress.get("models") or [])
        if primary in job_models:
            selected_jobs += 1

        for records in (progress.get("translation_attempts") or {}).values():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                model = (
                    record.get("selected_model")
                    or record.get("model")
                    or ""
                )
                model = str(model).lower()
                if model in KNOWN_TRANSLATOR_MODELS:
                    usage[model] += 1
                    if record.get("ok") is False:
                        failures[model] += 1

        for model in KNOWN_TRANSLATOR_MODELS:
            failed_key = f"failed_chunks_{model}"
            failed = progress.get(failed_key) or []
            if isinstance(failed, list):
                failures[model] += len(failed)

        status = str(progress.get("status") or "").lower()
        error_detail = progress.get("error_detail") or {}
        err = str(error_detail.get("message") or "").lower()
        for model in KNOWN_TRANSLATOR_MODELS:
            if model in status or model in err:
                failures[model] += 1

    total_attempts = sum(usage.values())
    primary_attempts = usage[primary]
    primary_share = (
        round(primary_attempts / total_attempts, 3)
        if total_attempts else None
    )

    warnings: list[dict] = []
    if fallback_models:
        warnings.append({
            "level": "info",
            "code": "emergency_fallback_enabled",
            "message": (
                "Bạn chỉ chọn một model nên hệ thống sẽ thêm fallback khẩn cấp: "
                f"{', '.join(fallback_models)}. Fallback chỉ đứng sau model ưu tiên."
            ),
        })
    if failures[primary] > 0:
        warnings.append({
            "level": "warning",
            "code": "primary_recent_failures",
            "message": (
                f"Model ưu tiên '{primary}' có {failures[primary]} dấu hiệu lỗi "
                f"trong {inspected_jobs} job gần đây."
            ),
        })
    if total_attempts and primary_share is not None and primary_share < 0.5:
        warnings.append({
            "level": "warning",
            "code": "primary_low_usage",
            "message": (
                f"Trong log gần đây, '{primary}' chỉ chiếm "
                f"{round(primary_share * 100)}% lượt dịch; pipeline thường phải "
                "dùng model khác."
            ),
        })

    return {
        "models": models,
        "execution_order": execution_order,
        "fallback_models": fallback_models,
        "primary": primary,
        "recent_jobs": inspected_jobs,
        "selected_jobs": selected_jobs,
        "usage": dict(usage),
        "failures": dict(failures),
        "primary_usage_share": primary_share,
        "warnings": warnings,
    }


def _recent_progress_files(workspace: str, owner: str, *, limit: int) -> list[str]:
    jobs_dir = os.path.join(workspace, "users", safe_username(owner), "jobs")
    if not os.path.isdir(jobs_dir):
        return []
    paths: list[str] = []
    for name in os.listdir(jobs_dir):
        if not name.startswith("pdf_"):
            continue
        path = os.path.join(jobs_dir, name, "progress.json")
        if os.path.isfile(path):
            paths.append(path)
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return paths[:limit]


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
