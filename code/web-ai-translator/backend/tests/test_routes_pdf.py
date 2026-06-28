"""Tests for /api/pdf-translate/* routes.

What is tested here (no real pipeline is started):
  POST /upload          — file validation, duplicate detection, job creation
  POST /start           — resume/force-restart an existing job
  GET  /{job_id}/status — progress.json read-back
  POST /{job_id}/cancel — no-op cancel
  GET  /{job_id}/quality    — reads quality from progress.json
  GET  /{job_id}/glossary   — returns terms dict
  PUT  /{job_id}/glossary   — persists term updates
  GET  /{job_id}/original   — serves original.pdf
  GET  /{job_id}/translated — serves translated.pdf
  GET  /jobs            — lists all pdf jobs

The PipelineManager._run_subprocess is patched so no real process spawns.
"""

import io
import json
import os

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from tests.conftest import make_job


pytestmark = pytest.mark.asyncio

# Patch target for the manager that spawns subprocesses
_MANAGER_START = "app.pdf.routes._manager.start"


# ── /upload ───────────────────────────────────────────────────────────────────

async def test_upload_no_file(client):
    """Missing file → 422."""
    res = await client.post("/api/pdf-translate/upload")
    assert res.status_code == 422


async def test_upload_non_pdf_extension(client):
    """Uploading a .txt file → 400."""
    with patch(_MANAGER_START):
        res = await client.post(
            "/api/pdf-translate/upload",
            files={"file": ("report.txt", b"hello world", "text/plain")},
        )
    assert res.status_code == 400


async def test_upload_valid_pdf(client, sample_pdf_bytes):
    """Uploading a real digital PDF → 200, job_id returned, progress.json created."""
    with patch(_MANAGER_START) as mock_start:
        res = await client.post(
            "/api/pdf-translate/upload",
            files={"file": ("paper.pdf", sample_pdf_bytes, "application/pdf")},
            data={"mode": "standard"},
        )
    assert res.status_code == 200
    data = res.json()
    assert "job_id" in data
    assert data["job_id"].startswith("pdf_")
    assert data["status"] == "started"
    mock_start.assert_called_once()


async def test_upload_creates_progress_json(client, tmp_workspace, sample_pdf_bytes):
    """After upload the job directory contains progress.json."""
    with patch(_MANAGER_START):
        res = await client.post(
            "/api/pdf-translate/upload",
            files={"file": ("myarticle.pdf", sample_pdf_bytes, "application/pdf")},
            data={"mode": "standard"},
        )
    job_id = res.json()["job_id"]
    # Per-user layout: bypass_auth fixture sets current_username -> "test_admin".
    pf = os.path.join(
        tmp_workspace, "users", "test_admin", "jobs", job_id, "progress.json"
    )
    assert os.path.exists(pf)
    with open(pf, encoding="utf-8") as f:
        p = json.load(f)
    assert p["status"] == "pending"
    assert p["source_type"] == "pdf_only"


async def test_upload_duplicate_returns_already_done(
    client, tmp_workspace, sample_pdf_bytes
):
    """Uploading the same PDF when a completed job exists → already_done."""
    # First upload
    with patch(_MANAGER_START):
        res1 = await client.post(
            "/api/pdf-translate/upload",
            files={"file": ("dup_paper.pdf", sample_pdf_bytes, "application/pdf")},
            data={"mode": "standard"},
        )
    job_id = res1.json()["job_id"]

    # Simulate completion (per-user dir matches bypass_auth username)
    job_dir = os.path.join(
        tmp_workspace, "users", "test_admin", "jobs", job_id
    )
    pf = os.path.join(job_dir, "progress.json")
    with open(pf, encoding="utf-8") as f:
        prog = json.load(f)
    prog["status"] = "done"
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(prog, f)
    out_dir = os.path.join(job_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "translated.pdf"), "wb") as f:
        f.write(sample_pdf_bytes)

    # Second upload of same file
    with patch(_MANAGER_START):
        res2 = await client.post(
            "/api/pdf-translate/upload",
            files={"file": ("dup_paper.pdf", sample_pdf_bytes, "application/pdf")},
            data={"mode": "standard"},
        )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "already_done"
    assert "translated_pdf_url" in data2


