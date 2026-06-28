"""Append-only JSONL audit logger per job.

Mỗi job → 1 file `audit.jsonl` trong `workspace/jobs/{job_id}/`.
Mỗi dòng = 1 JSON event với shape:

    {"ts": "...Z", "seq": N, "job_id": "...", "phase": "...",
     "event_type": "...", "data": {...}}

Thread-safe: dùng threading.Lock — nhiều coroutine cùng job ghi
song song được. Mỗi event flush ngay để không mất dữ liệu khi crash.

Cross-stack context: dùng contextvars để các module tầng sâu
(translator, vision_nav, selector_memory) tự gọi `log_event(...)`
mà không phải truyền `job_id` xuyên qua call stack.

Resume support: nếu file đã tồn tại, đếm dòng để tiếp tục seq counter.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Context-var: logger hiện tại của coroutine/thread ───────────────
_current: contextvars.ContextVar[Optional["AuditLogger"]] = contextvars.ContextVar(
    "audit_logger", default=None
)


def get_current() -> Optional["AuditLogger"]:
    """Trả về AuditLogger hiện tại (set bởi pipeline) hoặc None."""
    return _current.get()


def set_current(audit: "AuditLogger") -> contextvars.Token:
    """Set logger hiện tại. Trả về Token để reset sau."""
    return _current.set(audit)


def clear_current(token: Optional[contextvars.Token] = None) -> None:
    """Reset context var. Nếu có token thì reset chính xác về giá trị trước."""
    if token is not None:
        try:
            _current.reset(token)
            return
        except Exception:
            pass
    _current.set(None)


def log_event(event_type: str, **data: Any) -> None:
    """Tiện ích cho tầng sâu — log nếu có audit logger hiện tại, no-op nếu không.

    Không bao giờ raise — audit không được crash pipeline.
    """
    audit = _current.get()
    if audit is None:
        return
    try:
        audit.log(event_type, **data)
    except Exception as e:
        logger.debug("audit.log_event failed (non-fatal): %s", e)


# ── Phases (job lifecycle) ──────────────────────────────────────────
PHASE_INIT = "init"
PHASE_EXTRACTION = "extraction"
PHASE_CHUNKING = "chunking"
PHASE_GLOSSARY = "glossary"
PHASE_TRANSLATING = "translating"
PHASE_QUALITY_FIX = "quality_fix"
PHASE_REBUILDING = "rebuilding"
PHASE_QUALITY = "quality"
PHASE_VALIDATION = "validation"
PHASE_FINISHED = "finished"


def _now_iso() -> str:
    """UTC ISO-8601 với microsecond precision và suffix Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class AuditLogger:
    """Per-job audit logger ghi file JSONL.

    Cách dùng chính:
        audit = AuditLogger.open(job_id, job_dir)
        audit.set_phase("translating")
        audit.log("chunk.sent", chunk_idx=0, char_count=1500)
        ...
        audit.close()

    Hoặc dùng context manager:
        with AuditLogger.open(job_id, job_dir) as audit:
            audit.log(...)
    """

    # Per-process singleton cache: 1 logger per job_id để các module độc lập
    # dùng chung file handle (translator, vision_nav, scheduler...).
    _instances: dict[str, "AuditLogger"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def open(cls, job_id: str, job_dir: str) -> "AuditLogger":
        """Mở (hoặc reuse) logger cho job_id. Idempotent — gọi nhiều lần OK."""
        with cls._instances_lock:
            existing = cls._instances.get(job_id)
            if existing is not None and not existing._closed:
                return existing
            inst = cls(job_id, job_dir)
            cls._instances[job_id] = inst
            return inst

    def __init__(self, job_id: str, job_dir: str):
        self.job_id = job_id
        self.job_dir = job_dir
        self.audit_path = os.path.join(job_dir, "audit.jsonl")
        self.responses_dir = os.path.join(job_dir, "audit_responses")
        os.makedirs(job_dir, exist_ok=True)
        os.makedirs(self.responses_dir, exist_ok=True)

        # Resume: đếm số event cũ để tiếp tục seq counter.
        self._seq = self._count_existing_events()
        self._phase = PHASE_INIT
        self._run_id = uuid.uuid4().hex[:12]   # ID của lần chạy này (resume → run_id mới)
        self._lock = threading.Lock()
        self._closed = False

        # Mở file ở mode append. Line buffering không có trên Windows
        # binary mode, nên dùng text mode + flush thủ công.
        self._fh = open(self.audit_path, "a", encoding="utf-8", buffering=1)

    def _count_existing_events(self) -> int:
        """Đếm số dòng đã có trong audit.jsonl (resume support)."""
        if not os.path.exists(self.audit_path):
            return 0
        try:
            with open(self.audit_path, "r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def run_id(self) -> str:
        return self._run_id

    def set_phase(self, phase: str) -> None:
        """Đổi phase hiện tại. Log 1 event `job.phase_changed`."""
        if phase == self._phase:
            return
        prev = self._phase
        self._phase = phase
        self.log("job.phase_changed", from_phase=prev, to_phase=phase)

    def log(self, event_type: str, **data: Any) -> None:
        """Ghi 1 event. Không raise — audit không crash pipeline."""
        if self._closed:
            return
        try:
            with self._lock:
                self._seq += 1
                event = {
                    "ts": _now_iso(),
                    "seq": self._seq,
                    "run_id": self._run_id,
                    "job_id": self.job_id,
                    "phase": self._phase,
                    "event_type": event_type,
                    "data": _sanitize(data),
                }
                self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._fh.flush()
        except Exception as e:
            logger.debug("AuditLogger.log failed (non-fatal): %s", e)

    def save_raw_response(self, chunk_idx: int, attempt: int, text: str,
                          kind: str = "translation") -> str:
        """Lưu raw response của AI vào file riêng. Trả về relative path."""
        try:
            fname = f"chunk_{chunk_idx:03d}_attempt_{attempt}_{kind}.txt"
            full = os.path.join(self.responses_dir, fname)
            with open(full, "w", encoding="utf-8") as f:
                f.write(text or "")
            return os.path.relpath(full, self.job_dir).replace("\\", "/")
        except Exception as e:
            logger.debug("save_raw_response failed (non-fatal): %s", e)
            return ""

    def save_raw_prompt(self, chunk_idx: int, attempt: int, prompt: str) -> str:
        """Lưu raw prompt gửi đi. Trả về relative path."""
        try:
            fname = f"chunk_{chunk_idx:03d}_attempt_{attempt}_prompt.txt"
            full = os.path.join(self.responses_dir, fname)
            with open(full, "w", encoding="utf-8") as f:
                f.write(prompt or "")
            return os.path.relpath(full, self.job_dir).replace("\\", "/")
        except Exception as e:
            logger.debug("save_raw_prompt failed (non-fatal): %s", e)
            return ""

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            self._closed = True
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
        # Remove from singleton cache
        with self._instances_lock:
            if self._instances.get(self.job_id) is self:
                self._instances.pop(self.job_id, None)

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            try:
                self.log("error.unexpected",
                         exc_type=exc_type.__name__ if exc_type else "Unknown",
                         message=str(exc)[:500])
            except Exception:
                pass
        self.close()


# ── Sanitization ────────────────────────────────────────────────────

_MAX_STR_LEN = 4000   # cap individual string fields (raw text goes to files)


def _sanitize(value: Any) -> Any:
    """Convert dữ liệu sang JSON-serializable; cap chuỗi dài.

    Audit log không nên chứa raw prompt/response (đã có file riêng).
    Trường text dài tự động bị cắt để giữ JSONL grep-friendly.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_STR_LEN:
            return value[:_MAX_STR_LEN] + f"...[truncated {len(value)} chars]"
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    # Fallback: stringify exotic types (Path, datetime, dataclass...)
    try:
        return _sanitize(str(value))
    except Exception:
        return None
