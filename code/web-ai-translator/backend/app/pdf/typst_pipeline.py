"""Typst-based PDF rebuild pipeline (parallel to rebuild_pdf_inplace).

Architecture mirrors RetainPDF (https://github.com/wxyhgk/retain-pdf):
  1. Background = original PDF with translatable text redacted (fill=False),
     so images / vectors / lines / non-translatable text stay intact.
  2. Overlay   = Typst-compiled PDF with translated text placed at the exact
     coordinates of the original blocks.
  3. Final    = background with overlay composited on top via show_pdf_page.

Why Typst instead of TextWriter.fill_textbox:
  - Real typesetting engine: proper text shaping, kerning, font fallback.
  - Native multi-font fallback covers Vietnamese diacritics + CJK glyphs.
  - Auto-wrap with proper line breaking and hanging indent for bullets.
  - Clean separation: rendering issues are debuggable via raw .typ source.

This module is intentionally standalone — it does NOT touch rebuild_pdf_inplace
or any of the existing in-place logic. Callers pick the engine.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from typing import Sequence

import fitz

from .processor import (
    TextBlock,
    _apply_translation_provenance,
    _bbox_in_rects,
    _classify_block_style,
    _normalize,
)


def _is_preserved_region(b: TextBlock) -> bool:
    """Blocks that should keep the original layout: table cells and in-figure
    text that are NOT captions. Captions still get overlaid in-place so the
    reader sees a Vietnamese ``Bảng N: ...`` / ``Hình N: ...`` marker.

    Rotated text (arXiv side-banner, vertical watermarks) is also preserved:
    PyMuPDF's bbox is a vertical strip, so naively overlaying a horizontal
    translation produces a banner that runs sideways into the body column.
    """
    if b.is_caption:
        return False
    if getattr(b, "is_rotated", False):
        return True
    return bool(b.is_in_table or b.is_in_figure)


def find_typst_bin() -> str:
    """Locate the Typst CLI.

    Search order:
      1. ``backend/bin/typst.exe`` (portable, project-local).
      2. ``TYPST_BIN`` env var.
      3. ``typst`` on PATH.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    portable = os.path.join(here, "bin", "typst.exe" if sys.platform == "win32" else "typst")
    if os.path.isfile(portable):
        return portable
    env = os.environ.get("TYPST_BIN")
    if env and os.path.isfile(env):
        return env
    from shutil import which
    found = which("typst")
    if found:
        return found
    raise FileNotFoundError(
        "Typst CLI not found. Install at backend/bin/typst.exe or set TYPST_BIN."
    )


