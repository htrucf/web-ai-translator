"""Shared pytest fixtures for all test files.

Key design decisions:
- Uses httpx.AsyncClient with ASGI transport — no real HTTP server needed.
- The real workspace path (settings.WORKSPACE_DIR) is monkeypatched to a
  pytest tmp_path so tests are fully isolated and leave no side effects.
- Playwright / WebAITranslator / pipeline subprocess are never started in
  any test — routes that would launch them are tested only up to the point
  where they would start the subprocess (the subprocess call itself is mocked).
- A minimal 1-page digital PDF is generated via fitz so PDF-processing tests
  have real content to work with without shipping a binary fixture file.
"""

import asyncio
import json
import os
import shutil

# Provide deterministic built-in admin credentials before app.auth is imported
# by any test or fixture. Production no longer ships defaults, so tests must
# set them explicitly.
os.environ.setdefault("AUTH_USERNAME", "test_admin")
os.environ.setdefault("AUTH_PASSWORD", "test_password")

import pytest
import pytest_asyncio

try:
    import fitz
except ImportError:
    import pymupdf as fitz

from httpx import AsyncClient, ASGITransport


# ── Event loop ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Single event loop shared across the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Workspace isolation ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_workspace(tmp_path, monkeypatch):
    """Redirect every module's WORKSPACE constant to a fresh tmp dir.

    All route modules reference a module-level WORKSPACE string.  We patch
    each one so tests never touch the real workspace.
    Also redirect the SQLite DB to the same tmp dir and call init_db() so
    schema is present for tests that use the database (auth sessions, jobs).
    """
    ws = str(tmp_path / "workspace")
    os.makedirs(os.path.join(ws, "jobs"), exist_ok=True)

    # Patch every module that holds a WORKSPACE constant
    import app.pdf.routes as pdf_routes
    import app.main as main_module

    monkeypatch.setattr(pdf_routes, "WORKSPACE", ws)
    monkeypatch.setattr(main_module, "WORKSPACE_DIR", ws, raising=False)
    # main.py uses module-level WORKSPACE (already computed at import time)
    monkeypatch.setattr(main_module, "WORKSPACE", ws, raising=False)

    # Also patch the settings object used by main
    try:
        from app.config import settings
        monkeypatch.setattr(settings, "WORKSPACE_DIR", ws)
    except Exception:
        pass

    # Redirect the database to a per-test file and create the schema. Without
    # this, auth-related tests would write sessions into the real workspace DB.
    import app.database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", os.path.join(ws, "history.db"))
    db_module.init_db()

    return ws


# ── Auth bypass ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def bypass_auth(monkeypatch):
    """Disable auth middleware so route tests are not blocked by 401.

    Also stubs `current_username` in the modules that import it directly —
    routes use it via `get_owner` to drive per-user job lookups, so we need
    a non-None username for ownership checks to pass.
    `test_user_isolation.py` overrides this fixture with its own per-user
    stub, so individual tests can still assert per-user behavior.
    """
    from app import auth
    monkeypatch.setattr(auth, "validate_token", lambda token: True)
    monkeypatch.setattr(
        auth, "_extract_token", lambda request: "test-token-bypass"
    )

    # Routes import current_username at module load time, so patch each module
    # rather than just the auth source.
    fake_user = lambda _req_or_token: "test_admin"
    monkeypatch.setattr(auth, "current_username", fake_user)
    import app.main as main_mod
    import app.pdf.routes as pdf_routes_mod
    import app.api.history as history_mod
    monkeypatch.setattr(main_mod, "current_username", fake_user)
    monkeypatch.setattr(pdf_routes_mod, "current_username", fake_user)
    monkeypatch.setattr(history_mod, "current_username", fake_user)


# ── ASGI test client ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(tmp_workspace):
    """httpx AsyncClient pointed at the FastAPI app via ASGI transport."""
    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ── Minimal digital PDF fixture ───────────────────────────────────────────────

@pytest.fixture(scope="session")
def sample_pdf_bytes():
    """Return bytes of a minimal 1-page digital PDF with real text content.

    Generated via fitz so we never ship a binary blob in the repo.
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4

    page.insert_text(
        (50, 80),
        "Deep Learning for Natural Language Processing",
        fontsize=18,
    )
    page.insert_text(
        (50, 120),
        "John Smith, Jane Doe",
        fontsize=12,
    )
    page.insert_text(
        (50, 160),
        (
            "Abstract. In this paper we propose a novel neural network architecture "
            "for sequence-to-sequence translation tasks. Our model achieves "
            "state-of-the-art performance on the WMT benchmark with a BLEU score "
            "of 42.3, outperforming existing baseline methods by a significant margin."
        ),
        fontsize=11,
    )
    page.insert_text(
        (50, 260),
        "1  Introduction",
        fontsize=13,
    )
    page.insert_text(
        (50, 290),
        (
            "Machine learning models have demonstrated remarkable capabilities in "
            "natural language understanding tasks. Transformer-based architectures "
            "have become the dominant approach for many benchmarks in recent years."
        ),
        fontsize=11,
    )
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


@pytest.fixture
def sample_pdf_file(tmp_path, sample_pdf_bytes):
    """Write sample_pdf_bytes to a temp file and return the path."""
    path = tmp_path / "sample.pdf"
    path.write_bytes(sample_pdf_bytes)
    return str(path)


# ── Job helpers ───────────────────────────────────────────────────────────────

def make_job(
    ws: str,
    job_id: str,
    source_type: str = "pdf_only",
    status: str = "done",
    title: str = "Test Paper",
    page_count: int = 1,
    with_original_pdf: bool = True,
    with_translated_pdf: bool = True,
    extra_progress: dict | None = None,
    pdf_bytes: bytes | None = None,
) -> str:
    """Create a fake job directory with progress.json and optional PDFs.

    Returns the job directory path.
    """
    job_dir = os.path.join(ws, "jobs", job_id)
    output_dir = os.path.join(job_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    progress = {
        "status": status,
        "source_type": source_type,
        "title": title,
        "page_count": page_count,
        "translated_chunks": {},
    }
    if extra_progress:
        progress.update(extra_progress)

    with open(os.path.join(job_dir, "progress.json"), "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

    # Use supplied bytes or generate a minimal PDF
    _pdf = pdf_bytes or _make_minimal_pdf()

    if with_original_pdf:
        with open(os.path.join(job_dir, "original.pdf"), "wb") as f:
            f.write(_pdf)

    if with_translated_pdf:
        with open(os.path.join(output_dir, "translated.pdf"), "wb") as f:
            f.write(_pdf)

    return job_dir


def _make_minimal_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 100), "Translated content here.")
    data = doc.tobytes()
    doc.close()
    return data


# Expose as a fixture too
@pytest.fixture
def make_job_fixture(tmp_workspace):
    """Fixture wrapper around make_job — passes tmp_workspace automatically."""
    def _make(job_id, **kwargs):
        return make_job(tmp_workspace, job_id, **kwargs)
    return _make
