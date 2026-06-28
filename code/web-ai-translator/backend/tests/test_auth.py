# -*- coding: utf-8 -*-
"""Tests for app/auth.py — session-based authentication backed by SQLite.

Coverage:
  login()           — valid/invalid credentials, returns token
  logout()          — invalidates session
  validate_token()  — valid, expired, unknown tokens
  _extract_token()  — parses Authorization header
  auth_middleware()  — public paths bypass, protected paths require token
"""

import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from app.auth import (
    login,
    logout,
    validate_token,
    _extract_token,
    auth_middleware,
    _PUBLIC_PATHS,
)
from app.database import get_session, delete_session, create_session
from fastapi import HTTPException

# Built-in admin credentials — set in conftest.py before app.auth import.
ADMIN_USER = "test_admin"
ADMIN_PASS = "test_password"


# ── login ────────────────────────────────────────────────────────────────

def test_login_valid_credentials():
    """Valid username/password returns a non-empty token string."""
    token = login(ADMIN_USER, ADMIN_PASS)
    assert isinstance(token, str)
    assert len(token) > 10
    delete_session(token)


def test_login_invalid_password():
    """Wrong password raises HTTPException 401."""
    with pytest.raises(HTTPException) as exc_info:
        login(ADMIN_USER, "wrong_password")
    assert exc_info.value.status_code == 401


def test_login_invalid_username():
    """Wrong username raises HTTPException 401."""
    with pytest.raises(HTTPException) as exc_info:
        login("wrong_user", ADMIN_PASS)
    assert exc_info.value.status_code == 401


def test_login_creates_session():
    """After login, the token exists in the session store."""
    token = login(ADMIN_USER, ADMIN_PASS)
    assert get_session(token) is not None
    delete_session(token)


# ── logout ───────────────────────────────────────────────────────────────

def test_logout_removes_session():
    """Logout removes the token from session store."""
    token = login(ADMIN_USER, ADMIN_PASS)
    logout(token)
    assert get_session(token) is None


def test_logout_nonexistent_token():
    """Logout with unknown token does not raise."""
    logout("nonexistent_token_xyz")


# ── validate_token ──────────────────────────────────────────────────────

def test_validate_valid_token():
    """Valid token returns True."""
    token = login(ADMIN_USER, ADMIN_PASS)
    assert validate_token(token) is True
    delete_session(token)


def test_validate_unknown_token():
    """Unknown token returns False."""
    assert validate_token("unknown_token_abc") is False


def test_validate_expired_token():
    """Expired token (past IDLE_TIMEOUT) returns False and is purged."""
    token = "expired_token_test"
    # Plant a session whose last_active is way in the past.
    create_session(token, ADMIN_USER, time.time() - 10_000)
    assert validate_token(token) is False
    assert get_session(token) is None


def test_validate_refreshes_timer():
    """Validating a token bumps last_active forward."""
    token = login(ADMIN_USER, ADMIN_PASS)
    old = get_session(token)["last_active"]
    time.sleep(0.05)
    validate_token(token)
    assert get_session(token)["last_active"] >= old
    delete_session(token)


# ── _extract_token ──────────────────────────────────────────────────────

def test_extract_bearer_token():
    """Authorization: Bearer <token> returns the token."""
    req = MagicMock()
    req.headers = {"Authorization": "Bearer my_secret_token"}
    assert _extract_token(req) == "my_secret_token"


def test_extract_no_auth_header():
    """Missing Authorization header returns None."""
    req = MagicMock()
    req.headers = {}
    assert _extract_token(req) is None


def test_extract_wrong_scheme():
    """Non-Bearer scheme returns None."""
    req = MagicMock()
    req.headers = {"Authorization": "Basic dXNlcjpwYXNz"}
    assert _extract_token(req) is None


# ── auth_middleware ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_middleware_public_path_bypasses_auth():
    """Public paths (/health, /api/auth/login) pass through without token."""
    req = MagicMock()
    req.url.path = "/health"
    req.headers = {}
    call_next = AsyncMock(return_value="ok")
    result = await auth_middleware(req, call_next)
    assert result == "ok"
    call_next.assert_called_once()


@pytest.mark.asyncio
async def test_middleware_api_without_token_returns_401(monkeypatch):
    """API path without token returns 401 JSON response."""
    # Restore real auth functions (bypassed by conftest fixture)
    from app import auth as auth_module
    monkeypatch.setattr(auth_module, "validate_token", validate_token)
    monkeypatch.setattr(auth_module, "_extract_token", _extract_token)

    req = MagicMock()
    req.url.path = "/api/jobs"
    req.headers = {"Authorization": ""}
    call_next = AsyncMock()
    result = await auth_middleware(req, call_next)
    assert result.status_code == 401
    call_next.assert_not_called()


@pytest.mark.asyncio
async def test_middleware_api_with_valid_token_passes():
    """API path with valid token passes through."""
    token = login(ADMIN_USER, ADMIN_PASS)
    req = MagicMock()
    req.url.path = "/api/jobs"
    req.headers = {"Authorization": f"Bearer {token}"}
    call_next = AsyncMock(return_value="ok")
    result = await auth_middleware(req, call_next)
    assert result == "ok"
    delete_session(token)


@pytest.mark.asyncio
async def test_middleware_non_api_path_bypasses():
    """Non-API paths (static assets) are not protected."""
    req = MagicMock()
    req.url.path = "/static/app.js"
    req.headers = {}
    call_next = AsyncMock(return_value="ok")
    result = await auth_middleware(req, call_next)
    assert result == "ok"
