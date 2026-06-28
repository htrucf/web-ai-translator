"""Tests for per-user isolation and ownership guards.

Verifies:
  - /api/jobs and /api/pdf-translate/jobs only list the caller's own jobs
  - Cross-user access to a specific job → 403
  - Built-in admin (env var account) sees legacy `workspace/jobs/` entries
  - find_job_path / safe_username / resolve_job_dir behave as documented
  - 401 is raised when no user is authenticated

Auth strategy
-------------
The shared `bypass_auth` fixture in conftest.py disables `validate_token` and
`_extract_token`, but does NOT populate `current_username`. These tests need
to control which user is "logged in", so we additionally monkeypatch
`current_username` in both `app.main` and `app.pdf.routes` to a settable
identity. The DB ownership column is populated via `upsert_job(...,
username=...)` so `_check_owner` exercises real logic.
"""

import json
import os

import pytest


pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect app.database.DB_PATH to a tmp SQLite file and init schema.

    Without this, tests would write to the real `workspace/history.db`. The
    monkeypatch is autouse so every test in this module is fully isolated.
    """
    db_file = str(tmp_path / "test_history.db")
    import app.database as db
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()
    yield db_file


@pytest.fixture
def as_user(monkeypatch):
    """Return a setter that controls which user is "authenticated".

    Usage:
        as_user("alice")
        res = await client.get(...)   # request appears to come from alice
        as_user("bob")
        res = await client.get(...)   # now appears to come from bob

    Uses None to simulate an unauthenticated request → routes raise 401.
    """
    state = {"name": None}

    def fake_current_username(_req_or_token):
        return state["name"]

    import app.main as main_mod
    import app.pdf.routes as pdf_routes_mod
    monkeypatch.setattr(main_mod, "current_username", fake_current_username)
    monkeypatch.setattr(pdf_routes_mod, "current_username", fake_current_username)

    def setter(username):
        state["name"] = username

    return setter


# ── Helpers ───────────────────────────────────────────────────────────────────

_FAKE_PDF = b"%PDF-1.4\n%fake\n%%EOF\n"


def make_user_job(
    workspace: str,
    username: str,
    job_id: str,
    *,
    source_type: str = "pdf_only",
    status: str = "done",
    title: str = "Test Paper",
    with_translated_pdf: bool = True,
    with_original_pdf: bool = True,
) -> str:
    """Create a job under `workspace/users/{safe_username}/jobs/{job_id}/`.

    Also writes a corresponding row to the (test-isolated) DB with the
    username column set so ownership checks resolve correctly.
    """
    from app.user_paths import safe_username
    from app.database import upsert_job

    user_jobs_root = os.path.join(
        workspace, "users", safe_username(username), "jobs"
    )
    job_dir = os.path.join(user_jobs_root, job_id)
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    progress = {
        "status": status,
        "source_type": source_type,
        "title": title,
        "page_count": 1,
        "translated_chunks": {},
    }
    with open(os.path.join(job_dir, "progress.json"), "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False)

    if with_original_pdf:
        with open(os.path.join(job_dir, "original.pdf"), "wb") as f:
            f.write(_FAKE_PDF)
    if with_translated_pdf:
        with open(os.path.join(output_dir, "translated.pdf"), "wb") as f:
            f.write(_FAKE_PDF)

    upsert_job(
        job_id,
        username=username,
        source_type=source_type,
        status=status,
        title=title,
    )
    return job_dir


def make_legacy_job(
    workspace: str,
    job_id: str,
    *,
    source_type: str = "pdf_only",
    status: str = "done",
    title: str = "Legacy Paper",
) -> str:
    """Create a pre-multi-user job under `workspace/jobs/{job_id}/`.

    Does NOT set username in DB — represents jobs from before migration.
    """
    job_dir = os.path.join(workspace, "jobs", job_id)
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    progress = {
        "status": status,
        "source_type": source_type,
        "title": title,
        "translated_chunks": {},
    }
    with open(os.path.join(job_dir, "progress.json"), "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False)
    with open(os.path.join(job_dir, "original.pdf"), "wb") as f:
        f.write(_FAKE_PDF)
    with open(os.path.join(output_dir, "translated.pdf"), "wb") as f:
        f.write(_FAKE_PDF)
    return job_dir


def admin_username() -> str:
    from app.auth import ADMIN_USERNAME
    return ADMIN_USERNAME


# ── /api/pdf-translate/jobs — listing isolation ──────────────────────────────

async def test_pdf_jobs_list_only_callers_jobs(client, tmp_workspace, as_user):
    """Alice's listing must not contain bob's per-user jobs."""
    make_user_job(tmp_workspace, "alice", "pdf_alice_001")
    make_user_job(tmp_workspace, "bob", "pdf_bob_001")

    as_user("alice")
    res = await client.get("/api/pdf-translate/jobs")
    assert res.status_code == 200
    job_ids = {j["job_id"] for j in res.json()["jobs"]}
    assert "pdf_alice_001" in job_ids
    assert "pdf_bob_001" not in job_ids


async def test_pdf_jobs_list_swap_user(client, tmp_workspace, as_user):
    """Same workspace, different caller → different listing."""
    make_user_job(tmp_workspace, "alice", "pdf_alice_001")
    make_user_job(tmp_workspace, "bob", "pdf_bob_001")

    as_user("bob")
    res = await client.get("/api/pdf-translate/jobs")
    job_ids = {j["job_id"] for j in res.json()["jobs"]}
    assert "pdf_bob_001" in job_ids
    assert "pdf_alice_001" not in job_ids


async def test_pdf_jobs_list_admin_includes_legacy(
    client, tmp_workspace, as_user
):
    """Admin sees their own per-user jobs PLUS legacy entries."""
    make_legacy_job(tmp_workspace, "pdf_legacy_001")
    make_user_job(tmp_workspace, admin_username(), "pdf_admin_001")
    # Other user's job should remain invisible — admin's listing is not "see-all"
    make_user_job(tmp_workspace, "alice", "pdf_alice_001")

    as_user(admin_username())
    res = await client.get("/api/pdf-translate/jobs")
    job_ids = {j["job_id"] for j in res.json()["jobs"]}
    assert "pdf_admin_001" in job_ids
    assert "pdf_legacy_001" in job_ids
    assert "pdf_alice_001" not in job_ids


async def test_pdf_jobs_list_non_admin_no_legacy(
    client, tmp_workspace, as_user
):
    """Non-admin must NOT see legacy jobs even if their dir is empty."""
    make_legacy_job(tmp_workspace, "pdf_legacy_001")

    as_user("alice")
    res = await client.get("/api/pdf-translate/jobs")
    job_ids = {j["job_id"] for j in res.json()["jobs"]}
    assert "pdf_legacy_001" not in job_ids


# ── /api/pdf-translate/{job_id}/* — cross-user 403 ────────────────────────────

async def test_pdf_status_cross_user_forbidden(
    client, tmp_workspace, as_user
):
    """Bob asking for alice's job status → 403."""
    make_user_job(tmp_workspace, "alice", "pdf_alice_secret")

    as_user("bob")
    res = await client.get("/api/pdf-translate/pdf_alice_secret/status")
    assert res.status_code == 403


