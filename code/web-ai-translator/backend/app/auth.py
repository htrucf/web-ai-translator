"""Session-based authentication — multi-user support.

Users are stored in SQLite (via database.py).
The legacy env-var user (AUTH_USERNAME / AUTH_PASSWORD) is kept as a built-in
admin account so the app works without any prior registration.

Sessions track WHICH user owns a token: every API request can resolve the
current user via `current_username(request)`. Per-user job isolation
relies on this — see `app/user_paths.py`.

Public API
----------
login(username, password) -> token
logout(token)
validate_token(token) -> bool
current_username(request_or_token) -> str | None
register_user(username, password, security_question, security_answer) -> None  # raises on error
get_security_question(username) -> str                                          # raises 404 if unknown
reset_password(username, security_answer, new_password) -> None                # raises on bad answer
_extract_token(request) -> str | None
auth_middleware(request, call_next)
"""

import hashlib
import os
import secrets
import time
from typing import Union
from fastapi import HTTPException, Request

# ── Built-in admin account (opt-in via env vars) ──────────────────────────────
# Previously defaulted to "trucnb" / "1111" — those defaults are removed.
# The built-in admin only exists if BOTH AUTH_USERNAME and AUTH_PASSWORD are set.
# Otherwise everyone must register through /api/auth/register and no account has
# admin privileges. This prevents shipping with a known credential.
_USERNAME = (os.getenv("AUTH_USERNAME") or "").strip()
_PASSWORD = os.getenv("AUTH_PASSWORD") or ""
_BUILTIN_ADMIN_ENABLED = bool(_USERNAME and _PASSWORD)

# Empty string when disabled — `_is_admin` callers must guard against it because
# a stray "" username could otherwise match. We add `bool(ADMIN_USERNAME) and …`
# at every call site (see history.py, pdf/routes.py, main.py).
ADMIN_USERNAME = _USERNAME if _BUILTIN_ADMIN_ENABLED else ""

if not _BUILTIN_ADMIN_ENABLED:
    print(
        "[auth] AUTH_USERNAME / AUTH_PASSWORD not set — built-in admin "
        "account is DISABLED. Register users through /api/auth/register."
    )

# ── Session store ─────────────────────────────────────────────────────────────
IDLE_TIMEOUT = int(os.getenv("SESSION_IDLE_TIMEOUT", "14400"))  # seconds (default 4h)

# Sessions are persisted to SQLite (see app.database.sessions table) so users
# stay logged in after the desktop launcher restarts. We use time.time() (wall
# clock) instead of time.monotonic() because monotonic doesn't survive a reboot.

# Minimum password length for registered users
_MIN_PASSWORD_LEN = 6


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# Only computed when the built-in admin is enabled — leaving it as the sha256
# of the empty string would let `username="", password=""` slip past the check
# below if the short-circuit guard were ever removed.
_PASSWORD_HASH = _hash_password(_PASSWORD) if _BUILTIN_ADMIN_ENABLED else ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_credentials(username: str, password: str) -> bool:
    """Return True if username/password match either built-in or DB user."""
    # 1. Built-in admin (only when env vars configured)
    if (
        _BUILTIN_ADMIN_ENABLED
        and username == _USERNAME
        and _hash_password(password) == _PASSWORD_HASH
    ):
        return True

    # 2. DB-registered user
    try:
        from app.database import get_user
        user = get_user(username)
        if user and user["password_hash"] == _hash_password(password):
            return True
    except Exception:
        pass  # DB not ready yet (e.g. tests before startup)

    return False


def is_admin(username: str) -> bool:
    """Return True if `username` has admin privileges.

    Two paths to admin:
      1. Matches the built-in env-var admin (AUTH_USERNAME).
      2. Has is_admin=1 in the users table (auto-set for the first registration
         when no env-var admin is configured — see register_user).
    """
    if not username:
        return False
    if _BUILTIN_ADMIN_ENABLED and username == _USERNAME:
        return True
    try:
        from app.database import is_db_admin
        return is_db_admin(username)
    except Exception:
        return False


# ── Core auth functions ───────────────────────────────────────────────────────

def _resolve_session(token: str) -> str | None:
    """Shared logic for validate_token / current_username.

    Returns the session's username if the token exists and hasn't expired.
    Side effects: deletes expired rows, bumps last_active on success.
    """
    from app.database import get_session, delete_session, touch_session
    sess = get_session(token)
    if not sess:
        return None
    now = time.time()
    if now - sess["last_active"] > IDLE_TIMEOUT:
        delete_session(token)
        return None
    touch_session(token, now)
    return sess["username"]


def login(username: str, password: str) -> str:
    """Validate credentials and return a new session token.

    Raises HTTPException(401) on bad credentials.
    """
    if not _verify_credentials(username, password):
        raise HTTPException(status_code=401, detail="Tên đăng nhập hoặc mật khẩu không đúng")

    from app.database import create_session
    token = secrets.token_urlsafe(32)
    create_session(token, username.strip(), time.time())
    return token


