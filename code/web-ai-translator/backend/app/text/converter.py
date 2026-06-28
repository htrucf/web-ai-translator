"""Convert plain-text / Markdown sang LaTeX để tái sử dụng pipeline LaTeX hiện có.

Triết lý:
- Không cần full Markdown spec — chỉ cover heading, bold/italic, list, code block,
  inline code, link. Bất cứ thứ gì không match đều fall back về text thường.
- Output LaTeX dùng article class + UTF-8 (xelatex) — tương thích với
  ``latex_processor.compile_to_pdf``.
- Escape các ký tự đặc biệt của LaTeX để input ngẫu nhiên không phá compile.
"""

from __future__ import annotations

import re
from typing import Iterable


SUPPORTED_TEXT_EXTS: tuple[str, ...] = (".txt", ".md", ".markdown")


def text_ext(filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith(".md") or lower.endswith(".markdown"):
        return "md"
    if lower.endswith(".txt"):
        return "txt"
    return None


# ── LaTeX escape ────────────────────────────────────────────────────────────

# Order matters: backslash first, then the rest. We DO NOT escape backslash
# inside markdown_to_latex's emitted commands — we escape inside `_escape_text`
# which only runs on plain text segments.
_LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape_text(text: str) -> str:
    """Escape ký tự LaTeX đặc biệt trong text thường (chưa qua markdown)."""
    # backslash first to avoid double-escape
    out = text.replace("\\", _LATEX_ESCAPES["\\"])
    for ch, repl in _LATEX_ESCAPES.items():
        if ch == "\\":
            continue
        out = out.replace(ch, repl)
    return out


# ── Markdown → LaTeX (basic subset) ─────────────────────────────────────────

_CODE_FENCE = re.compile(r"^```([\w+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_HRULE = re.compile(r"^[-*_]{3,}\s*$")
_UL_ITEM = re.compile(r"^(\s*)[-*+]\s+(.+)$")
_OL_ITEM = re.compile(r"^(\s*)(\d+)[.)]\s+(.+)$")

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+)(?<!\*)\*(?!\*)|(?<!_)_(?!_)([^_\n]+)(?<!_)_(?!_)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

_HEADING_CMDS = ["section", "subsection", "subsubsection", "paragraph", "subparagraph", "subparagraph"]


def _apply_inline(text: str) -> str:
    """Apply inline Markdown → LaTeX. Text must already be LaTeX-escaped EXCEPT
    we handle inline code specially (it stays verbatim-ish via \\texttt).
    """
    # Inline code: extract first, escape contents minimally, replace with placeholder
    # so bold/italic regex won't touch them.
    placeholders: list[str] = []

    def _code_sub(m: re.Match) -> str:
        idx = len(placeholders)
        # texttt requires escaping LaTeX specials inside
        placeholders.append(r"\texttt{" + _escape_text(m.group(1)) + "}")
        return f"\x00INLINECODE{idx}\x00"

    text = _INLINE_CODE.sub(_code_sub, text)

    # Now escape the remaining text
    text = _escape_text(text)

    # Re-apply inline markdown (the markers `**`, `*`, `[]()` survived escaping
    # because escape only touched LaTeX specials — `*`, `[`, `]`, `(`, `)` are
    # safe in LaTeX so untouched).
    # Note: after escaping, our placeholders look like \x00INLINECODE0\x00 still.
    text = _BOLD.sub(lambda m: r"\textbf{" + (m.group(1) or m.group(2)) + "}", text)
    text = _ITALIC.sub(lambda m: r"\textit{" + (m.group(1) or m.group(2)) + "}", text)
    text = _LINK.sub(lambda m: r"\href{" + m.group(2) + "}{" + m.group(1) + "}", text)

    # Restore inline code placeholders
    for i, repl in enumerate(placeholders):
        text = text.replace(f"\x00INLINECODE{i}\x00", repl)
    return text


def markdown_to_latex_body(md_text: str) -> str:
    """Convert Markdown body (without preamble) sang LaTeX body.

    Cover: heading (1-6), bold, italic, inline code, code fence (verbatim),
    unordered/ordered list, link, horizontal rule, paragraph.
    """
    lines = md_text.splitlines()
    out: list[str] = []

    in_code = False
    in_ul = False
    in_ol = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            joined = " ".join(p.strip() for p in paragraph if p.strip())
            if joined:
                out.append(_apply_inline(joined))
                out.append("")
            paragraph = []

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append(r"\end{itemize}")
            out.append("")
            in_ul = False
        if in_ol:
            out.append(r"\end{enumerate}")
            out.append("")
            in_ol = False

    for raw in lines:
        # Code fence handling first
        if _CODE_FENCE.match(raw):
            if in_code:
                out.append(r"\end{verbatim}")
                out.append("")
                in_code = False
            else:
                flush_paragraph()
                close_lists()
                out.append(r"\begin{verbatim}")
                in_code = True
            continue

        if in_code:
            out.append(raw)
            continue

        if not raw.strip():
            flush_paragraph()
            close_lists()
            continue

        # Heading
        h = _HEADING.match(raw)
        if h:
            flush_paragraph()
            close_lists()
            level = len(h.group(1)) - 1
            cmd = _HEADING_CMDS[min(level, len(_HEADING_CMDS) - 1)]
            title = _apply_inline(h.group(2).strip())
            out.append(f"\\{cmd}*{{{title}}}")
            out.append("")
            continue

        # Horizontal rule
        if _HRULE.match(raw):
            flush_paragraph()
            close_lists()
            out.append(r"\noindent\rule{\linewidth}{0.4pt}")
            out.append("")
            continue

        # Unordered list
        ul = _UL_ITEM.match(raw)
        if ul:
            flush_paragraph()
            if in_ol:
                out.append(r"\end{enumerate}")
                in_ol = False
            if not in_ul:
                out.append(r"\begin{itemize}")
                in_ul = True
            out.append(r"  \item " + _apply_inline(ul.group(2).strip()))
            continue

        # Ordered list
        ol = _OL_ITEM.match(raw)
        if ol:
            flush_paragraph()
            if in_ul:
                out.append(r"\end{itemize}")
                in_ul = False
            if not in_ol:
                out.append(r"\begin{enumerate}")
                in_ol = True
            out.append(r"  \item " + _apply_inline(ol.group(3).strip()))
            continue

        # Plain paragraph line
        close_lists()
        paragraph.append(raw)

    flush_paragraph()
    close_lists()
    if in_code:
        out.append(r"\end{verbatim}")

    return "\n".join(out).rstrip() + "\n"


def text_to_latex_body(plain: str) -> str:
    """Convert plain text body sang LaTeX body. Mỗi block phân tách bởi blank line
    thành một paragraph; line break đơn giữ nguyên bằng `\\\\` để không nuốt format.
    """
    paragraphs = re.split(r"\n\s*\n", plain.replace("\r\n", "\n"))
    out: list[str] = []
    for p in paragraphs:
        if not p.strip():
            continue
        # Escape, then convert line breaks within paragraph
        lines = [_escape_text(line) for line in p.splitlines()]
        out.append(r" \\".join(lines))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ── Full LaTeX document wrappers ────────────────────────────────────────────

_PREAMBLE_TMPL = r"""\documentclass[11pt,a4paper]{article}
\usepackage{fontspec}
\usepackage[a4paper,margin=2.5cm]{geometry}
\usepackage{hyperref}
\usepackage{verbatim}
\setlength{\parskip}{0.5em}
\setlength{\parindent}{0pt}
\title{%TITLE%}
\date{}
"""


def _build_document(title: str, body: str) -> str:
    safe_title = _escape_text(title) if title else "Document"
    preamble = _PREAMBLE_TMPL.replace("%TITLE%", safe_title)
    return (
        preamble
        + "\\begin{document}\n"
        + "\\maketitle\n\n"
        + body
        + "\n\\end{document}\n"
    )


def text_to_latex(plain: str, title: str = "") -> str:
    return _build_document(title, text_to_latex_body(plain))


def markdown_to_latex(md_text: str, title: str = "") -> str:
    return _build_document(title, markdown_to_latex_body(md_text))


def convert_to_latex(content: bytes, ext: str, title: str = "") -> str:
    """Dispatch theo ext (`txt` hoặc `md`). Trả về full LaTeX document (str)."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")
    if ext == "md":
        return markdown_to_latex(text, title=title)
    if ext == "txt":
        return text_to_latex(text, title=title)
    raise ValueError(f"Unsupported text ext: {ext!r}")