async def test_pdf_original_cross_user_forbidden(
    client, tmp_workspace, as_user
):
    """Cross-user download of original.pdf → 403."""
    make_user_job(tmp_workspace, "alice", "pdf_alice_orig")

    as_user("bob")
    res = await client.get("/api/pdf-translate/pdf_alice_orig/original")
    assert res.status_code == 403


async def test_pdf_translated_cross_user_forbidden(
    client, tmp_workspace, as_user
):
    """Cross-user download of translated.pdf → 403."""
    make_user_job(tmp_workspace, "alice", "pdf_alice_trans")

    as_user("bob")
    res = await client.get("/api/pdf-translate/pdf_alice_trans/translated")
    assert res.status_code == 403


async def test_pdf_owner_can_access_own(
    client, tmp_workspace, as_user
):
    """Owner accessing their own job → 200."""
    make_user_job(tmp_workspace, "alice", "pdf_alice_own")

    as_user("alice")
    res = await client.get("/api/pdf-translate/pdf_alice_own/status")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "done"


async def test_pdf_admin_no_403_on_other_users_job(
    client, tmp_workspace, as_user
):
    """Admin requesting another user's job must NOT raise 403.

    The DB ownership check passes for admin (`_is_admin` bypass). Path
    resolution still constrains what files admin can actually read — so
    this returns 404 on download endpoints, but never 403. The /status
    endpoint uses `must_exist=False` and falls back to the admin's empty
    per-user dir, returning status="unknown" without 403.
    """
    make_user_job(tmp_workspace, "alice", "pdf_alice_priv")
    # DO NOT overwrite the DB owner — alice must remain the owner.

    as_user(admin_username())
    res = await client.get("/api/pdf-translate/pdf_alice_priv/status")
    assert res.status_code != 403


