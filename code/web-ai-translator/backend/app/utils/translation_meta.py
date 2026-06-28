"""Translation provenance metadata — used to stamp output PDF/.tex.

Mỗi bản dịch cần 2 indicator (theo yêu cầu DATN):
  (1) PDF metadata / `\\hypersetup`  — vô hình, không phá layout, machine-readable.
  (2) Footer mỏng trên mọi trang     — luôn thấy, có nguồn gốc + tài khoản dịch.

Module này gom mọi thứ về "đây là bản dịch" vào 1 dict + 1 format function cho
từng kiểu output (PDF metadata dict, PDF footer string, LaTeX hypersetup, LaTeX
fancyhdr).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


APP_NAME = "web-ai-translator"
APP_TAG = "DATN — Đại học Bách khoa Hà Nội"


def build_meta(
    *,
    job_id: str,
    source_kind: str,           # "arxiv" | "pdf_upload" | "docx" | "text"
    source_label: str,          # arxiv id "2301.12345" hoặc filename "paper.pdf"
    source_url: str = "",       # https://arxiv.org/abs/... nếu có; rỗng cho upload
    translator_backend: str = "gemini",
    account_email: str = "",    # rỗng nếu không bật scheduler
    title: str = "",            # tiêu đề bài (nếu biết)
) -> dict[str, Any]:
    """Build dict mô tả 1 bản dịch. An toàn để serialize vào progress.json."""
    return {
        "job_id": job_id,
        "source_kind": source_kind,
        "source_label": source_label,
        "source_url": source_url,
        "translator_backend": translator_backend or "unknown",
        "account_email": account_email or "",
        "title": title or "",
        "translated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_name": APP_NAME,
        "app_tag": APP_TAG,
    }


def _account_display(meta: dict) -> str:
    """Cách hiển thị tài khoản cho footer/metadata, tôn trọng privacy.

    Email đầy đủ in vào PDF metadata (machine-readable) — nhưng footer hiện
    dạng rút gọn `u***@gmail.com` để không lộ rõ địa chỉ khi user share PDF.
    """
    email = (meta.get("account_email") or "").strip()
    if not email or "@" not in email:
        return "tài khoản mặc định"
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked = local + "***"
    else:
        masked = local[0] + "***" + local[-1]
    return f"{masked}@{domain}"


# ── PDF (PyMuPDF) ─────────────────────────────────────────────────────────

def format_pdf_metadata(meta: dict) -> dict[str, str]:
    """Dict cho `fitz.Document.set_metadata(...)`.

    Các field PDF chuẩn (PDF 1.7 §14.3.3): Title, Author, Subject, Keywords,
    Creator, Producer, CreationDate, ModDate. Khóa lowercase theo PyMuPDF.
    """
    backend = meta.get("translator_backend", "unknown")
    src = meta.get("source_label") or meta.get("job_id", "")
    src_url = meta.get("source_url", "")
    email = (meta.get("account_email") or "").strip()
    title = meta.get("title") or f"Bản dịch tiếng Việt — {src}"

    subject_parts = [
        f"Bản dịch tự động sang tiếng Việt từ {src}",
    ]
    if src_url:
        subject_parts.append(f"Nguồn: {src_url}")
    if email:
        subject_parts.append(f"Tài khoản dịch: {email}")
    else:
        subject_parts.append("Tài khoản dịch: profile mặc định")

    keywords = ",".join(filter(None, [
        "translation", "vi", "vietnamese",
        f"backend:{backend}",
        f"source:{src}",
        f"account:{email}" if email else "account:default",
        f"app:{APP_NAME}",
    ]))

    return {
        "title": title,
        "author": APP_NAME,
        "subject": " | ".join(subject_parts),
        "keywords": keywords,
        "creator": f"{APP_NAME} — {APP_TAG}",
        "producer": f"{APP_NAME} (backend={backend})",
    }


def format_pdf_footer(meta: dict) -> str:
    """1 dòng footer ngắn cho PDF — phải fit trong ~80 ký tự để không tràn."""
    src = meta.get("source_label") or meta.get("job_id", "")
    src_url = meta.get("source_url", "")
    acct = _account_display(meta)
    parts = [f"Bản dịch tự động bởi {APP_NAME}"]
    if src_url:
        parts.append(f"Nguồn: {src_url}")
    elif src:
        parts.append(f"Nguồn: {src}")
    parts.append(f"Dịch qua: {meta.get('translator_backend','?')} ({acct})")
    return "  •  ".join(parts)


# ── LaTeX ─────────────────────────────────────────────────────────────────

def _latex_escape(text: str) -> str:
    """Escape ký tự LaTeX trong text injected vào hypersetup/fancyhdr."""
    replacements = [
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"), ("%", r"\%"), ("$", r"\$"),
        ("#", r"\#"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
        ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def format_latex_indicator_block(meta: dict) -> str:
    """LaTeX block chứa CẢ hypersetup (tầng 1) + fancyhdr footer (tầng 2).

    Inject vào preamble RIGHT TRƯỚC `\\begin{document}` để chắc chắn override
    `\\pagestyle` do class file/template đặt trước đó.
    """
    backend = _latex_escape(meta.get("translator_backend", "?"))
    src = _latex_escape(meta.get("source_label") or meta.get("job_id", ""))
    src_url_raw = (meta.get("source_url") or "").strip()
    src_url = _latex_escape(src_url_raw)
    title = _latex_escape(meta.get("title") or f"Bản dịch tiếng Việt — {src}")
    acct_display = _latex_escape(_account_display(meta))
    email = (meta.get("account_email") or "").strip()
    keywords = f"translation,vi,backend:{backend},source:{src}"
    if email:
        keywords += f",account:{_latex_escape(email)}"
    subject = f"Bản dịch tự động sang tiếng Việt từ {src}"
    if src_url_raw:
        subject += f" — Nguồn: {src_url}"

    # hypersetup — chỉ set các pdf* field, không bật colorlinks (đã được
    # `_ensure_hyperref` setup riêng).
    hypersetup = (
        "% --- Translation provenance metadata (auto-generated) ---\n"
        "\\hypersetup{\n"
        f"  pdftitle={{{title}}},\n"
        f"  pdfauthor={{{_latex_escape(APP_NAME)}}},\n"
        f"  pdfsubject={{{subject}}},\n"
        f"  pdfkeywords={{{keywords}}},\n"
        f"  pdfcreator={{{_latex_escape(APP_NAME)} — {_latex_escape(APP_TAG)}}},\n"
        f"  pdfproducer={{{_latex_escape(APP_NAME)} (backend={backend})}}\n"
        "}\n"
    )

    # fancyhdr footer — gray small text, mọi trang.
    # Dùng `\AtBeginDocument` để chạy sau khi class file đã chạy xong.
    source_line = (
        f"\\href{{{src_url}}}{{Nguồn: {src_url}}}" if src_url_raw
        else f"Nguồn: {src}"
    )
    footer_text = (
        f"Bản dịch tự động bởi {_latex_escape(APP_NAME)} "
        f"$\\bullet$ {source_line} "
        f"$\\bullet$ Dịch qua {backend} ({acct_display})"
    )
    fancy = (
        "\\usepackage{fancyhdr}\n"
        "\\usepackage{xcolor}\n"
        "\\AtBeginDocument{%\n"
        "  \\pagestyle{fancy}%\n"
        "  \\fancyhf{}%\n"
        "  \\renewcommand{\\headrulewidth}{0pt}%\n"
        "  \\renewcommand{\\footrulewidth}{0.2pt}%\n"
        "  \\fancyfoot[L]{\\scriptsize\\textcolor{gray}{"
        + footer_text +
        "}}%\n"
        "  \\fancyfoot[R]{\\scriptsize\\textcolor{gray}{\\thepage}}%\n"
        "  \\fancypagestyle{plain}{%\n"
        "    \\fancyhf{}%\n"
        "    \\renewcommand{\\headrulewidth}{0pt}%\n"
        "    \\renewcommand{\\footrulewidth}{0.2pt}%\n"
        "    \\fancyfoot[L]{\\scriptsize\\textcolor{gray}{"
        + footer_text +
        "}}%\n"
        "    \\fancyfoot[R]{\\scriptsize\\textcolor{gray}{\\thepage}}%\n"
        "  }%\n"
        "}\n"
        "% --- End translation provenance ---\n"
    )

    return hypersetup + fancy