def logout(token: str):
    """Invalidate a session token (no-op if already gone)."""
    from app.database import delete_session
    delete_session(token)


def validate_token(token: str) -> bool:
    """Return True if the token is valid and not expired. Updates last_active on success."""
    return _resolve_session(token) is not None


def _extract_token(request: Request) -> str | None:
    """Pull Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def current_username(request_or_token: Union[Request, str, None]) -> str | None:
    """Resolve the username for the current session, or None if not authenticated.

    Accepts either a FastAPI Request (extracts Bearer token) or a raw token string.
    Also refreshes the idle timer on success — same semantics as `validate_token`.
    """
    if request_or_token is None:
        return None
    token = (
        request_or_token
        if isinstance(request_or_token, str)
        else _extract_token(request_or_token)
    )
    if not token:
        return None
    return _resolve_session(token)


# ── Registration ──────────────────────────────────────────────────────────────

def register_user(
    username: str,
    password: str,
    security_question: str,
    security_answer: str,
) -> None:
    """Register a new user account.

    Raises HTTPException:
      400 — username taken, password too short, empty fields
    """
    username = username.strip()
    security_question = security_question.strip()
    security_answer = security_answer.strip()

    if not username:
        raise HTTPException(status_code=400, detail="Tên đăng nhập không được để trống")
    if len(password) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Mật khẩu phải có ít nhất {_MIN_PASSWORD_LEN} ký tự",
        )
    if not security_question:
        raise HTTPException(status_code=400, detail="Câu hỏi bảo mật không được để trống")
    if not security_answer:
        raise HTTPException(status_code=400, detail="Câu trả lời bảo mật không được để trống")

    # Reject if username collides with built-in admin (only when admin enabled —
    # otherwise an empty AUTH_USERNAME would block registration with empty name,
    # which is already rejected above).
    if _BUILTIN_ADMIN_ENABLED and username == _USERNAME:
        raise HTTPException(status_code=400, detail="Tên đăng nhập đã tồn tại")

    # First-user-becomes-admin: when no built-in admin is configured AND there
    # are no registered users yet, the first registration auto-promotes itself.
    # This bootstraps a single-user desktop install without any env var setup.
    # If the env-var admin IS configured, ownership is already covered, so new
    # registrations are treated as regular users.
    from app.database import create_user, count_users
    promote = (not _BUILTIN_ADMIN_ENABLED) and count_users() == 0

    ok = create_user(
        username=username,
        password_hash=_hash_password(password),
        security_question=security_question,
        # store answer case-insensitive hash
        security_answer_hash=_hash_password(security_answer.lower()),
        is_admin=promote,
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Tên đăng nhập đã tồn tại")
    if promote:
        print(f"[auth] {username} registered as the first user — granted admin role")


# ── Password reset ────────────────────────────────────────────────────────────

def get_security_question(username: str) -> str:
    """Return the security question for a user.

    Raises HTTPException(404) if user not found.
    """
    username = username.strip()

    # Built-in admin has no security question
    if _BUILTIN_ADMIN_ENABLED and username == _USERNAME:
        raise HTTPException(
            status_code=404,
            detail="Tài khoản này không hỗ trợ tính năng quên mật khẩu",
        )

    from app.database import get_user
    user = get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    return user["security_question"]


def reset_password(username: str, security_answer: str, new_password: str) -> None:
    """Reset password after verifying the security answer.

    Raises HTTPException:
      404 — user not found
      400 — wrong answer or password too short
    """
    username = username.strip()

    if _BUILTIN_ADMIN_ENABLED and username == _USERNAME:
        raise HTTPException(
            status_code=400,
            detail="Tài khoản này không hỗ trợ tính năng quên mật khẩu",
        )

    if len(new_password) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Mật khẩu mới phải có ít nhất {_MIN_PASSWORD_LEN} ký tự",
        )

    from app.database import get_user, update_user_password
    user = get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")

    expected_hash = _hash_password(security_answer.strip().lower())
    if user["security_answer_hash"] != expected_hash:
        raise HTTPException(status_code=400, detail="Câu trả lời bảo mật không đúng")

    update_user_password(username, _hash_password(new_password))


# ── Paths that bypass auth ────────────────────────────────────────────────────
_PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/security-question",
    "/api/auth/forgot-password",
    "/api/health/setup",
    "/api/translate/supported-formats",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
}


async def auth_middleware(request: Request, call_next):
    """FastAPI middleware: reject unauthenticated requests to /api/* endpoints."""
    path = request.url.path

    # Allow public paths and non-API static assets
    if path in _PUBLIC_PATHS or not path.startswith("/api/"):
        return await call_next(request)

    token = _extract_token(request)
    if not token or not validate_token(token):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Chưa đăng nhập hoặc phiên đã hết hạn"},
        )

    return await call_next(request)