async def test_pdf_admin_can_read_legacy_job(
    client, tmp_workspace, as_user
):
    """Admin can fetch /original from a legacy unowned job (allow_legacy)."""
    make_legacy_job(tmp_workspace, "pdf_legacy_xyz", status="done")

    as_user(admin_username())
    res = await client.get("/api/pdf-translate/pdf_legacy_xyz/original")
    assert res.status_code == 200
    assert res.headers.get("content-type", "").startswith("application/pdf")


async def test_pdf_non_admin_cannot_read_legacy_job(
    client, tmp_workspace, as_user
):
    """Regular user requesting a legacy job → 404 (no allow_legacy)."""
    make_legacy_job(tmp_workspace, "pdf_legacy_abc", status="done")

    as_user("alice")
    res = await client.get("/api/pdf-translate/pdf_legacy_abc/original")
    assert res.status_code == 404


# ── 404 vs 403 distinction ────────────────────────────────────────────────────

async def test_pdf_truly_missing_job_returns_404(
    client, tmp_workspace, as_user
):
    """Job that doesn't exist anywhere → 404 (not 403)."""
    as_user("alice")
    res = await client.get("/api/pdf-translate/pdf_does_not_exist/original")
    assert res.status_code == 404


# ── /api/jobs (LaTeX listing) ─────────────────────────────────────────────────

async def test_latex_jobs_list_only_callers_jobs(
    client, tmp_workspace, as_user
):
    """LaTeX listing also enforces per-user isolation."""
    make_user_job(
        tmp_workspace, "alice", "alice_2401_00001",
        source_type="latex",
    )
    make_user_job(
        tmp_workspace, "bob", "bob_2401_99999",
        source_type="latex",
    )

    as_user("alice")
    res = await client.get("/api/jobs")
    assert res.status_code == 200
    job_ids = {j["job_id"] for j in res.json()["jobs"]}
    assert "alice_2401_00001" in job_ids
    assert "bob_2401_99999" not in job_ids


async def test_latex_jobs_admin_sees_legacy(
    client, tmp_workspace, as_user
):
    """Admin's /api/jobs listing includes legacy unowned jobs."""
    make_legacy_job(
        tmp_workspace, "legacy_2310_12345",
        source_type="latex",
    )
    make_user_job(
        tmp_workspace, admin_username(), "admin_paper_001",
        source_type="latex",
    )

    as_user(admin_username())
    res = await client.get("/api/jobs")
    job_ids = {j["job_id"] for j in res.json()["jobs"]}
    assert "legacy_2310_12345" in job_ids
    assert "admin_paper_001" in job_ids


# ── Auth missing ──────────────────────────────────────────────────────────────

async def test_pdf_jobs_unauthenticated_returns_401(
    client, tmp_workspace, as_user
):
    """When current_username returns None, routes must raise 401."""
    as_user(None)
    res = await client.get("/api/pdf-translate/jobs")
    assert res.status_code == 401


async def test_latex_jobs_unauthenticated_returns_401(
    client, tmp_workspace, as_user
):
    """LaTeX listing requires auth too."""
    as_user(None)
    res = await client.get("/api/jobs")
    assert res.status_code == 401


# ── user_paths helpers (pure unit tests) ──────────────────────────────────────

