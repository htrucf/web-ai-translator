"""Per-user workspace path helpers — single source of truth.

Layout:
    workspace/
      jobs/                       # legacy (pre-multi-user) — read-only fallback
      users/
        {safe_username}/
          jobs/{job_id}/          # per-user job folders
          downloads/              # per-user arXiv cache
        ...

`safe_username` is the username with filesystem-unsafe characters replaced by
underscores. Two distinct usernames CANNOT collide because we also store the
canonical username in the DB and check ownership there — the safe form is
only used for path segments.

Public API
----------
safe_username(name) -> str
user_dir(workspace, username) -> str
user_jobs_dir(workspace, username) -> str
user_job_dir(workspace, username, job_id) -> str
user_downloads_dir(workspace, username) -> str
legacy_jobs_dir(workspace) -> str
find_job_path(workspace, job_id, username) -> str | None
"""

from __future__ import annotations

import os
import re

_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_username(name: str) -> str:
    """Convert a username to a filesystem-safe directory segment.

    Empty/None → "_anon". Leading/trailing junk stripped. Length capped at 64.
    """
    if not name:
        return "_anon"
    s = _SAFE_RE.sub("_", name.strip()).strip("._")
    if not s:
        return "_anon"
    return s[:64]


def user_dir(workspace: str, username: str) -> str:
    """Return `{workspace}/users/{safe_username}` (does not create)."""
    return os.path.join(workspace, "users", safe_username(username))


def user_jobs_dir(workspace: str, username: str) -> str:
    """Return `{workspace}/users/{safe_username}/jobs`."""
    return os.path.join(user_dir(workspace, username), "jobs")


def user_job_dir(workspace: str, username: str, job_id: str) -> str:
    """Return `{workspace}/users/{safe_username}/jobs/{job_id}`."""
    return os.path.join(user_jobs_dir(workspace, username), job_id)


def user_downloads_dir(workspace: str, username: str) -> str:
    """Return per-user arXiv download cache dir."""
    return os.path.join(user_dir(workspace, username), "downloads")


def legacy_jobs_dir(workspace: str) -> str:
    """Pre-multi-user `{workspace}/jobs`. Kept only for the migration fallback."""
    return os.path.join(workspace, "jobs")


def ensure_user_dirs(workspace: str, username: str) -> None:
    """Create the user's jobs + downloads directories if missing."""
    os.makedirs(user_jobs_dir(workspace, username), exist_ok=True)
    os.makedirs(user_downloads_dir(workspace, username), exist_ok=True)


def find_job_path(
    workspace: str,
    job_id: str,
    username: str,
    allow_legacy: bool = False,
) -> str | None:
    """Locate the on-disk path for a job.

    Lookup order:
      1. `users/{safe_username}/jobs/{job_id}` — current per-user location
      2. legacy `jobs/{job_id}` — only if `allow_legacy=True` (admin only)

    Returns None if the directory does not exist anywhere we're allowed to read.
    The DB ownership check is the actual access guard — this only finds the path.
    """
    p = user_job_dir(workspace, username, job_id)
    if os.path.isdir(p):
        return p
    if allow_legacy:
        legacy = os.path.join(legacy_jobs_dir(workspace), job_id)
        if os.path.isdir(legacy):
            return legacy
    return None


def resolve_job_dir(
    workspace: str,
    job_id: str,
    username: str,
    is_admin: bool = False,
    create: bool = False,
) -> str:
    """Resolve a job directory for a (job_id, username) pair.

    If `create=True`, ensures the per-user directory exists (never legacy).
    If the job folder already exists in the legacy location and the caller is
    admin, return the legacy path so historical jobs keep working.
    """
    found = find_job_path(workspace, job_id, username, allow_legacy=is_admin)
    if found:
        return found
    target = user_job_dir(workspace, username, job_id)
    if create:
        os.makedirs(target, exist_ok=True)
    return target