# ── /start ────────────────────────────────────────────────────────────────────

async def test_start_nonexistent_job(client):
    """Starting a job that has no directory → 404."""
    res = await client.post(
        "/api/pdf-translate/start",
        json={"job_id": "pdf_nonexistent", "force": False},
    )
    assert res.status_code == 404


async def test_start_existing_job(client, tmp_workspace, sample_pdf_bytes):
    """Starting an existing cancelled job → 200, status=started."""
    make_job(
        tmp_workspace,
        "pdf_resume_me",
        status="cancelled",
        with_translated_pdf=False,
        pdf_bytes=sample_pdf_bytes,
    )
    with patch(_MANAGER_START) as mock_start:
        res = await client.post(
            "/api/pdf-translate/start",
            json={"job_id": "pdf_resume_me", "force": False},
        )
    assert res.status_code == 200
    assert res.json()["status"] == "started"
    mock_start.assert_called_once()


async def test_start_force_resets_progress(client, tmp_workspace, sample_pdf_bytes):
    """force=True clears translated_chunks in progress.json."""
    make_job(
        tmp_workspace,
        "pdf_force_redo",
        status="done",
        pdf_bytes=sample_pdf_bytes,
        extra_progress={"translated_chunks": {"0": "old", "1": "old"}},
    )
    with patch(_MANAGER_START):
        await client.post(
            "/api/pdf-translate/start",
            json={"job_id": "pdf_force_redo", "force": True},
        )
    pf = os.path.join(tmp_workspace, "jobs", "pdf_force_redo", "progress.json")
    with open(pf, encoding="utf-8") as f:
        prog = json.load(f)
    # force=True strips translated_chunks entirely (only keeps metadata)
    assert prog.get("translated_chunks", {}) == {}
    assert prog["status"] == "pending"


# ── /{job_id}/status ──────────────────────────────────────────────────────────

async def test_status_unknown_job(client):
    """Job with no progress.json → status=unknown (not 404)."""
    res = await client.get("/api/pdf-translate/no_such_job/status")
    assert res.status_code == 200
    assert res.json()["status"] == "unknown"


async def test_status_done_job(client, tmp_workspace):
    """Completed job → progress_percent=100, translated_pdf_url present."""
    make_job(tmp_workspace, "pdf_done_001", status="done")
    res = await client.get("/api/pdf-translate/pdf_done_001/status")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "done"
    assert data.get("progress_percent") == 100
    assert "translated_pdf_url" in data


async def test_status_translating_job(client, tmp_workspace):
    """Mid-translation job parses chunk counts correctly."""
    make_job(
        tmp_workspace,
        "pdf_mid_001",
        status="translating 8/20",
        with_translated_pdf=False,
    )
    res = await client.get("/api/pdf-translate/pdf_mid_001/status")
    data = res.json()
    assert data["current_chunk"] == 8
    assert data["total_chunks"] == 20
    assert data["progress_percent"] == 40


# ── /{job_id}/cancel ─────────────────────────────────────────────────────────

async def test_cancel_running_job(client, tmp_workspace):
    """Cancel returns 200 and does not crash even if nothing is running."""
    make_job(tmp_workspace, "pdf_cancel_me", status="translating 3/10")
    res = await client.post("/api/pdf-translate/pdf_cancel_me/cancel")
    assert res.status_code == 200


# ── /{job_id}/quality ─────────────────────────────────────────────────────────

async def test_quality_no_report(client, tmp_workspace):
    """No quality field in progress.json → 404."""
    make_job(tmp_workspace, "pdf_no_quality", status="done", with_translated_pdf=True)
    res = await client.get("/api/pdf-translate/pdf_no_quality/quality")
    assert res.status_code == 404


