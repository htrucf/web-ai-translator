"""Tests cho các upload endpoint mở rộng (LaTeX / text / HTML / unified dispatcher).

Routes covered:
  GET  /api/translate/supported-formats   — danh sách định dạng public
  POST /api/translate/upload              — LaTeX (.tex / .tar.gz / .tgz / .zip)
  POST /api/translate/upload-text         — .txt / .md / .markdown
  POST /api/translate/upload-html         — .html / .htm
  POST /api/documents/upload              — auto-detect ext rồi dispatch

Cả pipeline LaTeX dispatcher và PDF manager đều được mock — không spawn subprocess.
Bypass-auth fixture set current_username="test_admin" → các artifact được kiểm tra
theo layout per-user `workspace/users/test_admin/jobs/...`.
"""

import io
import json
import os
import tarfile
import zipfile

import pytest
from unittest.mock import patch, MagicMock


pytestmark = pytest.mark.asyncio

# Patch targets — chặn subprocess spawn
_LATEX_DISPATCHER = "app.main.get_dispatcher"
_LATEX_FALLBACK = "app.main.pipeline_manager"
_PDF_MANAGER_START = "app.pdf.routes._manager.start"


def _user_job_dir(workspace: str, job_id: str) -> str:
    """Helper: per-user job dir cho test_admin (matches bypass_auth)."""
    return os.path.join(workspace, "users", "test_admin", "jobs", job_id)


def _make_dispatcher_mock() -> MagicMock:
    """Mock dispatcher trả về object có .start_latex() là no-op."""
    disp = MagicMock()
    disp.start_latex = MagicMock(return_value=None)
    return disp


# ──────────────────────────────────────────────────────────────────────────────
# /api/translate/supported-formats
# ──────────────────────────────────────────────────────────────────────────────

async def test_supported_formats_public(client):
    """Endpoint public — không cần auth, trả về danh sách 4 kind."""
    res = await client.get("/api/translate/supported-formats")
    assert res.status_code == 200
    data = res.json()
    assert "formats" in data
    kinds = {f["kind"] for f in data["formats"]}
    assert kinds == {"pdf", "latex", "text", "html"}
    assert data["max_size_mb"] == 50


async def test_supported_formats_contains_endpoints(client):
    """Mỗi entry phải có endpoint + extensions list."""
    res = await client.get("/api/translate/supported-formats")
    formats = res.json()["formats"]
    for f in formats:
        assert f["endpoint"].startswith("/api/")
        assert isinstance(f["exts"], list) and len(f["exts"]) >= 1


# ──────────────────────────────────────────────────────────────────────────────
# /api/translate/upload — LaTeX single .tex
# ──────────────────────────────────────────────────────────────────────────────

_SAMPLE_TEX = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Hello world. This is a sample document.\n"
    "\\end{document}\n"
).encode("utf-8")


async def test_upload_latex_no_file(client):
    """Thiếu file → 422 (FastAPI validation)."""
    res = await client.post("/api/translate/upload")
    assert res.status_code == 422


async def test_upload_latex_unsupported_ext(client):
    """Ext không hỗ trợ → 400."""
    res = await client.post(
        "/api/translate/upload",
        files={"file": ("foo.docx", b"PK...", "application/octet-stream")},
    )
    assert res.status_code == 400


async def test_upload_latex_empty_file(client):
    """File rỗng → 400."""
    res = await client.post(
        "/api/translate/upload",
        files={"file": ("main.tex", b"", "text/plain")},
    )
    assert res.status_code == 400


async def test_upload_latex_single_tex(client, tmp_workspace):
    """Upload .tex → 200, progress.json + source/main.tex được tạo."""
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload",
            files={"file": ("paper.tex", _SAMPLE_TEX, "text/x-tex")},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"].startswith("tex_")
    assert data["status"] == "translating"

    job_dir = _user_job_dir(tmp_workspace, data["job_id"])
    assert os.path.exists(os.path.join(job_dir, "progress.json"))
    assert os.path.exists(
        os.path.join(job_dir, "source_extracted", "source", "main.tex")
    )


