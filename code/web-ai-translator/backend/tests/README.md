# Test Suite — web-ai-translator backend

## Overview

All tests live in `backend/tests/`. They use **pytest** with the **httpx ASGI transport** — no real HTTP server is started, and no browser/Playwright process is ever launched. Tests run entirely in-process and are safe to run offline.

---

## File map

| File | What it tests |
|------|---------------|
| `conftest.py` | Shared fixtures: ASGI client, workspace isolation, sample PDF, job builder |
| `test_routes_main.py` | Top-level routes: `/health`, `/api/jobs` |
| `test_routes_pdf.py` | PDF pipeline routes: upload, start, status, cancel, quality, glossary, file serving |
| `test_routes_ieee.py` | IEEE Xplore routes: session, login, search, translate, status, quality |
| `test_routes_sd.py` | ScienceDirect routes: session, search, article, translate, status, quality |
| `test_pdf_processor.py` | Pure PDF logic: extract blocks, chunk, rebuild, get_pdf_info |
| `test_pdf_quality.py` | Heuristic quality scorer: scoring, penalties, issue categories, find_fixable_blocks |
| `test_pdf_glossary.py` | Glossary module: parse, filter, format, merge |

---

## Setup

```bash
cd web-ai-translator/backend

# Install test dependencies (pytest + httpx are the only additions)
pip install pytest pytest-asyncio httpx

# Or add to requirements.txt and install everything:
pip install -r requirements.txt
```

`pytest`, `pytest-asyncio`, and `httpx` must be present. Everything else (`fastapi`, `fitz`, etc.) is already in `requirements.txt`.

---

## Running tests

```bash
# Run the full suite
cd web-ai-translator/backend
pytest

# Run a single file
pytest tests/test_routes_pdf.py

# Run a single test by name
pytest tests/test_routes_pdf.py::test_upload_valid_pdf

# Run with verbose output (already set in pytest.ini, but can be added again)
pytest -v

# Stop on first failure
pytest -x

# Show full traceback on failure
pytest --tb=long

# Run only tests matching a keyword
pytest -k "glossary"
pytest -k "ieee and status"
```

---

## What is mocked vs real

### Mocked (never starts a real process)
| Component | Why mocked |
|-----------|-----------|
| `WebAITranslator` / Playwright | Would open a real browser; slow, requires Chromium |
| `LibopacSession.login/logout` | Would make real HTTP requests to `libopac.hust.edu.vn` |
| `search_ieee`, `search_sciencedirect` | External HTTP; flaky in CI |
| `PipelineManager.start` / `SDPipelineManager.start` | Would spawn a subprocess |

Mocking is done with `unittest.mock.patch` / `AsyncMock` inline in each test — no global mocking that could hide real bugs.

### Real (actually executes)
| Component | Notes |
|-----------|-------|
| FastAPI app + all routers | Full ASGI stack, all middleware |
| `app/pdf/processor.py` | Real PyMuPDF calls on in-memory PDFs |
| `app/pdf/quality.py` | Pure Python, no deps |
| `app/pdf/glossary.py` | Pure Python, no deps |
| File I/O (progress.json, PDFs) | Written to `tmp_path`, never touches real workspace |

---

## Workspace isolation

Every test automatically gets a fresh temporary workspace via the `tmp_workspace` fixture in `conftest.py`. This fixture:

1. Creates a `workspace/jobs/` directory inside pytest's `tmp_path`
2. Monkeypatches the `WORKSPACE` constant in every route module
3. Tears everything down after the test

This means tests never read from or write to `backend/workspace/` and can run safely alongside a running dev server.

---

## Adding new tests

### New route test
1. Add a function `test_<what>` to the appropriate `test_routes_*.py` file.
2. Use the `client` fixture for HTTP calls.
3. Use `make_job_fixture` (or `make_job` directly) to set up fake job state.
4. Patch any external calls (browser, HTTP) with `unittest.mock.patch`.

```python
async def test_my_new_endpoint(client, make_job_fixture):
    make_job_fixture("pdf_mytest", status="done")
    res = await client.get("/api/pdf-translate/pdf_mytest/status")
    assert res.status_code == 200
```

### New unit test (no routes)
Add a function to the appropriate `test_pdf_*.py` file. No fixtures needed beyond standard pytest ones — just call the function under test directly.

```python
def test_my_quality_edge_case():
    blocks = [_block("some english text", translated="")]
    report = check_translation_quality(blocks)
    assert report.score < 100
```

### New test file
1. Create `tests/test_<module>.py`
2. Add a one-line entry to the table in this README
3. Import fixtures from `conftest` as needed — they're available automatically

---

## Common failures and fixes

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError: app.*` | Not running from `backend/` directory | `cd web-ai-translator/backend` before running pytest |
| `RuntimeError: no running event loop` | asyncio_mode not set | Check `pytest.ini` has `asyncio_mode = auto` |
| `AssertionError` on workspace path | Monkeypatch missed a module | Add the module to `tmp_workspace` fixture in `conftest.py` |
| `404` on a route that should exist | Route prefix mismatch | Check `router = APIRouter(prefix=...)` in the routes file |
| Test hits real `libopac.hust.edu.vn` | Missing mock | Wrap the test body with `patch("app.libopac.session.LibopacSession.login", ...)` |