async def test_quality_with_report(client, tmp_workspace):
    """Quality data present → returned with score field."""
    quality_data = {
        "score": 87.5,
        "issue_count": 2,
        "total_blocks": 30,
        "translatable_blocks": 28,
        "translated_blocks": 26,
        "untranslated_blocks": 2,
        "issues_by_severity": {"error": 0, "warning": 2, "info": 0},
        "issues_by_category": {"length": 2},
        "issues": [],
    }
    make_job(
        tmp_workspace,
        "pdf_with_quality",
        status="done",
        extra_progress={"quality": quality_data},
    )
    res = await client.get("/api/pdf-translate/pdf_with_quality/quality")
    assert res.status_code == 200
    data = res.json()
    assert data["score"] == 87.5
    assert data["issue_count"] == 2


# ── /{job_id}/glossary ────────────────────────────────────────────────────────

async def test_glossary_get_empty(client, tmp_workspace):
    """No glossary in progress.json → returns empty terms dict."""
    make_job(tmp_workspace, "pdf_gloss_empty", status="done")
    res = await client.get("/api/pdf-translate/pdf_gloss_empty/glossary")
    assert res.status_code == 200
    data = res.json()
    assert data["terms"] == {}
    assert data["count"] == 0


async def test_glossary_get_with_terms(client, tmp_workspace):
    """Glossary terms in progress.json → returned correctly."""
    gloss = {"terms": {"neural network": "mạng nơ-ron", "gradient": "gradient"}, "enabled": True}
    make_job(
        tmp_workspace,
        "pdf_gloss_full",
        status="done",
        extra_progress={"glossary": gloss},
    )
    res = await client.get("/api/pdf-translate/pdf_gloss_full/glossary")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 2
    assert data["terms"]["neural network"] == "mạng nơ-ron"


async def test_glossary_put_updates_terms(client, tmp_workspace):
    """PUT glossary → terms persisted in progress.json."""
    make_job(tmp_workspace, "pdf_gloss_edit", status="done")
    new_terms = {"transformer": "bộ biến đổi", "attention": "chú ý"}
    res = await client.put(
        "/api/pdf-translate/pdf_gloss_edit/glossary",
        json={"terms": new_terms, "enabled": True},
    )
    assert res.status_code == 200

    pf = os.path.join(tmp_workspace, "jobs", "pdf_gloss_edit", "progress.json")
    with open(pf, encoding="utf-8") as f:
        prog = json.load(f)
    assert prog["glossary"]["terms"] == new_terms


# ── /{job_id}/original & /translated ─────────────────────────────────────────

async def test_original_pdf_not_found(client, tmp_workspace):
    """original.pdf doesn't exist → 404."""
    make_job(tmp_workspace, "pdf_no_orig", status="done", with_original_pdf=False)
    res = await client.get("/api/pdf-translate/pdf_no_orig/original")
    assert res.status_code == 404


async def test_original_pdf_served(client, tmp_workspace, sample_pdf_bytes):
    """original.pdf exists → 200 with application/pdf content-type."""
    make_job(
        tmp_workspace, "pdf_has_orig", status="done", pdf_bytes=sample_pdf_bytes
    )
    res = await client.get("/api/pdf-translate/pdf_has_orig/original")
    assert res.status_code == 200
    assert "application/pdf" in res.headers["content-type"]


async def test_translated_pdf_not_found(client, tmp_workspace):
    """translated.pdf doesn't exist → 404."""
    make_job(
        tmp_workspace, "pdf_no_trans", status="done", with_translated_pdf=False
    )
    res = await client.get("/api/pdf-translate/pdf_no_trans/translated")
    assert res.status_code == 404


async def test_translated_pdf_served(client, tmp_workspace, sample_pdf_bytes):
    """translated.pdf exists → 200."""
    make_job(
        tmp_workspace, "pdf_has_trans", status="done", pdf_bytes=sample_pdf_bytes
    )
    res = await client.get("/api/pdf-translate/pdf_has_trans/translated")
    assert res.status_code == 200
    assert "application/pdf" in res.headers["content-type"]


# ── /jobs ─────────────────────────────────────────────────────────────────────

async def test_jobs_listing(client, tmp_workspace):
    """GET /api/pdf-translate/jobs lists pdf jobs."""
    make_job(tmp_workspace, "pdf_list_001", status="done", title="Paper One")
    make_job(tmp_workspace, "pdf_list_002", status="cancelled", title="Paper Two")
    res = await client.get("/api/pdf-translate/jobs")
    assert res.status_code == 200
    jobs = res.json()["jobs"]
    ids = [j["job_id"] for j in jobs]
    assert "pdf_list_001" in ids
    assert "pdf_list_002" in ids


