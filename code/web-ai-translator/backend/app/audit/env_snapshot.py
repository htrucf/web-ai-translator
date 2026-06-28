"""Environment snapshot — chụp môi trường runtime 1 lần khi job bắt đầu.

Ghi ra file `env_snapshot.json` trong thư mục job. Mục đích là tái dựng
được kết quả: phiên bản Playwright/Chromium/PyMuPDF, AI backend, ENV vars
opt-in (scheduler/multi-agent), Ollama models đang có sẵn.

Cần thiết cho thesis defense — chứng minh "kết quả X được tạo ra trong môi
trường Y" thay vì chỉ "kết quả X".
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _safe_version(import_path: str) -> str:
    """Trả về __version__ của 1 package, hoặc 'unknown'."""
    try:
        mod = __import__(import_path, fromlist=["__version__"])
        return str(getattr(mod, "__version__", "unknown"))
    except Exception:
        return "not_installed"


def _list_ollama_models(url: str, timeout: float = 3.0) -> list[str]:
    """Liệt kê models Ollama đang có sẵn. Trả về [] nếu Ollama không chạy."""
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{url.rstrip('/')}/api/tags")
            if r.status_code != 200:
                return []
            data = r.json() or {}
            return sorted(m.get("name", "") for m in data.get("models", []) if m)
    except Exception:
        return []


def _scheduler_config() -> dict:
    """Đọc config liên quan AccountPool/Scheduler từ ENV."""
    return {
        "ENABLE_SCHEDULER": os.getenv("ENABLE_SCHEDULER", "0") == "1",
        "ACCOUNT_LEASE_TTL": int(os.getenv("ACCOUNT_LEASE_TTL", "7200")),
        "ACCOUNT_COOLDOWN": int(os.getenv("ACCOUNT_COOLDOWN", "1800")),
        "GEMINI_ACCOUNTS_FILE": os.getenv("GEMINI_ACCOUNTS_FILE", ""),
    }


def _multi_agent_config() -> dict:
    return {
        "ENABLE_MULTI_AGENT": os.getenv("ENABLE_MULTI_AGENT", "0") == "1",
        "MULTI_AGENT_MODEL": os.getenv("MULTI_AGENT_MODEL", "qwen2.5:7b"),
        "MULTI_AGENT_MAX_CHUNKS": int(os.getenv("MULTI_AGENT_MAX_CHUNKS", "10")),
    }


def collect_env_snapshot(job_id: str, extra: dict | None = None) -> dict:
    """Thu thập snapshot môi trường. extra cho phép pipeline bổ sung field."""
    try:
        from app.config import settings
        config = {
            "AI_BACKEND": getattr(settings, "AI_BACKEND", "gemini"),
            "TRANSLATOR_MODE": getattr(settings, "TRANSLATOR_MODE", "new_browser"),
            "WORKSPACE_DIR": getattr(settings, "WORKSPACE_DIR", ""),
            "OLLAMA_URL": getattr(settings, "OLLAMA_URL", "http://localhost:11434"),
            "VLM_MODEL": getattr(settings, "VLM_MODEL", ""),
        }
        ollama_url = config["OLLAMA_URL"]
    except Exception:
        config = {}
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")

    ollama_models = _list_ollama_models(ollama_url)

    snap = {
        "ts": _now_iso(),
        "job_id": job_id,
        "python": {
            "version": sys.version.split()[0],
            "implementation": platform.python_implementation(),
        },
        "os": {
            "platform": sys.platform,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "packages": {
            "playwright": _safe_version("playwright"),
            "pymupdf": _safe_version("fitz") if _safe_version("fitz") != "not_installed"
                       else _safe_version("pymupdf"),
            "fastapi": _safe_version("fastapi"),
            "httpx": _safe_version("httpx"),
            "structlog": _safe_version("structlog"),
        },
        "translator": {
            "backend": config.get("AI_BACKEND"),
            "mode": config.get("TRANSLATOR_MODE"),
        },
        "config": config,
        "ollama": {
            "url": ollama_url,
            "available": bool(ollama_models),
            "models": ollama_models,
        },
        "scheduler": _scheduler_config(),
        "multi_agent": _multi_agent_config(),
    }

    if extra:
        snap["extra"] = extra
    return snap


def write_env_snapshot(job_id: str, job_dir: str, extra: dict | None = None) -> str:
    """Ghi env snapshot ra file. Trả về path. Không raise nếu lỗi."""
    try:
        snap = collect_env_snapshot(job_id, extra)
        os.makedirs(job_dir, exist_ok=True)
        path = os.path.join(job_dir, "env_snapshot.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2, ensure_ascii=False)
        return path
    except Exception as e:
        logger.warning("write_env_snapshot failed (non-fatal): %s", e)
        return ""
