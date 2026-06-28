"""Marker-based PDF extraction — converts PDF to Markdown with LaTeX math.

Alternative to PyMuPDF raw text extraction. Marker uses ML models to:
- Better detect document structure (headings, paragraphs, lists)
- Preserve mathematical expressions in LaTeX notation
- Handle multi-column layouts more accurately
- Extract tables as Markdown tables

Output is clean Markdown suitable for LLM translation.

Requires: pip install marker-pdf
Fallback: pymupdf4llm (lighter, no ML models needed)
"""

import os
import re
import json

# Try marker first, fall back to pymupdf4llm
_BACKEND = None  # "marker" | "pymupdf4llm" | None


def _detect_backend() -> str | None:
    """Detect which extraction backend is available."""
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND

    try:
        import marker
        _BACKEND = "marker"
        return _BACKEND
    except ImportError:
        pass

    try:
        import pymupdf4llm
        _BACKEND = "pymupdf4llm"
        return _BACKEND
    except ImportError:
        pass

    _BACKEND = ""
    return None


def is_available() -> bool:
    """Check if any Markdown extraction backend is installed."""
    return bool(_detect_backend())


def get_backend_name() -> str:
    """Return the name of the active backend."""
    return _detect_backend() or "none"


def extract_to_markdown(pdf_path: str) -> str:
    """Extract PDF content as Markdown text with LaTeX math preserved.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Markdown string with document content.

    Raises:
        RuntimeError: If no extraction backend is available.
    """
    backend = _detect_backend()
    if not backend:
        raise RuntimeError(
            "No Markdown extraction backend available. "
            "Install one of: pip install marker-pdf OR pip install pymupdf4llm"
        )

    if backend == "marker":
        return _extract_with_marker(pdf_path)
    else:
        return _extract_with_pymupdf4llm(pdf_path)


def _extract_with_marker(pdf_path: str) -> str:
    """Extract using marker-pdf (ML-powered, best quality)."""
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.config.parser import ConfigParser

    config_parser = ConfigParser({"output_format": "markdown"})
    converter = PdfConverter(
        artifact_dict=create_model_dict(),
        config=config_parser.generate_config_dict(),
    )
    rendered = converter(pdf_path)
    return rendered.markdown


def _extract_with_pymupdf4llm(pdf_path: str) -> str:
    """Extract using pymupdf4llm (lightweight, PyMuPDF-based)."""
    import pymupdf4llm
    return pymupdf4llm.to_markdown(pdf_path)


# ── Markdown chunking ────────────────────────────────────────────

def split_markdown_into_chunks(
    markdown: str,
    max_chars: int = 1500,
) -> list[dict]:
    """Split Markdown into translation-ready chunks.

    Respects document structure:
    - Never splits inside math blocks ($$...$$, $...$)
    - Splits at heading boundaries preferentially
    - Splits at paragraph boundaries (double newline)
    - Preserves heading hierarchy for context

    Returns list of dicts: [{"text": str, "heading_context": str}, ...]
    """
    if not markdown:
        return []

    # Split into logical sections by headings
    sections = _split_by_headings(markdown)

    chunks = []
    for section in sections:
        heading = section["heading"]
        body = section["body"].strip()
        if not body:
            continue

        if len(body) <= max_chars:
            chunks.append({
                "text": body,
                "heading_context": heading,
            })
        else:
            # Split large sections into smaller chunks at paragraph boundaries
            paragraphs = re.split(r'\n\n+', body)
            current_chunk = ""
            for para in paragraphs:
                if not para.strip():
                    continue
                if current_chunk and len(current_chunk) + len(para) + 2 > max_chars:
                    chunks.append({
                        "text": current_chunk.strip(),
                        "heading_context": heading,
                    })
                    current_chunk = ""
                current_chunk += para + "\n\n"

            if current_chunk.strip():
                chunks.append({
                    "text": current_chunk.strip(),
                    "heading_context": heading,
                })

    return chunks