class TestSafeUsername:
    """`safe_username` must produce filesystem-safe segments."""

    def test_simple_alphanumeric_unchanged(self):
        from app.user_paths import safe_username
        assert safe_username("alice") == "alice"

    def test_hyphen_underscore_kept(self):
        from app.user_paths import safe_username
        assert safe_username("a_b-c") == "a_b-c"

    def test_special_chars_replaced(self):
        from app.user_paths import safe_username
        assert safe_username("alice@host.com") == "alice_host_com"

    def test_path_traversal_neutralized(self):
        from app.user_paths import safe_username
        # "../etc/passwd" must not produce a traversable path
        out = safe_username("../etc/passwd")
        assert ".." not in out
        assert "/" not in out
        assert "\\" not in out

    def test_empty_returns_anon(self):
        from app.user_paths import safe_username
        assert safe_username("") == "_anon"
        assert safe_username(None) == "_anon"

    def test_whitespace_only_returns_anon(self):
        from app.user_paths import safe_username
        # Leading/trailing whitespace → stripped, then sanitized to _anon
        assert safe_username("   ") == "_anon"

    def test_length_capped(self):
        from app.user_paths import safe_username
        long = "a" * 200
        assert len(safe_username(long)) == 64


class TestFindJobPath:
    """`find_job_path` lookup order: per-user dir → legacy (if allowed)."""

    def test_per_user_dir_returned(self, tmp_path):
        from app.user_paths import find_job_path
        ws = str(tmp_path)
        target = os.path.join(ws, "users", "alice", "jobs", "job_x")
        os.makedirs(target)

        result = find_job_path(ws, "job_x", "alice")
        assert result == target

    def test_legacy_returned_only_when_allowed(self, tmp_path):
        from app.user_paths import find_job_path
        ws = str(tmp_path)
        legacy = os.path.join(ws, "jobs", "job_legacy")
        os.makedirs(legacy)

        # Without allow_legacy → not found
        assert find_job_path(ws, "job_legacy", "alice") is None
        # With allow_legacy (admin) → found
        assert find_job_path(
            ws, "job_legacy", "alice", allow_legacy=True
        ) == legacy

    def test_per_user_takes_precedence_over_legacy(self, tmp_path):
        from app.user_paths import find_job_path
        ws = str(tmp_path)
        per_user = os.path.join(ws, "users", "trucnb", "jobs", "job_dup")
        legacy = os.path.join(ws, "jobs", "job_dup")
        os.makedirs(per_user)
        os.makedirs(legacy)

        result = find_job_path(ws, "job_dup", "trucnb", allow_legacy=True)
        assert result == per_user

    def test_missing_returns_none(self, tmp_path):
        from app.user_paths import find_job_path
        assert find_job_path(str(tmp_path), "no_such", "alice") is None


# ── DB ownership functions ───────────────────────────────────────────────────

class TestJobOwnership:
    """`get_jobs_for_user` and `get_job_owner` enforce DB-level isolation."""

    def test_get_jobs_for_user_filters(self, tmp_path, monkeypatch):
        # SQLAlchemy engine is module-level → rows from other tests may persist.
        # We assert filter behavior (alice sees her own, never sees bob's),
        # not strict equality, to stay robust against shared-engine leakage.
        import app.database as db
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "h.db"))
        db.init_db()

        db.upsert_job("j_alice_unique_filter", username="alice", status="done")
        db.upsert_job("j_bob_unique_filter", username="bob", status="done")

        alice_ids = {r["job_id"] for r in db.get_jobs_for_user("alice")}
        bob_ids = {r["job_id"] for r in db.get_jobs_for_user("bob")}
        assert "j_alice_unique_filter" in alice_ids
        assert "j_bob_unique_filter" not in alice_ids
        assert "j_bob_unique_filter" in bob_ids
        assert "j_alice_unique_filter" not in bob_ids

    def test_get_jobs_for_user_with_legacy(self, tmp_path, monkeypatch):
        import app.database as db
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "h.db"))
        db.init_db()

        db.upsert_job("j_admin", username="trucnb", status="done")
        db.upsert_job("j_legacy", status="done")  # username left NULL

        admin_jobs = db.get_jobs_for_user("trucnb", include_unowned=True)
        ids = {r["job_id"] for r in admin_jobs}
        assert "j_admin" in ids
        assert "j_legacy" in ids

        # Without include_unowned, legacy is hidden
        admin_jobs_strict = db.get_jobs_for_user("trucnb")
        assert "j_legacy" not in {r["job_id"] for r in admin_jobs_strict}

    def test_get_job_owner_returns_username(self, tmp_path, monkeypatch):
        import app.database as db
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "h.db"))
        db.init_db()

        db.upsert_job("j_alice", username="alice")
        assert db.get_job_owner("j_alice") == "alice"
        assert db.get_job_owner("j_missing") is None
