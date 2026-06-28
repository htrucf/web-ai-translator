"""Audit trail subsystem.

Mỗi job ghi 1 file `audit.jsonl` (append-only) và 1 `env_snapshot.json`
vào thư mục `workspace/jobs/{job_id}/`. Audit log dùng cho:
  - Tái dựng (reproducibility) toàn bộ quá trình dịch của 1 bài báo
  - Bằng chứng cho 3 contributions của luận văn
  - Debug khi pipeline thất bại

Sử dụng:
    from app.audit import AuditLogger, log_event, set_current

    # Trong pipeline.run():
    audit = AuditLogger.open(job_id, job_dir)
    set_current(audit)        # context-var → các tầng sâu dùng được
    audit.log("job.started", pdf_path=...)
    ...
    audit.close()

    # Trong tầng sâu (translator, vision_nav, ...) — không cần biết job_id:
    from app.audit import log_event
    log_event("vlm.fallback_triggered", backend="gemini", element_type="input_box")
"""

from app.audit.logger import (
    AuditLogger,
    get_current,
    set_current,
    clear_current,
    log_event,
)
from app.audit.env_snapshot import write_env_snapshot

__all__ = [
    "AuditLogger",
    "get_current",
    "set_current",
    "clear_current",
    "log_event",
    "write_env_snapshot",
]