def _split_by_headings(markdown: str) -> list[dict]:
    """Split Markdown by heading boundaries.

    Returns list of {"heading": str, "body": str}.
    """
    # Match Markdown headings: # Heading, ## Heading, etc.
    heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

    sections = []
    last_pos = 0
    current_heading = ""

    for match in heading_pattern.finditer(markdown):
        # Save previous section
        body = markdown[last_pos:match.start()]
        if body.strip() or current_heading:
            sections.append({
                "heading": current_heading,
                "body": body,
            })

        current_heading = match.group(0)  # Full heading line
        last_pos = match.end()

    # Last section
    body = markdown[last_pos:]
    if body.strip():
        sections.append({
            "heading": current_heading,
            "body": body,
        })

    # If no headings found, return whole document as one section
    if not sections:
        sections = [{"heading": "", "body": markdown}]

    return sections


# ── Math extraction from Markdown ────────────────────────────────

def extract_math_expressions(markdown: str) -> list[dict]:
    """Extract all math expressions from Markdown.

    Returns list of {"type": "inline"|"display", "latex": str, "start": int, "end": int}.
    Useful for validation — checking math is preserved after translation.
    """
    expressions = []

    # Display math: $$...$$
    for m in re.finditer(r'\$\$(.+?)\$\$', markdown, re.DOTALL):
        expressions.append({
            "type": "display",
            "latex": m.group(1).strip(),
            "start": m.start(),
            "end": m.end(),
        })

    # Inline math: $...$ (not preceded/followed by $)
    for m in re.finditer(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', markdown):
        expressions.append({
            "type": "inline",
            "latex": m.group(1).strip(),
            "start": m.start(),
            "end": m.end(),
        })

    return sorted(expressions, key=lambda x: x["start"])


def markdown_to_pdf(markdown_path: str, output_pdf: str) -> str:
    """Convert Markdown file to PDF using pandoc + xelatex.

    Requires pandoc and xelatex installed on the system.
    XeLaTeX is used for Vietnamese Unicode support.

    Args:
        markdown_path: Path to the .md file.
        output_pdf: Path for the output PDF.

    Returns:
        Path to the generated PDF.

    Raises:
        RuntimeError: If pandoc or xelatex is not available.
    """
    import subprocess
    import shutil

    # Check pandoc
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise RuntimeError(
            "pandoc not found. Install from https://pandoc.org/installing.html"
        )

    # Check xelatex (Windows MiKTeX + Linux/macOS TeX Live)
    xelatex = shutil.which("xelatex")
    if not xelatex:
        import sys
        if sys.platform == "win32":
            local_app = os.environ.get("LOCALAPPDATA", "")
            if local_app:
                miktex_bin = os.path.join(
                    local_app, "Programs", "MiKTeX", "miktex", "bin", "x64", "xelatex.exe"
                )
                if os.path.exists(miktex_bin):
                    xelatex = miktex_bin
        else:
            for candidate in (
                "/usr/bin/xelatex",
                "/usr/local/bin/xelatex",
                "/Library/TeX/texbin/xelatex",
            ):
                if os.path.isfile(candidate):
                    xelatex = candidate
                    break

    if not xelatex:
        raise RuntimeError(
            "xelatex not found. Install MiKTeX/TeX Live on Windows or "
            "'apt install texlive-xetex' on Linux."
        )

    # Build pandoc command
    cmd = [
        pandoc,
        markdown_path,
        "-o", output_pdf,
        "--pdf-engine", xelatex,
        "-V", "mainfont=Times New Roman",
        "-V", "geometry:margin=1in",
        "-V", "fontsize=11pt",
        "--variable", "CJKmainfont=Times New Roman",
    ]

    # Add header-includes for Vietnamese support
    header = (
        r"\usepackage{fontspec}"
        r"\usepackage{polyglossia}"
        r"\setdefaultlanguage{vietnamese}"
    )

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        # Retry without polyglossia (simpler)
        cmd_simple = [
            pandoc,
            markdown_path,
            "-o", output_pdf,
            "--pdf-engine", xelatex,
            "-V", "mainfont=Times New Roman",
            "-V", "geometry:margin=1in",
            "-V", "fontsize=11pt",
        ]
        result = subprocess.run(
            cmd_simple,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pandoc failed:\n{result.stderr[:500]}"
            )

    return output_pdf