def _default_font_dirs() -> list[str]:
    """Font directories to pass to Typst via --font-path."""
    dirs: list[str] = []
    if sys.platform == "win32":
        dirs.append(os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"))
    elif sys.platform == "darwin":
        dirs.extend(["/Library/Fonts", "/System/Library/Fonts",
                     os.path.expanduser("~/Library/Fonts")])
    else:
        dirs.extend([
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ])
    return [d for d in dirs if os.path.isdir(d)]


def _escape_typst_string(s: str) -> str:
    """Escape a string for use inside Typst double-quoted string literal."""
    # Typst string literals support \\, \", \n, \r, \t, \\u{HHHH}
    return s.replace("\\", "\\\\").replace("\"", "\\\"")


# Vietnamese compound words/phrases that read as a single unit and must
# stay on the same line. Splitting them at a line break ("Trí tuệ\nnhân
# tạo", "có\nthể") is grammatically jarring even though each token is a
# valid word. By replacing the internal space with U+00A0 (NBSP) we let
# Typst still wrap at OTHER spaces but never at these joins.
_VI_COMPOUNDS: tuple[str, ...] = (
    # modals & adverbs
    "có thể", "không thể", "có lẽ", "có vẻ", "cần phải", "phải chăng",
    # conjunctions / connectives
    "tuy nhiên", "do đó", "vì vậy", "ngoài ra", "hơn nữa",
    "trong khi", "mặc dù", "đồng thời", "bởi vì", "cho nên",
    # prepositions
    "đối với", "dựa trên", "dựa vào", "thông qua", "nhờ vào", "bên cạnh",
    # common 2-syllable verbs in academic writing
    "phân tích", "đánh giá", "phân loại", "thực hiện", "phát triển",
    "giải thích", "đề xuất", "đề cập", "xác định", "trình bày",
    "khảo sát", "tổng hợp", "so sánh", "kết luận", "chứng minh",
    "áp dụng", "sử dụng", "tính toán", "tối ưu", "huấn luyện",
    # common 2-syllable nouns
    "kết quả", "phương pháp", "nghiên cứu", "mô hình", "thuật toán",
    "dữ liệu", "đặc trưng", "ví dụ", "thí nghiệm", "tham số",
    "độ chính xác", "giả thuyết", "khái niệm", "vấn đề", "bài toán",
    "công trình", "tài liệu", "hiệu quả", "ứng dụng",
    # AI/ML compound concepts (2-token, treated as one term)
    "trí tuệ", "nhân tạo", "học máy", "học sâu",
    "mạng nơ-ron", "xử lý", "ngôn ngữ", "tự nhiên",
)

_VI_COMPOUNDS_PATTERN = re.compile(
    "|".join(re.escape(c) for c in sorted(_VI_COMPOUNDS, key=len, reverse=True)),
    re.IGNORECASE,
)


def _protect_vi_compounds(s: str) -> str:
    """Replace the internal space inside common Vietnamese compounds with
    U+00A0 NBSP so Typst never wraps between the two halves.

    Examples::

        "Trí tuệ nhân tạo có thể học"  ->  "Trí tuệ nhân tạo có thể học"
        # "Trí tuệ" and "có thể" become atomic; everything else can wrap.
    """
    if not s:
        return s
    return _VI_COMPOUNDS_PATTERN.sub(
        lambda m: m.group(0).replace(" ", " "),
        s,
    )


def _color_to_typst_rgb(color) -> str:
    """Convert a fitz color (tuple or packed int) to Typst rgb(...) literal."""
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        r, g, b = color[0], color[1], color[2]
        if max(r, g, b) <= 1.0:
            r, g, b = int(r * 255), int(g * 255), int(b * 255)
        else:
            r, g, b = int(r), int(g), int(b)
    elif isinstance(color, (int, float)):
        c = int(color)
        r = (c >> 16) & 0xFF
        g = (c >> 8) & 0xFF
        b = c & 0xFF
    else:
        r = g = b = 0
    return f"rgb({r}, {g}, {b})"


def _align_to_typst(align: int) -> str:
    if align == TextBlock.ALIGN_CENTER:
        return "center"
    if align == TextBlock.ALIGN_RIGHT:
        return "right"
    return "left"  # left and justify both start at left


def _block_weight_style(b: TextBlock) -> tuple[str, str]:
    is_bold, is_italic = _classify_block_style(b.spans_info)
    weight = "bold" if is_bold else "regular"
    style = "italic" if is_italic else "normal"
    return weight, style


def make_background_pdf(
    original_pdf: str,
    blocks: Sequence[TextBlock],
    output_path: str,
) -> str:
    """Strip translatable text from *original_pdf*; keep all other content.

    Uses ``add_redact_annot(fill=False)`` + ``apply_redactions`` with
    ``PDF_REDACT_IMAGE_NONE`` and ``PDF_REDACT_LINE_ART_NONE`` so that
    images, vector drawings, and backgrounds remain untouched.
    """
    doc = fitz.open(original_pdf)
    by_page: dict[int, list[TextBlock]] = {}
    for b in blocks:
        if not b.is_translatable:
            continue
        if _is_preserved_region(b):
            continue
        by_page.setdefault(b.page_num, []).append(b)

    for page_num, page_blocks in by_page.items():
        if page_num >= len(doc):
            continue
        page = doc[page_num]
        for b in page_blocks:
            rect = fitz.Rect(b.bbox) + (-0.5, -0.5, 0.5, 0.5)
            page.add_redact_annot(rect, fill=False)
        page.apply_redactions(
            text=fitz.PDF_REDACT_TEXT_REMOVE,
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return output_path


_SUB_LABEL_RE = __import__("re").compile(r"^\(?[a-z]\)?$", __import__("re").IGNORECASE)


def _looks_like_real_table(data: list[list], trans: list[list[str]]) -> bool:
    """Filter out PyMuPDF false-positives from ``page.find_tables()``.

    Common false positives we want to drop:
      - Math formulas extracted into a 1-column "table".
      - Sub-figure label grids (``(a)`` ``(b)`` ``(c)`` ``(d)``).
      - Tiny 1×N or N×1 layouts with mostly empty / single-glyph cells.
    """
    if not data:
        return False
    non_empty_rows = [r for r in data if any((c or "").strip() for c in r)]
    if len(non_empty_rows) < 2:
        return False
    max_cols = max(len(r) for r in non_empty_rows)
    if max_cols < 2:
        return False

    flat_cells = [
        (c or "").strip() for row in non_empty_rows for c in row
        if (c or "").strip()
    ]
    if not flat_cells:
        return False
    short_count = sum(1 for c in flat_cells if len(c) <= 4)
    if short_count / len(flat_cells) > 0.85:
        return False
    sub_label_count = sum(1 for c in flat_cells if _SUB_LABEL_RE.match(c))
    if sub_label_count / len(flat_cells) > 0.6:
        return False

    def _norm(s: str) -> str:
        return " ".join((s or "").split()).lower()

    has_translation = any(
        _norm(cell) and _norm(cell) != _norm(orig)
        for row_o, row_t in zip(data, trans)
        for orig, cell in zip(row_o, row_t)
    )
    return has_translation


def _collect_translated_tables(
    original_pdf: str,
    blocks: Sequence[TextBlock],
) -> list[dict]:
    """Re-detect tables on each page and pair every cell with a translation.

    For each detected table we return::

        {
            "page_num":   int,  # 0-based PDF page index
            "table_idx":  int,  # 1-based table number on that page
            "data":       [[str, ...], ...],   # original cell text
            "trans":      [[str, ...], ...],   # translated cell text (fallback EN)
            "header_bold": bool,
        }

    Cells are matched to translated blocks by normalised text content (the
    same strategy as ``_table_to_latex`` in processor.py) so it stays robust
    to PyMuPDF's cell-grouping quirks.
    """
    out: list[dict] = []
    blocks_by_page: dict[int, list[TextBlock]] = {}
    for b in blocks:
        blocks_by_page.setdefault(b.page_num, []).append(b)

    doc = fitz.open(original_pdf)
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]
            try:
                tabs = page.find_tables()
                tables = list(tabs.tables)
            except Exception:
                tables = []
            if not tables:
                continue
            page_blocks = blocks_by_page.get(page_num, [])

            for t_idx, t in enumerate(tables, start=1):
                tbbox = fitz.Rect(t.bbox)
                tbl_blocks = [
                    b for b in page_blocks
                    if _bbox_in_rects(b.bbox, [tbbox], threshold=0.3)
                ]
                trans_map: dict[str, str] = {}
                for b in tbl_blocks:
                    if b.is_translatable and b.translated_text and b.text:
                        key = _normalize(b.text)
                        if key:
                            trans_map[key] = b.translated_text

                try:
                    data = t.extract()
                except Exception:
                    data = []
                if not data:
                    continue

                header_bold = False
                for b in tbl_blocks:
                    if b.bbox[1] < tbbox.y0 + (tbbox.y1 - tbbox.y0) * 0.15:
                        is_b, _ = _classify_block_style(b.spans_info)
                        if is_b:
                            header_bold = True
                            break

                trans_grid: list[list[str]] = []
                for row in data:
                    trans_row: list[str] = []
                    for raw in row:
                        s = (raw or "").strip()
                        key = _normalize(s)
                        cell = trans_map.get(key, s) if key else s
                        cell = " ".join(cell.split())
                        trans_row.append(cell)
                    trans_grid.append(trans_row)

                if not _looks_like_real_table(data, trans_grid):
                    continue

                out.append({
                    "page_num": page_num,
                    "table_idx": t_idx,
                    "data": [[(c or "").strip() for c in row] for row in data],
                    "trans": trans_grid,
                    "header_bold": header_bold,
                })
    finally:
        doc.close()
    return out


def _build_typst_appendix(tables: list[dict]) -> str:
    """Emit a Typst appendix section listing the Vietnamese version of every
    detected table. Uses standard A4 with normal margins so the wide tables
    breathe even if the source PDF used a tighter layout.
    """
    if not tables:
        return ""

    lines: list[str] = []
    lines.append("#pagebreak()")
    lines.append("#set page(\"a4\", margin: (x: 2cm, y: 2cm))")
    lines.append("#set text(size: 10pt)")
    lines.append("#align(center, text(size: 16pt, weight: \"bold\", "
                 "\"Phụ lục: Bản dịch các bảng\"))")
    lines.append("#v(0.5em)")
    lines.append("#align(center, text(size: 10pt, fill: rgb(80,80,80), "
                 "\"Bản gốc tiếng Anh giữ nguyên trong nội dung chính. "
                 "Phần dưới đây chỉ liệt kê bản dịch tiếng Việt của các bảng.\"))")
    lines.append("#v(1em)")

    for entry in tables:
        page_num = entry["page_num"] + 1  # 1-based for humans
        t_idx = entry["table_idx"]
        grid = entry["trans"]
        header_bold = entry["header_bold"]
        if not grid:
            continue
        col_count = max(len(r) for r in grid) or 1

        heading = f"Bảng {t_idx} (trang {page_num})"
        lines.append("#v(0.8em)")
        lines.append(
            f"#text(size: 12pt, weight: \"bold\", "
            f"\"{_escape_typst_string(heading)}\")"
        )
        lines.append("#v(0.3em)")
        lines.append("#table(")
        lines.append(f"  columns: {col_count},")
        lines.append("  stroke: 0.5pt,")
        lines.append("  inset: 5pt,")
        for r_idx, row in enumerate(grid):
            cells: list[str] = []
            for c in range(col_count):
                raw = row[c] if c < len(row) else ""
                esc = _escape_typst_string(raw)
                if r_idx == 0 and header_bold:
                    cells.append(f"text(weight: \"bold\", \"{esc}\")")
                else:
                    cells.append(f"text(\"{esc}\")")
            lines.append("  " + ", ".join(cells) + ",")
        lines.append(")")

    lines.append("")
    return "\n".join(lines)


def build_typst_source(
    blocks: Sequence[TextBlock],
    page_sizes: list[tuple[float, float]],
    default_font: str = "Times New Roman",
    fallback_fonts: tuple[str, ...] = ("DejaVu Serif", "Liberation Serif"),
    appendix_tables: list[dict] | None = None,
) -> str:
    """Emit Typst markup that places each translated block at its bbox.

    Typst's coordinate system with ``margin: 0pt`` matches PDF: origin at
    top-left, units in points, y grows downward. So ``dx=bbox.x0, dy=bbox.y0``
    is a direct copy.
    """
    if not page_sizes:
        return "// no pages\n"

    # Group translatable blocks by page; preserve insertion order otherwise.
    # Skip blocks that are kept in their original layout (table cells and
    # in-figure text) — captions still get overlaid so the Vietnamese
    # "Bảng N: ..." / "Hình N: ..." marker shows in-place.
    by_page: dict[int, list[TextBlock]] = {}
    for b in blocks:
        if not b.is_translatable:
            continue
        if _is_preserved_region(b):
            continue
        if not (b.translated_text or "").strip():
            continue
        by_page.setdefault(b.page_num, []).append(b)

    font_list = ", ".join(
        f'"{f}"' for f in (default_font, *fallback_fonts)
    )

    lines: list[str] = []
    lines.append("// auto-generated by typst_pipeline.build_typst_source")
    lines.append(f"#set text(font: ({font_list}), hyphenate: false)")
    lines.append("#set par(leading: 0.55em)")
    lines.append("")

    for page_num, (pw, ph) in enumerate(page_sizes):
        if page_num > 0:
            lines.append("#pagebreak()")
        lines.append(
            f"#set page(width: {pw:.2f}pt, height: {ph:.2f}pt, margin: 0pt)"
        )
        page_blocks = by_page.get(page_num, [])
        for b in page_blocks:
            x0, y0, x1, y1 = b.bbox
            w = max(1.0, x1 - x0)
            # Generous extra height: Typst will wrap; clip handles overflow.
            # We extend down by ~3× block height as a safety so common
            # Vietnamese expansions don't get clipped.
            orig_h = max(1.0, y1 - y0)
            h = max(orig_h, orig_h * 3.0)

            weight, style = _block_weight_style(b)
            fs = float(b.font_size or 10.0)
            color = _color_to_typst_rgb(b.color)
            align = _align_to_typst(b.align)
            text = _escape_typst_string(_protect_vi_compounds(b.translated_text))

            # Headings: original bbox is the exact glyph extent so VI
            # translations (typically 1.2-1.5× longer) wrap awkwardly.
            # Detect heading-like blocks (bold OR large font on a short
            # paragraph) and stretch the box to the page right margin.
            is_bold_block = (weight == "bold")
            is_heading = (
                len((b.text or "").strip()) <= 200
                and (is_bold_block or fs >= 12.0)
            )
            # `place_dx` is the box's top-left X. For centered headings we
            # rebase it around the original glyph center so the box grows
            # symmetrically — otherwise expanding only to the right pushes
            # short text ("Tóm tắt") far to the right of the page.
            place_dx = x0
            if is_heading:
                page_margin = 30.0
                if b.align == TextBlock.ALIGN_CENTER:
                    cx = (x0 + x1) / 2
                    max_w = min(pw - 2 * page_margin, pw - page_margin)
                    max_w = max(w, max_w)
                    place_dx = cx - max_w / 2
                    # Clamp so the box stays on the page.
                    if place_dx < page_margin:
                        place_dx = page_margin
                    if place_dx + max_w > pw - page_margin:
                        place_dx = pw - page_margin - max_w
                    w = max_w
                else:
                    max_w = max(w, pw - x0 - page_margin)
                    w = max_w

            # Vietnamese stacked diacritics (ẵ ồ ữ ệ …) extend higher than
            # the original English cap-height. The block bbox from the
            # source PDF only encompasses the English ink, so naively
            # placing VI text inside a clipped box at dy=y0 eats the
            # top-most mark. We extend the box upward by `top_pad` and
            # pad the inner content back down so the baseline stays put.
            top_pad = fs * 0.35 if is_heading else 0.0
            place_dy = y0 - top_pad
            box_h = h + top_pad
            if top_pad > 0:
                inner = (
                    f"pad(top: {top_pad:.2f}pt, "
                    f"align({align} + top, "
                    f"text(size: {fs:.2f}pt, weight: \"{weight}\", "
                    f"style: \"{style}\", fill: {color}, "
                    f"\"{text}\")"
                    f"))"
                )
            else:
                inner = (
                    f"align({align} + top, "
                    f"text(size: {fs:.2f}pt, weight: \"{weight}\", "
                    f"style: \"{style}\", fill: {color}, "
                    f"\"{text}\")"
                    f")"
                )

            lines.append(
                f"#place(top + left, dx: {place_dx:.2f}pt, dy: {place_dy:.2f}pt, "
                f"box(width: {w:.2f}pt, height: {box_h:.2f}pt, clip: true, "
                f"{inner}))"
            )
        lines.append("")

    if appendix_tables:
        lines.append(_build_typst_appendix(appendix_tables))

    return "\n".join(lines)


def compile_typst(
    source_path: str,
    output_path: str,
    font_paths: list[str] | None = None,
    typst_bin: str | None = None,
) -> str:
    """Compile a .typ file to PDF via the Typst CLI."""
    if typst_bin is None:
        typst_bin = find_typst_bin()
    if font_paths is None:
        font_paths = _default_font_dirs()

    cmd = [typst_bin, "compile"]
    for d in font_paths:
        cmd.extend(["--font-path", d])
    cmd.extend([source_path, output_path])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"typst compile failed (exit {proc.returncode}):\n"
            f"STDERR: {proc.stderr}\nSTDOUT: {proc.stdout}"
        )
    return output_path