async def test_upload_latex_targz_archive(client, tmp_workspace):
    """Upload .tar.gz chứa main.tex → 200, archive được giải nén."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="main.tex")
        info.size = len(_SAMPLE_TEX)
        tar.addfile(info, io.BytesIO(_SAMPLE_TEX))
    archive_bytes = buf.getvalue()

    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload",
            files={"file": ("project.tar.gz", archive_bytes, "application/gzip")},
        )
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert job_id.startswith("tex_")
    job_dir = _user_job_dir(tmp_workspace, job_id)
    assert os.path.exists(
        os.path.join(job_dir, "source_extracted", "source", "main.tex")
    )


async def test_upload_latex_zip_archive(client, tmp_workspace):
    """Upload .zip (Overleaf export style) → 200, archive được giải nén."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("main.tex", _SAMPLE_TEX)
    zip_bytes = buf.getvalue()

    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload",
            files={"file": ("overleaf.zip", zip_bytes, "application/zip")},
        )
    assert res.status_code == 200
    job_id = res.json()["job_id"]
    assert job_id.startswith("tex_")
    job_dir = _user_job_dir(tmp_workspace, job_id)
    assert os.path.exists(
        os.path.join(job_dir, "source_extracted", "source", "main.tex")
    )


async def test_upload_latex_malicious_zip_rejected(client, tmp_workspace):
    """ZIP chứa entry vượt thư mục đích (zip-slip) → 400."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("../escape.tex", _SAMPLE_TEX)
    zip_bytes = buf.getvalue()

    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload",
            files={"file": ("bad.zip", zip_bytes, "application/zip")},
        )
    assert res.status_code == 400
    assert "không an toàn" in res.json()["detail"].lower() or "unsafe" in res.json()["detail"].lower()


async def test_upload_latex_already_done(client, tmp_workspace):
    """Tải lại .tex đã có translated.pdf → status=already_done."""
    # 1) Upload lần đầu
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res1 = await client.post(
            "/api/translate/upload",
            files={"file": ("dup.tex", _SAMPLE_TEX, "text/x-tex")},
        )
    job_id = res1.json()["job_id"]

    # 2) Tạo translated.pdf giả lập đã dịch xong
    job_dir = _user_job_dir(tmp_workspace, job_id)
    out_dir = os.path.join(job_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "translated.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 fake")

    # 3) Upload lại
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res2 = await client.post(
            "/api/translate/upload",
            files={"file": ("dup.tex", _SAMPLE_TEX, "text/x-tex")},
        )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "already_done"
    assert "translated_pdf_url" in data2


async def test_upload_latex_force_resets(client, tmp_workspace):
    """force=True khi đã có output → output cũ được rename, dịch lại từ đầu."""
    # 1) Upload + tạo translated.pdf
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res1 = await client.post(
            "/api/translate/upload",
            files={"file": ("redo.tex", _SAMPLE_TEX, "text/x-tex")},
        )
    job_id = res1.json()["job_id"]
    job_dir = _user_job_dir(tmp_workspace, job_id)
    out_dir = os.path.join(job_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "translated.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 old")

    # 2) Force re-translate
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res2 = await client.post(
            "/api/translate/upload",
            data={"force": "true"},
            files={"file": ("redo.tex", _SAMPLE_TEX, "text/x-tex")},
        )
    assert res2.status_code == 200
    assert res2.json()["status"] == "translating"
    # Output cũ đã bị rename → không còn `output/` mặc định
    archived = [d for d in os.listdir(job_dir) if d.startswith("output_v")]
    assert archived, "force=true phải rename output/ cũ thành output_v<ts>/"


# ──────────────────────────────────────────────────────────────────────────────
# /api/translate/upload-text — txt / md
# ──────────────────────────────────────────────────────────────────────────────

async def test_upload_text_unsupported(client):
    """File .pdf vào endpoint text → 400."""
    res = await client.post(
        "/api/translate/upload-text",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert res.status_code == 400


async def test_upload_text_empty(client):
    """File rỗng → 400."""
    res = await client.post(
        "/api/translate/upload-text",
        files={"file": ("blank.txt", b"", "text/plain")},
    )
    assert res.status_code == 400


async def test_upload_text_txt(client, tmp_workspace):
    """Upload .txt → 200, original.txt + source/main.tex được tạo."""
    body = b"Hello.\n\nThis is a paragraph.\n"
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload-text",
            files={"file": ("notes.txt", body, "text/plain")},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"].startswith("text_")
    assert data["uploaded_kind"] == "txt"

    job_dir = _user_job_dir(tmp_workspace, data["job_id"])
    assert os.path.exists(os.path.join(job_dir, "progress.json"))
    assert os.path.exists(
        os.path.join(job_dir, "source_extracted", "source", "main.tex")
    )


async def test_upload_text_markdown(client, tmp_workspace):
    """Upload .md → 200, kind=md, original.md được lưu."""
    md = b"# Title\n\nA paragraph with **bold** and *italic*.\n\n- one\n- two\n"
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload-text",
            files={"file": ("readme.md", md, "text/markdown")},
            data={"title": "Demo"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["uploaded_kind"] == "md"

    job_dir = _user_job_dir(tmp_workspace, data["job_id"])
    # progress.json phải lưu title người dùng cung cấp
    with open(os.path.join(job_dir, "progress.json"), encoding="utf-8") as f:
        prog = json.load(f)
    assert prog["title"] == "Demo"
    assert prog["source_type"] == "latex"  # đã convert sang LaTeX


# ──────────────────────────────────────────────────────────────────────────────
# /api/translate/upload-html
# ──────────────────────────────────────────────────────────────────────────────

async def test_upload_html_unsupported(client):
    """File .txt vào endpoint HTML → 400."""
    res = await client.post(
        "/api/translate/upload-html",
        files={"file": ("doc.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 400


async def test_upload_html_empty(client):
    """File rỗng → 400."""
    res = await client.post(
        "/api/translate/upload-html",
        files={"file": ("blank.html", b"", "text/html")},
    )
    assert res.status_code == 400


async def test_upload_html_basic(client, tmp_workspace):
    """Upload .html → 200, original.html lưu + title được trích xuất."""
    html = (
        b"<!doctype html><html><head><title>Sample Article</title></head>"
        b"<body><h1>Heading</h1><p>This is a <strong>bold</strong> paragraph.</p>"
        b"<ul><li>One</li><li>Two</li></ul></body></html>"
    )
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload-html",
            files={"file": ("article.html", html, "text/html")},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"].startswith("html_")
    assert data["uploaded_kind"] == "html"
    # Title được derive từ <title> nếu không truyền tham số `title`
    assert data["title"] == "Sample Article"

    job_dir = _user_job_dir(tmp_workspace, data["job_id"])
    assert os.path.exists(os.path.join(job_dir, "original.html"))
    assert os.path.exists(
        os.path.join(job_dir, "source_extracted", "source", "main.tex")
    )


async def test_upload_html_user_title_overrides_tag(client, tmp_workspace):
    """Tham số title từ form ưu tiên hơn thẻ <title>."""
    html = b"<html><head><title>From Tag</title></head><body><p>x</p></body></html>"
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/translate/upload-html",
            files={"file": ("a.html", html, "text/html")},
            data={"title": "From User"},
        )
    assert res.status_code == 200
    assert res.json()["title"] == "From User"


# ──────────────────────────────────────────────────────────────────────────────
# /api/documents/upload — unified dispatcher
# ──────────────────────────────────────────────────────────────────────────────

async def test_unified_upload_unknown_ext(client):
    """File .docx → 400 (định dạng không hỗ trợ)."""
    res = await client.post(
        "/api/documents/upload",
        files={"file": ("a.docx", b"PK...", "application/octet-stream")},
    )
    assert res.status_code == 400


async def test_unified_upload_routes_latex(client, tmp_workspace):
    """.tex → kind=latex, job_id prefix tex_."""
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/documents/upload",
            files={"file": ("paper.tex", _SAMPLE_TEX, "text/x-tex")},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "latex"
    assert data["job_id"].startswith("tex_")


async def test_unified_upload_routes_text(client, tmp_workspace):
    """.md → kind=text."""
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/documents/upload",
            files={"file": ("note.md", b"# Hi\n\nbody.", "text/markdown")},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "text"
    assert data["job_id"].startswith("text_")


async def test_unified_upload_routes_html(client, tmp_workspace):
    """.html → kind=html."""
    html = b"<html><body><p>hello</p></body></html>"
    with patch(_LATEX_DISPATCHER, return_value=_make_dispatcher_mock()):
        res = await client.post(
            "/api/documents/upload",
            files={"file": ("page.html", html, "text/html")},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "html"
    assert data["job_id"].startswith("html_")


async def test_unified_upload_routes_pdf(client, tmp_workspace, sample_pdf_bytes):
    """.pdf → kind=pdf, job_id prefix pdf_, dùng pipeline PDF (mock _manager.start)."""
    with patch(_PDF_MANAGER_START):
        res = await client.post(
            "/api/documents/upload",
            files={"file": ("doc.pdf", sample_pdf_bytes, "application/pdf")},
            data={"mode": "standard"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "pdf"
    assert data["job_id"].startswith("pdf_")
