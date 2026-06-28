"""Path / IO safety helpers — single source of truth for input validation
and atomic JSON writes. Used everywhere user-controlled identifiers cross a
filesystem boundary.

Two concerns:

1. Identifier validation (`validate_job_id`):
   prevents path traversal and command-injection through user input. Anything
   that does not match the canonical pattern is rejected at the API boundary
   so downstream code can `os.path.join` without sanitization.

2. Atomic writes (`atomic_write_json`):
   `progress.json` is written by the pipeline subprocess and read by HTTP
   handlers concurrently. A bare `open(..., "w")` truncates the file before
   `json.dump` finishes; if a reader fires in that window it sees malformed
   JSON. We write to a sibling temp file and `os.replace` (POSIX rename)
   which is atomic on the same filesystem.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any


# Job IDs are derived from upload filenames (`tex_<slug>`, `pdf_<hash>`, etc.).
# Restrict to alphanumeric + dot/underscore/hyphen to be safe.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def is_valid_job_id(job_id: str) -> bool:
    if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        return False
    # Defence in depth: even if regex misses, reject anything that resolves
    # to a parent directory.
    if ".." in job_id.split(os.sep) or ".." in job_id.split("/"):
        return False
    return True


def validate_job_id(job_id: str) -> str:
    """Return job_id if valid, else raise ValueError."""
    if not is_valid_job_id(job_id):
        raise ValueError(f"invalid job_id: {job_id!r}")
    return job_id


def is_within_directory(parent: str, child: str) -> bool:
    """True if `child` resolves to a path inside `parent` (after symlink resolution)."""
    parent_real = os.path.realpath(parent)
    child_real = os.path.realpath(child)
    if child_real == parent_real:
        return True
    return child_real.startswith(parent_real + os.sep)


def atomic_write_json(path: str, data: Any, *, indent: int = 2) -> None:
    """Write `data` as JSON to `path` atomically (no torn reads).

    Strategy: serialize to a temp file in the same directory, fsync, then
    `os.replace` over the target. `os.replace` is atomic on POSIX and on
    Windows (since Python 3.3) when source and destination are on the same
    filesystem — which is why we put the temp file alongside the target
    rather than in the system temp dir.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_", suffix=".json", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # Some filesystems (eg. tmpfs) don't support fsync
        # Windows quirk: os.replace can raise PermissionError (WinError 5/32)
        # when another process has the destination file open for reading —
        # e.g. the FastAPI status endpoint polling progress.json. POSIX
        # rename is atomic w.r.t. readers; Windows isn't. Brief retry loop
        # rides out the contention window without losing the write.
        last_err: Exception | None = None
        for attempt in range(8):  # ~3.5s total (geometric backoff)
            try:
                os.replace(tmp_path, path)
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                time.sleep(0.05 * (1.6 ** attempt))
        if last_err is not None:
            raise last_err
    except Exception:
        # Best-effort cleanup of the temp file if replace failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
