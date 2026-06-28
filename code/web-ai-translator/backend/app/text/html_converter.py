"""Convert HTML sang LaTeX body — chỉ giữ structural elements (h1-h6, p, list, em, strong, code, blockquote, link).

Strategy:
- BeautifulSoup walk DOM.
- Skip: script, style, nav, header, footer, aside, form, iframe.
- Map: h1-h6 → \\section*..\\subparagraph*; p → paragraph; ul/ol → itemize/enumerate;
  li → \\item; strong/b → \\textbf; em/i → \\textit; code → \\texttt; pre → verbatim;
  blockquote → quote; a → \\href.
- Unknown tags → walk children, keep text.

Mục tiêu KHÔNG phải replicate styling — chỉ cần produce LaTeX dịch được, sau đó
compile thành PDF có cùng cấu trúc text với HTML gốc.
"""

from __future__ import annotations

from typing import Iterable

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError as e:
    BeautifulSoup = None  # type: ignore[assignment]
    NavigableString = None  # type: ignore[assignment]
    Tag = None  # type: ignore[assignment]
    _IMPORT_ERR = e
else:
    _IMPORT_ERR = None

from app.text.converter import _escape_text, _build_document


SUPPORTED_HTML_EXTS: tuple[str, ...] = (".html", ".htm")

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "iframe", "noscript", "svg"}
_HEADING_CMDS = ["section", "subsection", "subsubsection", "paragraph", "subparagraph", "subparagraph"]
_INLINE_TAGS = {"strong", "b", "em", "i", "code", "a", "span", "sup", "sub", "mark", "u"}


def html_ext(filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith(".html"):
        return "html"
    if lower.endswith(".htm"):
        return "html"
    return None


def _ensure_bs() -> None:
    if BeautifulSoup is None:
        raise RuntimeError(f"beautifulsoup4 chưa cài: {_IMPORT_ERR}")


def _render_inline(node) -> str:
    """Render inline content (text + inline tags) thành LaTeX inline string."""
    if isinstance(node, NavigableString):
        # Preserve boundary whitespace so concatenation doesn't glue words
        # ("hello <b>world</b>!" → "hello \textbf{world}!", not "hello\textbf{world}!").
        text = str(node)
        if not text:
            return ""
        leading = " " if text[0].isspace() else ""
        trailing = " " if text[-1].isspace() else ""
        normalized = " ".join(text.split())
        if not normalized:
            return leading or trailing
        return leading + _escape_text(normalized) + trailing

    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name in _SKIP_TAGS:
        return ""

    children = "".join(_render_inline(c) for c in node.children).strip()

    if name in ("strong", "b"):
        return r"\textbf{" + children + "}"
    if name in ("em", "i"):
        return r"\textit{" + children + "}"
    if name in ("code", "tt", "kbd"):
        return r"\texttt{" + children + "}"
    if name == "a":
        href = node.get("href", "")
        if href and not href.startswith("#"):
            return r"\href{" + href + "}{" + children + "}"
        return children
    if name == "br":
        return r" \\ "
    if name == "sup":
        return r"\textsuperscript{" + children + "}"
    if name == "sub":
        return r"\textsubscript{" + children + "}"
    if name in ("u", "mark"):
        return r"\underline{" + children + "}"
    # Default — drop tag wrapper, keep inline content
    return children


def _render_block(node, out: list[str]) -> None:
    """Render a block-level node. Appends LaTeX strings (each its own paragraph) to out."""
    if isinstance(node, NavigableString):
        text = str(node).strip()
        if text:
            out.append(_escape_text(" ".join(text.split())))
            out.append("")
        return

    if not isinstance(node, Tag):
        return

    name = node.name.lower()
    if name in _SKIP_TAGS:
        return

    # Headings
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1]) - 1
        cmd = _HEADING_CMDS[min(level, len(_HEADING_CMDS) - 1)]
        out.append(f"\\{cmd}*{{" + _render_inline(node).strip() + "}")
        out.append("")
        return

    if name == "p":
        rendered = _render_inline(node).strip()
        if rendered:
            out.append(rendered)
            out.append("")
        return

    if name == "br":
        return  # handled inline

    if name == "blockquote":
        out.append(r"\begin{quote}")
        inner: list[str] = []
        for child in node.children:
            _render_block(child, inner)
        # Drop leading/trailing blank lines
        out.extend(line for line in inner if line is not None)
        out.append(r"\end{quote}")
        out.append("")
        return

    if name == "pre":
        # Preserve text exactly (no escape — verbatim handles it)
        text = node.get_text()
        out.append(r"\begin{verbatim}")
        out.append(text.rstrip())
        out.append(r"\end{verbatim}")
        out.append("")
        return

    if name == "ul":
        out.append(r"\begin{itemize}")
        for li in node.find_all("li", recursive=False):
            out.append(r"  \item " + _render_inline(li).strip())
        out.append(r"\end{itemize}")
        out.append("")
        return

    if name == "ol":
        out.append(r"\begin{enumerate}")
        for li in node.find_all("li", recursive=False):
            out.append(r"  \item " + _render_inline(li).strip())
        out.append(r"\end{enumerate}")
        out.append("")
        return

    if name == "hr":
        out.append(r"\noindent\rule{\linewidth}{0.4pt}")
        out.append("")
        return

    if name == "table":
        # Table support is limited — flatten rows as paragraphs joined by " | "
        for tr in node.find_all("tr", recursive=True):
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            row_text = " \\quad | \\quad ".join(_render_inline(c).strip() for c in cells)
            out.append(row_text)
            out.append("")
        return

    # Inline tag at block level — render as paragraph
    if name in _INLINE_TAGS:
        rendered = _render_inline(node).strip()
        if rendered:
            out.append(rendered)
            out.append("")
        return

    # Container (div, section, article, main, body, html) — recurse
    for child in node.children:
        _render_block(child, out)


def html_to_latex_body(html_text: str) -> tuple[str, str]:
    """Convert HTML string → (latex_body, derived_title).

    Title được lấy từ <title> nếu có, else h1 đầu tiên, else "".
    """
    _ensure_bs()
    soup = BeautifulSoup(html_text, "html.parser")

    # Derive title
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif soup.find("h1"):
        h1_text = soup.find("h1").get_text()
        if h1_text:
            title = " ".join(h1_text.split())

    body = soup.body or soup

    out: list[str] = []
    for child in body.children:
        _render_block(child, out)

    # Compact double blank lines
    compacted: list[str] = []
    prev_blank = False
    for line in out:
        if not line.strip():
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        compacted.append(line)

    return "\n".join(compacted).rstrip() + "\n", title


def html_to_latex(html_text: str, title: str = "") -> str:
    body, derived_title = html_to_latex_body(html_text)
    final_title = title or derived_title or "Document"
    return _build_document(final_title, body)


def convert_html_to_latex(content: bytes, title: str = "") -> tuple[str, str]:
    """Decode + convert HTML bytes → (LaTeX document, derived title)."""
    try:
        html_text = content.decode("utf-8")
    except UnicodeDecodeError:
        html_text = content.decode("latin-1", errors="replace")
    body, derived_title = html_to_latex_body(html_text)
    final_title = title or derived_title or "Document"
    return _build_document(final_title, body), final_title