def merge_background_overlay(
    background_pdf: str,
    overlay_pdf: str,
    output_path: str,
) -> str:
    """Composite *overlay_pdf* on top of *background_pdf*.

    The overlay may contain MORE pages than the background — those extra
    pages (the appendix) are appended verbatim so the Vietnamese table
    listing shows up at the end of the document.
    """
    bg = fitz.open(background_pdf)
    ov = fitz.open(overlay_pdf)
    bg_count = len(bg)
    ov_count = len(ov)
    overlap = min(bg_count, ov_count)
    for i in range(overlap):
        bg_page = bg[i]
        bg_page.show_pdf_page(bg_page.rect, ov, i, overlay=True)
    if ov_count > bg_count:
        bg.insert_pdf(ov, from_page=bg_count, to_page=ov_count - 1)
    bg.save(output_path, garbage=4, deflate=True)
    bg.close()
    ov.close()
    return output_path


def rebuild_pdf_typst(
    original_pdf: str,
    blocks: Sequence[TextBlock],
    output_path: str,
    translation_meta: dict | None = None,
    keep_intermediates: bool = False,
) -> str:
    """Build a translated PDF via the Typst background+overlay pipeline.

    Mirrors the signature of ``rebuild_pdf_inplace`` so it can be swapped in.
    Returns the path to the final merged PDF.
    """
    # Determine page sizes from the original.
    src = fitz.open(original_pdf)
    page_sizes = [(p.rect.width, p.rect.height) for p in src]
    src.close()

    tmpdir = tempfile.mkdtemp(prefix="typst_pipeline_")
    bg_path = os.path.join(tmpdir, "background.pdf")
    src_path = os.path.join(tmpdir, "overlay.typ")
    overlay_path = os.path.join(tmpdir, "overlay.pdf")

    print(f"[typst] Building background -> {bg_path}")
    make_background_pdf(original_pdf, blocks, bg_path)

    print(f"[typst] Generating Typst source -> {src_path}")
    appendix_tables = _collect_translated_tables(original_pdf, blocks)
    if appendix_tables:
        print(f"[typst] Appendix: {len(appendix_tables)} translated table(s)")
    source = build_typst_source(
        blocks, page_sizes, appendix_tables=appendix_tables
    )
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(source)

    print(f"[typst] Compiling overlay -> {overlay_path}")
    compile_typst(src_path, overlay_path)

    print(f"[typst] Merging background + overlay -> {output_path}")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    merge_background_overlay(bg_path, overlay_path, output_path)

    if translation_meta:
        doc = fitz.open(output_path)
        _apply_translation_provenance(doc, translation_meta)
        doc.saveIncr()
        doc.close()

    if not keep_intermediates:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        print(f"[typst] Intermediates kept at {tmpdir}")

    return output_path
