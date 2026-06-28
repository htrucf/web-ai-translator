"""OS-appropriate user data paths.

When this app is packaged and distributed, the binary's CWD is wherever the
user double-clicks — could be Desktop, Downloads, Program Files, anywhere.
A relative `./workspace` would scatter user data and break on next launch.

Resolution order for each path (first hit wins):
  1. Explicit env var (WORKSPACE_DIR / BROWSER_DATA_DIR)
  2. OS-appropriate user data location:
       Windows: %APPDATA%/web-ai-translator/
       macOS:   ~/Library/Application Support/web-ai-translator/
       Linux:   $XDG_DATA_HOME/web-ai-translator/  (default ~/.local/share/...)
  3. Fallback to ./workspace beside the running script (dev mode only)

The dev fallback exists so `uvicorn app.main:app --reload` from the backend
directory keeps working without setting any env vars.
"""

from __future__ import annotations

import os
import sys

APP_NAME = "web-ai-translator"


def _platform_data_root() -> str:
    """Return the per-OS root dir for application data (without APP_NAME suffix)."""
    if sys.platform == "win32":
        # %APPDATA% is roaming; falls back to %LOCALAPPDATA% if missing
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if root:
            return root
        return os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    # Linux / BSD / other Unix — follow XDG Base Directory spec
    return os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )


def _dev_fallback_root() -> str:
    """Repo-relative `./workspace` parent for dev mode."""
    # paths.py lives at backend/app/paths.py — fallback is backend/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_packaged() -> bool:
    """True when running from a PyInstaller bundle.

    PyInstaller sets sys.frozen=True. In that case we MUST use the platform
    user-data dir — the dev fallback would write inside the read-only bundle.
    """
    return bool(getattr(sys, "frozen", False))


def user_data_dir() -> str:
    """Return the app's user-data dir, creating it if missing."""
    root = os.path.join(_platform_data_root(), APP_NAME)
    os.makedirs(root, exist_ok=True)
    return root


def workspace_dir() -> str:
    """Resolve the workspace dir.

    Env var WORKSPACE_DIR wins. Otherwise: user data dir in production,
    `backend/workspace/` in dev (so existing dev jobs keep working).
    """
    override = os.environ.get("WORKSPACE_DIR")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    elif _is_packaged():
        path = os.path.join(user_data_dir(), "workspace")
    else:
        path = os.path.join(_dev_fallback_root(), "workspace")
    os.makedirs(path, exist_ok=True)
    return path


def browser_data_dir() -> str:
    """Resolve the Playwright user-data dir.

    Env var BROWSER_DATA_DIR wins. Stored separately from workspace so users
    can wipe Gemini login state without losing translation history.
    """
    override = os.environ.get("BROWSER_DATA_DIR")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    elif _is_packaged():
        path = os.path.join(user_data_dir(), "browser_data")
    else:
        path = os.path.join(_dev_fallback_root(), "browser_data")
    os.makedirs(path, exist_ok=True)
    return path
