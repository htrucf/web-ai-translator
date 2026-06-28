"""Tests for top-level main.py routes.

Routes covered:
  GET  /health
  GET  /api/jobs

Playwright / pipeline are never started.
"""

import json
import os

import pytest

from tests.conftest import make_job


pytestmark = pytest.mark.asyncio


# ── /health ───────────────────────────────────────────────────────────────────

async def test_health(client):
    res = await client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data.get("status") == "ok"


# ── /api/jobs ─────────────────────────────────────────────────────────────────

async def test_jobs_empty_workspace(client):
    """Empty workspace → jobs list is empty."""
    res = await client.get("/api/jobs")
    assert res.status_code == 200
    data = res.json()
    assert data["jobs"] == []


async def test_jobs_lists_pdf_job(client, tmp_workspace):
    """A pdf_ job appears with source_type='pdf'."""
    make_job(tmp_workspace, "pdf_sample_paper", source_type="pdf_only", status="done")
    res = await client.get("/api/jobs")
    assert res.status_code == 200
    jobs = res.json()["jobs"]
    assert any(j["job_id"] == "pdf_sample_paper" for j in jobs)
    pdf_job = next(j for j in jobs if j["job_id"] == "pdf_sample_paper")
    assert pdf_job["source_type"] == "pdf"



async def test_jobs_completed_job_has_100_percent(client, tmp_workspace):
    """A done job shows progress_percent=100."""
    make_job(tmp_workspace, "pdf_done_paper", status="done")
    res = await client.get("/api/jobs")
    job = next(j for j in res.json()["jobs"] if j["job_id"] == "pdf_done_paper")
    assert job["progress_percent"] == 100


async def test_jobs_translating_job_has_partial_percent(client, tmp_workspace):
    """A mid-translation job reports correct progress_percent."""
    make_job(
        tmp_workspace,
        "pdf_in_progress",
        status="translating 5/20",
        with_translated_pdf=False,
    )
    res = await client.get("/api/jobs")
    job = next(j for j in res.json()["jobs"] if j["job_id"] == "pdf_in_progress")
    assert job["progress_percent"] == 25


async def test_jobs_title_in_response(client, tmp_workspace):
    """Title stored in progress.json is returned."""
    make_job(
        tmp_workspace,
        "pdf_titled",
        title="My Amazing Paper",
        status="done",
    )
    res = await client.get("/api/jobs")
    job = next(j for j in res.json()["jobs"] if j["job_id"] == "pdf_titled")
    assert job.get("title") == "My Amazing Paper"
