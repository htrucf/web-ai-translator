"""Pre-flight check: ensure no orphan translator browser is still alive
before starting a new job.

Why this exists:
  The translator pipeline launches a Playwright Chromium (or attaches to a
  user-owned Chrome via CDP) that uses our `browser_data` profile. If a
  previous job crashed or the user closed the launcher window without
  cleanup, that Chromium can stay alive, holding the profile lock. Starting
  a new job would either:
    (a) collide on the profile lock and fail mid-pipeline, or
    (b) silently reuse a Gemini session whose auth/state is unknown.

  Easier and safer to refuse the new job up front with a 409 and ask the
  user to close the leftover window manually — matches the "single job at
  a time" model the user picked in the streamline discussion.
"""

from __future__ import annotations

import os
from fastapi import HTTPException

from app import paths


# Profile basename — always "browser_data" unless BROWSER_DATA_DIR env var
# points somewhere else. Resolved lazily so tests that override the env var
# still see the right marker.
def _profile_marker() -> str:
    return os.path.basename(os.path.abspath(paths.browser_data_dir()))


_CHROMIUM_NAMES = {
    "chrome.exe", "chrome", "chromium", "chromium.exe",
    "chromium-browser", "google-chrome", "headless_shell",
}


def count_translator_browsers() -> int:
    """Number of Chromium processes that look like *our* translator browser.

    Matches either the Playwright-bundled Chromium (`ms-playwright` in
    cmdline) or any Chrome attached to our profile (`browser_data` in
    cmdline). Skips arbitrary user Chrome windows.

    Returns 0 if psutil isn't installed — fail open rather than blocking
    every job in environments where the dep is missing.
    """
    try:
        import psutil
    except ImportError:
        return 0

    marker = _profile_marker()
    count = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            if pname not in _CHROMIUM_NAMES:
                continue
            cmdline = proc.info.get("cmdline") or []
            cmd_str = " ".join(cmdline)
            if "ms-playwright" in cmd_str or marker in cmd_str:
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return count


def require_no_browser_running() -> None:
    """Raise HTTPException(409) if a translator browser is still alive.

    Call at the top of any endpoint that spawns a new pipeline job. The
    error message tells the user to close the leftover browser window —
    we deliberately don't kill it automatically to avoid clobbering an
    in-flight job the user thinks is still running.
    """
    n = count_translator_browsers()
    if n > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "Browser dịch của lần chạy trước vẫn còn mở. "
                "Hãy đóng cửa sổ Chrome/Chromium đó rồi thử lại."
            ),
        )