# ── /{job_id}/judge/web — cross-model web judge ─────────────────────────────
# web_judge.judge_segments_batch drives a real browser, so it is mocked. We
# test only the route plumbing: gating, pair collection, caching, response.

_PAIRS_PROGRESS = {
    "input_chunks": {"0": "This is a sufficiently long English source sentence."},
    "translated_chunks": {"0": "Đây là câu nguồn tiếng Anh đủ dài."},
    "ai_backend": "gemini",
}

_WEB_REPORT = {
    "judge_backend": "chatgpt",
    "translator_backend": "gemini",
    "model": "chatgpt-web",
    "num_judged": 1,
    "avg_score": 92,
    "error_counts": {"accuracy": 1},
    "results": [{"index": 0, "src": "x", "mt": "y", "score_pct": 50, "llm_result": {}}],
}


async def test_web_judge_get_no_report(client, tmp_workspace):
    """No web_judge field → 404."""
    make_job(tmp_workspace, "pdf_wj_none", status="done")
    res = await client.get("/api/pdf-translate/pdf_wj_none/judge/web")
    assert res.status_code == 404


async def test_web_judge_post_runs_and_caches(client, tmp_workspace):
    """POST runs the (mocked) batch, returns the report, and caches it."""
    job_dir = make_job(
        tmp_workspace, "pdf_wj_run", status="done",
        extra_progress=dict(_PAIRS_PROGRESS),
    )
    mock = AsyncMock(return_value=dict(_WEB_REPORT))
    with patch("app.pdf.web_judge.judge_segments_batch", mock):
        res = await client.post(
            "/api/pdf-translate/pdf_wj_run/judge/web",
            json={"judge_backend": "chatgpt", "max_segments": 5},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == "pdf_wj_run"
    assert data["judge_backend"] == "chatgpt"
    assert data["translator_backend"] == "gemini"
    assert data["model"] == "chatgpt-web"

    # The job's persisted translator backend is passed through for cross-model.
    _, kwargs = mock.call_args
    assert kwargs["translator_backend"] == "gemini"
    assert kwargs["judge_backend"] == "chatgpt"

    # Cached into progress.json
    with open(os.path.join(job_dir, "progress.json"), encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["web_judge"]["model"] == "chatgpt-web"


async def test_web_judge_get_returns_cache(client, tmp_workspace):
    """GET after a run returns the cached report."""
    make_job(
        tmp_workspace, "pdf_wj_cached", status="done",
        extra_progress={"web_judge": dict(_WEB_REPORT)},
    )
    res = await client.get("/api/pdf-translate/pdf_wj_cached/judge/web")
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == "pdf_wj_cached"
    assert data["model"] == "chatgpt-web"
    assert data["avg_score"] == 92


async def test_web_judge_post_requires_done_job(client, tmp_workspace):
    """POST on a still-running job → 400 (must be completed first)."""
    make_job(
        tmp_workspace, "pdf_wj_running", status="translating 2/10",
        with_translated_pdf=False, extra_progress=dict(_PAIRS_PROGRESS),
    )
    mock = AsyncMock(return_value=dict(_WEB_REPORT))
    with patch("app.pdf.web_judge.judge_segments_batch", mock):
        res = await client.post(
            "/api/pdf-translate/pdf_wj_running/judge/web", json={},
        )
    assert res.status_code == 400
    mock.assert_not_called()


async def test_web_judge_post_no_pairs(client, tmp_workspace):
    """POST on a done job with no translation pairs → 404."""
    make_job(tmp_workspace, "pdf_wj_empty", status="done")
    mock = AsyncMock(return_value=dict(_WEB_REPORT))
    with patch("app.pdf.web_judge.judge_segments_batch", mock):
        res = await client.post(
            "/api/pdf-translate/pdf_wj_empty/judge/web", json={},
        )
    assert res.status_code == 404
    mock.assert_not_called()
