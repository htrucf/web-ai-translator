"""PDF text extraction and reconstruction using PyMuPDF.

Extracts text blocks with exact coordinates from a PDF, classifies them
(translatable text vs math vs figures), and rebuilds a new PDF with
translated text placed at the same positions.
"""

import math
import os
import re
import shutil
import subprocess
import sys
try:
    import fitz  # PyMuPDF < 1.25
except ImportError:
    import pymupdf as fitz  # PyMuPDF >= 1.25

from app.utils.translation_meta import format_pdf_metadata, format_pdf_footer


# Fonts commonly used for math in LaTeX-generated PDFs.
# CMR (Computer Modern Roman) is intentionally excluded — it is a body text
# font (CMR10, CMR12 …) and would cause false-positive math classification.
# Expanded coverage (D1):
#   - Latin Modern Math (LMRoman, LMMath, LMSym)
#   - STIX, STIX Two Math
#   - XITS, XITS Math
#   - Cambria Math
#   - Asana Math
#   - TeX Gyre families' math companions (Pagella, Termes, Schola, Bonum, DejaVu Math)
#   - MathTime, MathTime Professional
#   - Fira Math, Libertinus Math
MATH_FONT_PATTERNS = re.compile(
    r"(CMM[A-Z]|CMSY|CMEX|MSAM|MSBM|Symbol|Math|rsfs|eufm|stmary"
    r"|wasy|lasy|esint|bbold|dsrom|EUSM|EURM|cmmi|cmsy|cmex"
    r"|LMMath|LMSym|LMRoman.*Math|Latin\s*Modern\s*Math"
    r"|STIX|XITS|Cambria\s*Math|AsanaMath|Asana\s*Math"
    r"|TeXGyre.*Math|DejaVuMath|MathTime|FiraMath|LibertinusMath"
    r"|Neo\s*Euler|Euler\s*Math)",
    re.IGNORECASE,
)

# Standalone math-only font tokens that don't include "Math" in the name
# but render mathematical glyphs (heuristic — matched as substrings).
_AUX_MATH_FONT_TOKENS = (
    "txsy", "txex", "pxsy", "pxex",       # tx/px math fonts
    "boondox", "dutchcal",                  # ams calligraphic alternatives
    "stix",
)

# Unicode ranges for mathematical symbols
MATH_UNICODE_RANGES = [
    (0x0370, 0x03FF),  # Greek
    (0x2070, 0x209F),  # Superscripts & Subscripts
    (0x2100, 0x214F),  # Letterlike Symbols
    (0x2150, 0x218F),  # Number Forms (fractions ½ etc.)
    (0x2190, 0x21FF),  # Arrows
    (0x2200, 0x22FF),  # Mathematical Operators
    (0x2300, 0x23FF),  # Miscellaneous Technical
    (0x27C0, 0x27EF),  # Miscellaneous Mathematical Symbols-A
    (0x2980, 0x29FF),  # Miscellaneous Mathematical Symbols-B
    (0x2A00, 0x2AFF),  # Supplemental Mathematical Operators
    (0x1D400, 0x1D7FF),  # Mathematical Alphanumeric Symbols
]

# PDF font flag bits (standardized in PDF spec, exposed by PyMuPDF)
#   bit 1 (1)   = superscript (PyMuPDF span flag — span is rendered above baseline)
#   bit 2 (2)   = italic
#   bit 3 (4)   = serif
#   bit 4 (8)   = monospaced
#   bit 5 (16)  = bold
# Symbolic-font flag is at bit 3 of the *PDF font dictionary* /Flags entry, but
# PyMuPDF doesn't surface that directly — we keep it lexical (font name) above.
_FLAG_SUPERSCRIPT = 1


def _is_math_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in MATH_UNICODE_RANGES)


# ── Font style detection ─────────────────────────────────────────

_BOLD_RE = re.compile(
    r"(Bold|Medi|CMBX|CMBXTI|Demi|Heavy|Black|-Bd|-B$|bd$)", re.IGNORECASE
)
_ITALIC_RE = re.compile(
    r"(Italic|Ital|Oblique|CMTI|CMBXTI|Slant|-It|-I$|it$)", re.IGNORECASE
)


def _detect_span_style(span: dict) -> tuple[bool, bool]:
    """Detect bold and italic from span flags and font name.

    Returns (is_bold, is_italic).
    """
    flags = span.get("flags", 0)
    font_name = span.get("font", "")
    is_bold = bool(flags & 16) or bool(_BOLD_RE.search(font_name))
    is_italic = bool(flags & 2) or bool(_ITALIC_RE.search(font_name))
    return is_bold, is_italic


def _classify_block_style(spans_info: list[dict]) -> tuple[bool, bool]:
    """Determine dominant bold/italic style of a block by character weight.

    Returns (is_bold, is_italic).
    """
    if not spans_info:
        return False, False

    bold_chars = 0
    italic_chars = 0
    total_chars = 0

    for span in spans_info:
        n = len(span.get("text", "").strip())
        if n == 0:
            continue
        b, i = _detect_span_style(span)
        if b:
            bold_chars += n
        if i:
            italic_chars += n
        total_chars += n

    if total_chars == 0:
        return False, False

    return (bold_chars / total_chars > 0.5, italic_chars / total_chars > 0.5)


class FontFamily:
    """Vietnamese-capable font family with bold/italic variants."""

    def __init__(self, fonts_dir: str | None = None):
        # Tap hop cac thu muc font theo OS — duyet theo thu tu uu tien.
        # Windows: C:\Windows\Fonts (mac dinh) hoac override.
        # Linux: msttcorefonts hoac fallback DejaVu/Liberation co san.
        # macOS: /Library/Fonts.
        search_dirs: list[str] = []
        if fonts_dir:
            search_dirs.append(fonts_dir)
        elif sys.platform == "win32":
            search_dirs.append(os.path.join(
                os.environ.get("WINDIR", "C:\\Windows"), "Fonts"
            ))
        elif sys.platform == "darwin":
            search_dirs.extend([
                "/Library/Fonts",
                "/System/Library/Fonts",
                os.path.expanduser("~/Library/Fonts"),
            ])
        else:
            # Linux — TeX Live + msttcorefonts + DejaVu/Liberation
            search_dirs.extend([
                "/usr/share/fonts/truetype/msttcorefonts",
                "/usr/share/fonts/truetype/liberation",
                "/usr/share/fonts/truetype/dejavu",
                "/usr/share/fonts",
            ])

        self._fonts: dict[tuple[bool, bool], fitz.Font] = {}
        self._paths: dict[tuple[bool, bool], str] = {}

        # Times New Roman (Windows + msttcorefonts) — uu tien
        variants = {
            (False, False): "times.ttf",
            (True, False):  "timesbd.ttf",
            (False, True):  "timesi.ttf",
            (True, True):   "timesbi.ttf",
        }
        # Linux alternatives: Times New Roman tu msttcorefonts (Times_New_Roman.ttf)
        # hoac Liberation Serif (drop-in metric-compatible replacement)
        linux_variants = {
            (False, False): ["Times_New_Roman.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"],
            (True, False):  ["Times_New_Roman_Bold.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"],
            (False, True):  ["Times_New_Roman_Italic.ttf", "LiberationSerif-Italic.ttf", "DejaVuSerif-Italic.ttf"],
            (True, True):   ["Times_New_Roman_Bold_Italic.ttf", "LiberationSerif-BoldItalic.ttf", "DejaVuSerif-BoldItalic.ttf"],
        }

        def _find_in_dirs(filenames: list[str]) -> str | None:
            for d in search_dirs:
                if not os.path.isdir(d):
                    continue
                for fn in filenames:
                    path = os.path.join(d, fn)
                    if os.path.isfile(path):
                        return path
                # Recursive search 1 level deep (Linux fonts thuong nested)
                try:
                    for sub in os.listdir(d):
                        subpath = os.path.join(d, sub)
                        if os.path.isdir(subpath):
                            for fn in filenames:
                                path = os.path.join(subpath, fn)
                                if os.path.isfile(path):
                                    return path
                except OSError:
                    pass
            return None

        for key, filename in variants.items():
            candidates = [filename] + linux_variants.get(key, [])
            found = _find_in_dirs(candidates)
            if found:
                self._fonts[key] = fitz.Font(fontfile=found)
                self._paths[key] = found

        # Fallback if no Times-family font found
        if not self._fonts:
            fallbacks = [
                "arial.ttf", "segoeui.ttf",  # Windows
                "DejaVuSans.ttf", "LiberationSans-Regular.ttf",  # Linux
            ]
            found = _find_in_dirs(fallbacks)
            if found:
                self._fonts[(False, False)] = fitz.Font(fontfile=found)
                self._paths[(False, False)] = found
            else:
                self._fonts[(False, False)] = fitz.Font("helv")
                self._paths[(False, False)] = ""

    def get(self, bold: bool = False, italic: bool = False) -> fitz.Font:
        """Get font for the given style, with graceful degradation."""
        key = (bold, italic)
        if key in self._fonts:
            return self._fonts[key]
        # Try without italic, then without bold, then regular
        for fallback in [(bold, False), (False, italic), (False, False)]:
            if fallback in self._fonts:
                return self._fonts[fallback]
        return list(self._fonts.values())[0]

    def get_path(self, bold: bool = False, italic: bool = False) -> str:
        """Get font file path for the given style."""
        key = (bold, italic)
        if key in self._paths:
            return self._paths[key]
        for fallback in [(bold, False), (False, italic), (False, False)]:
            if fallback in self._paths:
                return self._paths[fallback]
        return list(self._paths.values())[0] if self._paths else ""


def _is_math_span(span: dict) -> bool:
    """Determine if a text span is mathematical content.

    Layered detection (D1):
      1. Lexical font-name match (regex over expanded font dictionary).
      2. Auxiliary token check for math fonts whose name doesn't contain
         "math" as a literal substring (e.g. "txsy", "stixtwo").
      3. Span flag check: PDF superscript flag is rare in prose but common
         on inline math (exponents, indices).
      4. Unicode-density: if >50 % of the printable characters fall inside
         a math Unicode block, treat the span as math.

    A single match in any tier wins; classification is intentionally
    aggressive on the math side — false positives are isolated by glyph
    and don't bleed into prose translation thanks to per-span chunking.
    """
    font = span.get("font", "") or ""
    if MATH_FONT_PATTERNS.search(font):
        return True

    font_lower = font.lower()
    if any(tok in font_lower for tok in _AUX_MATH_FONT_TOKENS):
        return True

    flags = span.get("flags", 0) or 0
    if flags & _FLAG_SUPERSCRIPT:
        # PyMuPDF marks subscripts/superscripts via this flag; in prose
        # such spans are vanishingly rare, in math they're everywhere.
        return True

    text = (span.get("text", "") or "").strip()
    if not text:
        return False
    # If most characters are math symbols, treat as math
    math_chars = sum(1 for c in text if _is_math_char(c))
    return len(text) > 0 and math_chars / len(text) > 0.5


# ── D2: Geometric baseline analysis ──────────────────────────────
#
# Many PDFs render inline math in body fonts (CMR, Times Italic, etc.) —
# font name alone won't betray them.  But math glyphs almost always sit at
# unusual baselines (subscript/superscript), or have noticeably smaller
# font sizes than the surrounding prose.
#
# We compute, per text line, the dominant baseline & font size, then flag
# spans that deviate as math fragments. Such spans are merged into the
# block's math character count so `is_math` classification picks them up.

_BASELINE_TOLERANCE_PT = 0.8       # vertical wiggle considered "on baseline"
_SUPER_SUB_OFFSET_PT = 1.5         # baseline shift indicating super/subscript
_SMALLER_FONT_RATIO = 0.85         # span is "smaller" if size < 85 % of dominant


def _line_baseline_and_size(line: dict) -> tuple[float, float]:
    """Compute the dominant baseline_y and font_size for a single line.

    Baseline is approximated as the max y of span bboxes (PDF y grows
    downward, so the baseline sits at the bottom of the glyphs). We weight
    by character count so the dominant body text wins over short
    super/subscripts.
    """
    spans = line.get("spans", [])
    if not spans:
        ly0, ly1 = line.get("bbox", (0, 0, 0, 0))[1], line.get("bbox", (0, 0, 0, 0))[3]
        return ly1, 10.0

    weighted_y, weighted_sz, total_w = 0.0, 0.0, 0
    for s in spans:
        text = s.get("text", "") or ""
        n = len(text.strip())
        if n == 0:
            continue
        bb = s.get("bbox") or (0, 0, 0, 0)
        baseline_y = bb[3]  # bottom edge ≈ baseline
        sz = float(s.get("size", 10.0))
        weighted_y += baseline_y * n
        weighted_sz += sz * n
        total_w += n

    if total_w == 0:
        return line["bbox"][3], 10.0
    return weighted_y / total_w, weighted_sz / total_w


def _span_is_geometric_math(span: dict, base_y: float, base_sz: float) -> bool:
    """Return True if a span's geometry strongly suggests math (super/sub
    or anomalously small text mid-line).

    Pure ASCII letters that just happen to be smaller (e.g. footnote
    markers) are skipped via a content guard — only flag when there's at
    least one identifier-like character or math symbol.
    """
    text = (span.get("text", "") or "").strip()
    if not text:
        return False

    bb = span.get("bbox") or (0, 0, 0, 0)
    span_baseline = bb[3]
    span_size = float(span.get("size", base_sz) or base_sz)

    baseline_shift = base_y - span_baseline  # +ve → above baseline (super)
    is_super = baseline_shift > _SUPER_SUB_OFFSET_PT
    is_sub = baseline_shift < -_SUPER_SUB_OFFSET_PT
    is_small = span_size < base_sz * _SMALLER_FONT_RATIO

    if not (is_super or is_sub or is_small):
        return False

    # Content guard: ignore footnote markers like "1", "2" alone — those
    # are usually references, not math. Flag if the span contains any
    # operator, Greek char, or alphabetic identifier of length ≤ 4
    # (typical math variable lengths).
    if any(_is_math_char(c) for c in text):
        return True
    if re.search(r"[+\-=<>≤≥≠≈∞∑∏∫∂∇±×÷^_]", text):
        return True
    # Identifier-only super/subscript ("x", "i", "n", "k"): treat as math
    # when short and baseline-shifted (not just smaller-font).
    if (is_super or is_sub) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,3}", text):
        return True

    return False


def _is_header_footer(block_bbox, page_height, page_width):
    """Detect header/footer blocks by position."""
    _, y0, _, y1 = block_bbox
    margin = page_height * 0.06  # ~6% top/bottom margin
    if y0 < margin or y1 > page_height - margin:
        return True
    return False


class TextBlock:
    """A translatable text block with position metadata."""

    __slots__ = (
        "page_num", "block_idx", "bbox", "text", "spans_info",
        "is_math", "is_translatable", "translated_text",
        "font_name", "font_size", "color",
        "align", "indent",
        # Structural flags (Phase A): mark blocks that sit inside non-text
        # regions so rebuild can skip TextWriter for them and a LaTeX/image
        # render can take over.
        "is_in_table", "is_in_figure", "is_caption", "caption_for",
        # Rotated text (dir != (1, 0)): arXiv banners, vertical watermarks.
        # Preserved as-is in the background; never translated/overlaid.
        "is_rotated",
    )

    # Alignment constants (match fitz.TEXT_ALIGN_*)
    ALIGN_LEFT = 0
    ALIGN_CENTER = 1
    ALIGN_RIGHT = 2
    ALIGN_JUSTIFY = 3

    def __init__(self, page_num, block_idx, bbox, text, spans_info,
                 is_math=False, is_translatable=True,
                 font_name="helv", font_size=10, color=(0, 0, 0),
                 align=0, indent=0.0,
                 is_in_table=False, is_in_figure=False,
                 is_caption=False, caption_for=None,
                 is_rotated=False):
        self.page_num = page_num
        self.block_idx = block_idx
        self.bbox = bbox  # (x0, y0, x1, y1)
        self.text = text
        self.spans_info = spans_info  # list of span dicts from fitz
        self.is_math = is_math
        self.is_translatable = is_translatable
        self.translated_text = None
        self.font_name = font_name
        self.font_size = font_size
        self.color = color
        self.align = align    # fitz.TEXT_ALIGN_* constant
        self.indent = indent  # first-line indent in points
        # Structural flags
        self.is_in_table = is_in_table
        self.is_in_figure = is_in_figure
        self.is_caption = is_caption
        self.caption_for = caption_for  # "table" | "figure" | None
        self.is_rotated = is_rotated

    def to_dict(self):
        return {
            "page_num": self.page_num,
            "block_idx": self.block_idx,
            "bbox": list(self.bbox),
            "text": self.text,
            "is_math": self.is_math,
            "is_translatable": self.is_translatable,
            "font_name": self.font_name,
            "font_size": self.font_size,
            "align": self.align,
            "indent": self.indent,
            "is_in_table": self.is_in_table,
            "is_in_figure": self.is_in_figure,
            "is_caption": self.is_caption,
            "caption_for": self.caption_for,
        }


# Caption detection: matches "Figure 1:", "Fig. 2.", "Table 3 —", "Hình 4:",
# "Bảng 5.", "Biểu đồ 6:". Anchored at start, allows leading whitespace.
_CAPTION_RE = re.compile(
    r"^\s*(Figure|Fig\.?|Table|Hình|Bảng|Biểu\s*đồ)\s*\d+\s*[.:—–\-]",
    re.IGNORECASE,
)


def _classify_caption(text: str) -> str | None:
    """Return "table" or "figure" if `text` opens with a caption marker."""
    if not text:
        return None
    m = _CAPTION_RE.match(text)
    if not m:
        return None
    label = m.group(1).lower().replace(".", "").replace(" ", "")
    if label in ("table", "bảng"):
        return "table"
    return "figure"


def _detect_block_format(
    lines: list[dict], block_bbox: tuple, page_width: float,
) -> tuple[int, float]:
    """Detect paragraph alignment and first-line indent from line positions.

    Analyses horizontal positions of text lines within a block to determine:
    - Alignment: left (0), center (1), right (2), justify (3)
    - First-line indent in points

    Returns ``(align, indent)``.
    """
    # Collect bboxes of lines that contain visible text
    line_bboxes = []
    for line in lines:
        has_text = any(s.get("text", "").strip() for s in line.get("spans", []))
        if has_text:
            line_bboxes.append(line["bbox"])

    if not line_bboxes:
        return 0, 0.0

    bx0, _, bx1, _ = block_bbox
    block_w = bx1 - bx0

    # ── Single-line block ────────────────────────────────────────
    if len(line_bboxes) == 1:
        lx0, _, lx1, _ = line_bboxes[0]
        line_w = lx1 - lx0
        # Short line relative to page?  Check centering.
        if line_w < page_width * 0.6:
            left_margin = lx0
            right_margin = page_width - lx1
            if left_margin > 20 and abs(left_margin - right_margin) < page_width * 0.08:
                return 1, 0.0  # center
        return 0, 0.0  # left (default for single lines)

    # ── Multi-line block ─────────────────────────────────────────
    starts = [lb[0] for lb in line_bboxes]
    ends = [lb[2] for lb in line_bboxes]

    # Variance of start/end positions (exclude last line for ends —
    # the last line of a justified paragraph is typically short)
    start_range = max(starts) - min(starts)
    ends_body = ends[:-1] if len(ends) > 2 else ends
    end_range = max(ends_body) - min(ends_body) if ends_body else 0

    tolerance = max(block_w * 0.05, 4.0)  # 5 % of block width, ≥ 4 pt

    # Justified: both starts AND ends aligned, ≥ 3 lines
    if start_range < tolerance and end_range < tolerance and len(line_bboxes) >= 3:
        indent = 0.0
        if starts[0] - min(starts[1:]) > 3:
            indent = starts[0] - min(starts[1:])
        return 3, indent

    # Center: midpoints aligned but start/end vary
    midpoints = [(s + e) / 2 for s, e in zip(starts, ends)]
    mid_range = max(midpoints) - min(midpoints)
    if mid_range < tolerance and start_range > tolerance:
        return 1, 0.0

    # Right-aligned: ends aligned, starts vary
    if end_range < tolerance and start_range > tolerance:
        return 2, 0.0

    # Left-aligned (default) — check first-line indent
    indent = 0.0
    if len(starts) >= 2 and starts[0] - min(starts[1:]) > 3:
        indent = starts[0] - min(starts[1:])
    return 0, indent


def extract_text_blocks(pdf_path: str) -> list[TextBlock]:
    """Extract all text blocks from a PDF with position and font metadata.

    Returns a list of TextBlock objects, each classified as translatable or not.
    Also tags blocks with structural context: is_in_table, is_in_figure,
    is_caption — used by the rebuild stage to swap-in LaTeX-rendered tables
    and to keep figure/table captions paired with their visual.
    """
    doc = fitz.open(pdf_path)
    blocks = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_height = page.rect.height
        page_width = page.rect.width
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        # Pre-compute non-text obstacle rects once per page so structural
        # flagging is cheap inside the block loop.
        table_rects = _get_table_rects(page)
        image_rects = _get_image_rects(page)

        # Pending captions on this page — paired in a second pass once all
        # blocks have been extracted (we need bboxes for proximity matching).
        page_blocks_start = len(blocks)

        for b_idx, block in enumerate(page_dict["blocks"]):
            # Skip image blocks
            if block["type"] != 0:
                continue

            lines = block.get("lines", [])
            if not lines:
                continue

            bbox = (block["bbox"][0], block["bbox"][1],
                    block["bbox"][2], block["bbox"][3])

            # Collect all spans
            all_spans = []
            full_text_parts = []
            math_span_count = 0
            total_span_count = 0
            # Rotated text (e.g. arXiv banner at left margin reading
            # bottom-to-top) — PyMuPDF reports dir != (1, 0) on those
            # lines. We keep them as-is in the background and skip
            # translation: forcing them through the horizontal overlay
            # path produces a horizontal banner that overlaps the body.
            is_rotated = False

            for line in lines:
                ldir = line.get("dir", (1.0, 0.0))
                # Anything more than ~5° off horizontal counts as rotated.
                if abs(ldir[1]) > 0.1:
                    is_rotated = True
                # D2: per-line baseline + dominant font size for geometric
                # math detection. Computed once per line so super/sub-script
                # spans on the same line share a reference.
                base_y, base_sz = _line_baseline_and_size(line)
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    all_spans.append(span)
                    full_text_parts.append(text)
                    total_span_count += 1
                    if _is_math_span(span):
                        math_span_count += 1
                    elif _span_is_geometric_math(span, base_y, base_sz):
                        # Baseline-shifted or unusually small span — likely
                        # an exponent / index / inline math fragment
                        # rendered in a body font.
                        math_span_count += 1

            full_text = " ".join(full_text_parts).strip()
            if not full_text:
                continue

            # Determine font info: weighted average size, dominant color
            font_name = "helv"
            font_size = 10.0
            color_int = 0
            if all_spans:
                font_name = all_spans[0].get("font", "helv")
                color_int = all_spans[0].get("color", 0)
                # Weighted average font size by character count
                total_len = sum(len(s.get("text", "")) for s in all_spans)
                if total_len > 0:
                    font_size = sum(
                        s.get("size", 10.0) * len(s.get("text", ""))
                        for s in all_spans
                    ) / total_len
                else:
                    font_size = all_spans[0].get("size", 10.0)

            # Convert color int to RGB tuple
            r = (color_int >> 16) & 0xFF
            g = (color_int >> 8) & 0xFF
            b = color_int & 0xFF
            color = (r / 255.0, g / 255.0, b / 255.0)

            # Classify block
            is_math = (total_span_count > 0 and
                       math_span_count / total_span_count > 0.5)
            is_header_footer = _is_header_footer(bbox, page_height, page_width)

            # Short blocks (page numbers, single symbols) are not translatable
            is_too_short = len(full_text.strip()) < 5

            # Detect reference entries — should not be translated
            stripped = full_text.strip()
            is_reference = (
                # "REFERENCES" heading itself
                bool(re.match(
                    r'^R\s*E\s*F\s*E\s*R\s*E\s*N\s*C\s*E\s*S$',
                    stripped, re.IGNORECASE,
                ))
                # Reference entry: starts with [number]
                or bool(re.match(r'^\[\d+\]', stripped))
            )

            is_translatable = (
                not is_math
                and not is_header_footer
                and not is_too_short
                and not is_reference
                and not is_rotated
            )

            # Detect paragraph alignment & first-line indent
            align, indent = _detect_block_format(lines, bbox, page_width)

            # Structural flags
            is_in_table = _bbox_in_rects(bbox, table_rects, threshold=0.3)
            is_in_figure = _bbox_in_rects(bbox, image_rects, threshold=0.3)
            caption_kind = _classify_caption(full_text)
            is_caption = caption_kind is not None

            tb = TextBlock(
                page_num=page_num,
                block_idx=b_idx,
                bbox=bbox,
                text=full_text,
                spans_info=all_spans,
                is_math=is_math,
                is_translatable=is_translatable,
                font_name=font_name,
                font_size=font_size,
                color=color,
                align=align,
                indent=indent,
                is_in_table=is_in_table,
                is_in_figure=is_in_figure,
                is_caption=is_caption,
                caption_for=caption_kind,
                is_rotated=is_rotated,
            )
            blocks.append(tb)

        # ── Caption pairing (per page) ─────────────────────────────
        # Captions outside table/figure rects: link to the nearest rect
        # of the matching kind by vertical proximity. We only USE this
        # metadata downstream — translation still happens.
        page_blocks = blocks[page_blocks_start:]
        for cb in page_blocks:
            if not cb.is_caption:
                continue
            if cb.is_in_table or cb.is_in_figure:
                continue  # already structurally inside the visual
            target_rects = table_rects if cb.caption_for == "table" else image_rects
            if not target_rects:
                continue
            cb_cy = (cb.bbox[1] + cb.bbox[3]) / 2
            # Pick the rect with smallest vertical gap (above OR below the caption)
            best = min(
                target_rects,
                key=lambda r: min(abs(cb_cy - r.y0), abs(cb_cy - r.y1)),
            )
            gap = min(abs(cb_cy - best.y0), abs(cb_cy - best.y1))
            # Only pair if within ~80pt (~1 inch) — otherwise it's a
            # standalone caption-shaped paragraph elsewhere on the page.
            if gap > 80:
                cb.is_caption = False
                cb.caption_for = None

    doc.close()
    return blocks


_DEFAULT_CHUNK_TARGET_SIZE = int(os.getenv("PDF_CHUNK_TARGET_SIZE", "3500"))


def split_blocks_into_chunks(
    blocks: list[TextBlock],
    target_size: int | None = None,
) -> list[list[TextBlock]]:
    """Group consecutive translatable blocks into chunks for translation.

    Each chunk is a list of TextBlock objects whose combined text length
    is approximately target_size characters. When target_size is None,
    falls back to env var PDF_CHUNK_TARGET_SIZE (default 3500) — bigger
    chunks mean fewer Gemini round-trips per page, but raise truncation risk.
    """
    if target_size is None:
        target_size = _DEFAULT_CHUNK_TARGET_SIZE
    translatable = [b for b in blocks if b.is_translatable]
    if not translatable:
        return []

    chunks = []
    current_chunk = []
    current_size = 0

    for block in translatable:
        text_len = len(block.text)
        if current_size + text_len > target_size and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0
        current_chunk.append(block)
        current_size += text_len

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def chunk_to_text(chunk: list[TextBlock]) -> str:
    """Convert a chunk of blocks to a numbered text for translation.

    Format:
        [1] First block text here...
        [2] Second block text here...

    The numbers allow mapping translations back to blocks.
    """
    parts = []
    for i, block in enumerate(chunk):
        parts.append(f"[{i + 1}] {block.text}")
    return "\n\n".join(parts)


# ── E1: Pre-translation length budget ────────────────────────────
#
# Vietnamese typically expands 30-40 % vs English in character count.
# Without a hint, Gemini sometimes produces 2-3× longer renderings that
# overflow the original block, even at minimum font size. We compute a
# physical capacity (chars that fit at the original font size in the
# original bbox) and inject a soft constraint into the prompt:
#
#   "[N] (max ~K chars) original text..."
#
# The inflate factor 1.15 gives the model some headroom; rendering tier
# (E2) handles small overflows via line-height compression.
#
# Caveat: for tiny blocks (captions, single lines) the budget can be
# extremely tight. We clamp to a minimum of 1.1× source length so we
# never ask for shorter-than-source — that would force lossy summary.

_LINE_HEIGHT_DEFAULT = 1.35
_AVG_GLYPH_WIDTH_RATIO = 0.5  # average glyph width / font size (Times-like)
_BUDGET_INFLATE = 1.15        # allow 15 % beyond physical capacity
_BUDGET_MIN_RATIO = 1.1       # never below 110 % of source length

# E4: Table-aware budgets. Cells live inside narrow bboxes and captions
# need to stay compact to preserve their visual pairing with figures —
# both contexts get tighter ratios so the model leans concise instead of
# overflowing the cramped area.
_BUDGET_CELL_INFLATE = 1.05
_BUDGET_CELL_RATIO = 1.0
_BUDGET_CAPTION_INFLATE = 1.08
_BUDGET_CAPTION_RATIO = 1.05


def estimate_block_capacity(block: TextBlock) -> int:
    """Estimate how many characters fit in a block's bbox at its font size.

    Capacity = (lines_available * chars_per_line) where:
        lines_available = bbox_height / (font_size * line_height)
        chars_per_line  = bbox_width  / (font_size * avg_glyph_width)

    Returns 0 if the block has no measurable bbox (caller should treat
    as "no constraint").
    """
    x0, y0, x1, y1 = block.bbox
    width = max(x1 - x0, 0.0)
    height = max(y1 - y0, 0.0)
    fs = max(block.font_size, 4.0)

    char_w = fs * _AVG_GLYPH_WIDTH_RATIO
    line_h = fs * _LINE_HEIGHT_DEFAULT
    if char_w <= 0 or line_h <= 0:
        return 0
    chars_per_line = width / char_w
    lines = height / line_h
    return int(chars_per_line * lines)


def compute_block_budget(block: TextBlock) -> int:
    """Soft character budget for translating a single block.

    Combines physical capacity with a floor based on source length so we
    don't force compression below the source. Tighter ratios apply for
    table cells (cramped bbox) and captions (must stay concise to pair
    with figure visually).
    """
    src_len = len(block.text or "")
    capacity = estimate_block_capacity(block)

    # Pick the right tier of multipliers for this block's role.
    if block.is_in_table:
        inflate = _BUDGET_CELL_INFLATE
        floor_ratio = _BUDGET_CELL_RATIO
        fallback_ratio = 1.15
    elif block.is_caption:
        inflate = _BUDGET_CAPTION_INFLATE
        floor_ratio = _BUDGET_CAPTION_RATIO
        fallback_ratio = 1.2
    else:
        inflate = _BUDGET_INFLATE
        floor_ratio = _BUDGET_MIN_RATIO
        fallback_ratio = 1.4

    if capacity == 0:
        # No measurable box — fall back to source-relative budget
        return int(src_len * fallback_ratio) or 100
    inflated = int(capacity * inflate)
    floor = int(src_len * floor_ratio)
    # Captions and table cells (small bbox) can have capacity < src_len —
    # in that case we'd still rather honor the floor (avoid lossy summary).
    return max(inflated, floor)


def chunk_to_text_with_budget(chunk: list[TextBlock]) -> str:
    """Same as chunk_to_text but each block gets `(max ~N chars)` annotation.

    Used by pipeline._build_prompt when length budgeting is enabled.
    The annotation is a soft hint; the model still prioritises faithful
    translation but will lean concise when given the choice.

    E4: Table cells and captions get extra role tags so the prompt rules
    can target them specifically (e.g. "keep captions in 'Hình N: ...' form").
    """
    parts = []
    for i, block in enumerate(chunk):
        budget = compute_block_budget(block)
        if block.is_in_table:
            tag = f"(table cell, max ~{budget} chars)"
        elif block.is_caption:
            tag = f"(caption, max ~{budget} chars)"
        else:
            tag = f"(max ~{budget} chars)"
        parts.append(f"[{i + 1}] {tag} {block.text}")
    return "\n\n".join(parts)


_CHATBOT_ARTIFACT_RE = re.compile(
    r'^(Bạn có muốn|Lưu ý|Note:|Chú ý:|Would you|Let me know|'
    r'Nếu bạn cần|Hy vọng|Tôi có thể hỗ trợ|Tôi có thể giúp|'
    r'Nếu bạn muốn|Hãy cho tôi biết|If you)',
    re.IGNORECASE,
)


def _strip_chatbot_artifacts(text: str) -> str:
    """Remove chatbot commentary from translated text."""
    lines = text.split("\n")
    clean = []
    for line in lines:
        s = line.strip()
        if _CHATBOT_ARTIFACT_RE.match(s):
            # Skip this line and any following lines until the next [N] marker
            continue
        clean.append(line)
    # Remove trailing blank lines
    while clean and not clean[-1].strip():
        clean.pop()
    return "\n".join(clean)


def parse_translated_chunk(translated_text: str, chunk: list[TextBlock]) -> None:
    """Parse numbered translated text and assign back to blocks.

    Expected format:
        [1] Translated first block...
        [2] Translated second block...

    Block markers [N] appear at the START of a line (or after a blank line).
    Inline citations like [3], [4] inside text must NOT be treated as markers.
    """
    # Split by [N] markers that appear at line start (possibly after whitespace)
    # Use a regex that requires [N] to be at the beginning of a line
    parts = re.split(r'(?:^|\n)\s*\[(\d+)\]\s*', translated_text)
    # parts = ['preamble', '1', 'text1', '2', 'text2', ...]

    # Determine which numbers are valid block markers (1..len(chunk))
    valid_markers = set(range(1, len(chunk) + 1))

    parsed = {}
    i = 1
    while i < len(parts) - 1:
        try:
            marker = int(parts[i])
            text = parts[i + 1].strip()
            if marker in valid_markers:
                # Strip chatbot artifacts from each segment
                text = _strip_chatbot_artifacts(text)
                idx = marker - 1  # 0-based
                parsed[idx] = text
            else:
                # Not a valid block marker — it's a citation like [3], [4]
                # Append it back to the previous block's text
                if parsed:
                    last_idx = max(parsed.keys())
                    parsed[last_idx] += f" [{marker}] {text}"
        except (ValueError, IndexError):
            pass
        i += 2

    # If no markers were parsed at all, try a simpler fallback:
    # treat the entire text as translation for the first block
    if not parsed and len(chunk) == 1:
        text = _strip_chatbot_artifacts(translated_text.strip())
        # Remove "Plaintext" header if present
        if text.lower().startswith("plaintext"):
            text = text[len("plaintext"):].strip()
        parsed[0] = text

    # Assign translations to blocks
    for idx, block in enumerate(chunk):
        if idx in parsed and parsed[idx]:
            block.translated_text = parsed[idx]
        else:
            # Fallback: keep original
            block.translated_text = block.text


def _detect_columns(blocks: list[TextBlock], page_width: float) -> list[tuple[float, float]]:
    """Detect column layout from block positions.

    Returns a list of (col_left, col_right) tuples, e.g.:
    - Single column: [(margin, page_width - margin)]
    - Two columns: [(49, 300), (312, 563)]
    """
    if not blocks:
        return [(0, page_width)]

    # Only consider blocks wide enough to be body text (> 30% page width)
    body_blocks = [b for b in blocks if (b.bbox[2] - b.bbox[0]) > page_width * 0.25]
    if not body_blocks:
        return [(0, page_width)]

    # Collect x0 values of body blocks
    x0_values = sorted(set(round(b.bbox[0]) for b in body_blocks))

    # Cluster x0 values: if there are two distinct groups, it's two-column
    if len(x0_values) >= 2:
        mid = page_width / 2
        left_x0s = [x for x in x0_values if x < mid - 20]
        right_x0s = [x for x in x0_values if x > mid - 20]

        if left_x0s and right_x0s:
            # Two-column layout
            left_blocks = [b for b in body_blocks if round(b.bbox[0]) in left_x0s]
            right_blocks = [b for b in body_blocks if round(b.bbox[0]) in right_x0s]

            left_col = (
                min(b.bbox[0] for b in left_blocks),
                max(b.bbox[2] for b in left_blocks),
            )
            right_col = (
                min(b.bbox[0] for b in right_blocks),
                max(b.bbox[2] for b in right_blocks),
            )
            return [left_col, right_col]

    # Single column
    return [(min(b.bbox[0] for b in body_blocks),
             max(b.bbox[2] for b in body_blocks))]


def _get_block_column(tb: TextBlock, columns: list[tuple[float, float]]) -> int:
    """Determine which column a block belongs to."""
    if len(columns) <= 1:
        return 0
    block_center_x = (tb.bbox[0] + tb.bbox[2]) / 2
    for i, (cl, cr) in enumerate(columns):
        if cl - 10 <= block_center_x <= cr + 10:
            return i
    # Default: nearest column
    dists = [abs(block_center_x - (cl + cr) / 2) for cl, cr in columns]
    return dists.index(min(dists))


def _find_next_block_y(tb: TextBlock, same_col_blocks: list[TextBlock],
                       page_height: float) -> float:
    """Find Y of the next block below in the SAME column."""
    y1 = tb.bbox[3]
    best = page_height - 20

    for other in same_col_blocks:
        if other is tb:
            continue
        other_y0 = other.bbox[1]
        if other_y0 > y1 + 1:
            best = min(best, other_y0 - 2)
            break

    return best


def _estimate_textbox_height(text: str, font: fitz.Font, fontsize: float,
                             rect_width: float, line_height_mult: float = 1.35) -> float:
    """Estimate height needed to render text in a textbox using font metrics.

    `line_height_mult` (E2) controls vertical line spacing — caller can tighten
    from 1.35 → 1.20 → 1.10 to gain capacity before resorting to font shrink.
    """
    line_height = fontsize * line_height_mult
    lines_needed = 0

    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines_needed += 1
            continue
        # Word-wrap estimation: measure words and wrap at rect_width
        words = paragraph.split()
        if not words:
            lines_needed += 1
            continue

        current_line_width = 0.0
        space_width = font.text_length(" ", fontsize=fontsize)
        para_lines = 1

        for word in words:
            try:
                word_width = font.text_length(word, fontsize=fontsize)
            except Exception:
                word_width = len(word) * fontsize * 0.5

            if current_line_width > 0 and current_line_width + space_width + word_width > rect_width:
                para_lines += 1
                current_line_width = word_width
            else:
                current_line_width += (space_width if current_line_width > 0 else 0) + word_width

        lines_needed += para_lines

    return lines_needed * line_height


def _find_best_fit(text: str, font: fitz.Font, orig_fs: float,
                   rect_width: float, rect_height: float,
                   max_expand_y: float) -> tuple[float, float]:
    """Find optimal font size and rect height for text.

    Returns (best_fontsize, best_rect_height).
    """
    # Try original size
    h = _estimate_textbox_height(text, font, orig_fs, rect_width)
    if h <= rect_height:
        return orig_fs, rect_height

    # Try shrinking font (keep original rect)
    for ratio in [0.9, 0.82, 0.75]:
        try_fs = orig_fs * ratio
        h = _estimate_textbox_height(text, font, try_fs, rect_width)
        if h <= rect_height:
            return try_fs, rect_height

    # Expand vertically at 80% font
    best_fs = orig_fs * 0.8
    h = _estimate_textbox_height(text, font, best_fs, rect_width)
    expanded_height = min(h + 5, max_expand_y)
    if expanded_height >= h:
        return best_fs, expanded_height

    # Try 70% in expanded rect
    best_fs = orig_fs * 0.7
    h = _estimate_textbox_height(text, font, best_fs, rect_width)
    if h <= expanded_height:
        return best_fs, expanded_height

    # Last resort: 60% font in max space
    best_fs = orig_fs * 0.6
    h = _estimate_textbox_height(text, font, best_fs, rect_width)
    if h <= expanded_height:
        return best_fs, expanded_height

    return orig_fs * 0.55, expanded_height


def _snapshot_block(page, bbox: tuple, dpi: int = 300) -> bytes:
    """Capture a rectangular region of a page as high-res PNG bytes."""
    clip = fitz.Rect(bbox)
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix.tobytes("png")


def _font_name_for_style(is_bold: bool, is_italic: bool) -> str:
    """Return a PyMuPDF fontname string for the given style."""
    if is_bold and is_italic:
        return "vi-font-bi"
    if is_bold:
        return "vi-font-b"
    if is_italic:
        return "vi-font-i"
    return "vi-font"


# ── Obstacle detection (images & tables) ─────────────────────────


def _get_image_rects(page) -> list:
    """Get bounding boxes of embedded images on the page."""
    rects = []
    try:
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in page_dict.get("blocks", []):
            if block.get("type") == 1:  # image block
                rect = fitz.Rect(block["bbox"])
                if rect.width > 20 and rect.height > 20:
                    rects.append(rect)
    except Exception:
        pass
    return rects


def _get_table_rects(page) -> list:
    """Detect table bounding boxes.

    Two-pass strategy:
      1. PyMuPDF's built-in ``page.find_tables()``.
      2. Fallback: cluster horizontal + vertical ruling lines into bboxes.
         Many academic PDFs draw table borders as sequences of zero-area
         line segments which the built-in finder ignores entirely
         (e.g. arXiv 2502.12525 page 13).
    """
    rects: list = []
    try:
        tabs = page.find_tables()
        for t in tabs.tables:
            rects.append(fitz.Rect(t.bbox))
    except Exception:
        pass

    try:
        ruling_rects = _detect_tables_by_rulings(page)
    except Exception:
        ruling_rects = []

    for rr in ruling_rects:
        # Skip if already covered by an existing rect (>50% overlap)
        skip = False
        for er in rects:
            inter = fitz.Rect(rr) & fitz.Rect(er)
            if inter.is_valid and not inter.is_empty:
                area_rr = max(rr.width * rr.height, 1.0)
                if (inter.width * inter.height) / area_rr > 0.5:
                    skip = True
                    break
        if not skip:
            rects.append(rr)
    return rects


def _detect_tables_by_rulings(page) -> list:
    """Find tables by clustering horizontal + vertical ruling lines.

    A region qualifies as a table when its rulings include at least 2 vertical
    lines AND 3 horizontal lines that share approximately the same x/y span —
    enough evidence of a grid. Returns ``fitz.Rect`` bboxes of detected tables.
    """
    h_lines: list[tuple[float, float, float]] = []  # (y, x0, x1)
    v_lines: list[tuple[float, float, float]] = []  # (x, y0, y1)

    for d in page.get_drawings():
        for it in d.get("items", []):
            if not it or it[0] != "l":
                continue
            p1, p2 = it[1], it[2]
            if abs(p1.y - p2.y) < 0.5 and abs(p1.x - p2.x) >= 20:
                y = (p1.y + p2.y) / 2.0
                x0, x1 = min(p1.x, p2.x), max(p1.x, p2.x)
                h_lines.append((y, x0, x1))
            elif abs(p1.x - p2.x) < 0.5 and abs(p1.y - p2.y) >= 5:
                x = (p1.x + p2.x) / 2.0
                y0, y1 = min(p1.y, p2.y), max(p1.y, p2.y)
                v_lines.append((x, y0, y1))

    if len(h_lines) < 3 or len(v_lines) < 2:
        return []

    # Cluster horizontal lines that share an x-span (same column band)
    # using a simple sweep on y.
    h_lines.sort(key=lambda t: t[0])
    rects: list = []
    used = [False] * len(h_lines)
    for i, (y_i, x0_i, x1_i) in enumerate(h_lines):
        if used[i]:
            continue
        cluster = [(y_i, x0_i, x1_i)]
        used[i] = True
        for j in range(i + 1, len(h_lines)):
            if used[j]:
                continue
            y_j, x0_j, x1_j = h_lines[j]
            # Group lines that share most of their x-span
            inter = min(x1_i, x1_j) - max(x0_i, x0_j)
            span = max(x1_i, x1_j) - min(x0_i, x0_j)
            if span <= 0:
                continue
            if inter / span < 0.7:
                continue
            # And whose y-gap to the cluster's last line is small enough
            if abs(y_j - cluster[-1][0]) > 60:
                continue
            cluster.append((y_j, x0_j, x1_j))
            used[j] = True
        if len(cluster) < 3:
            continue
        ys = [c[0] for c in cluster]
        x0 = min(c[1] for c in cluster)
        x1 = max(c[2] for c in cluster)
        y0, y1 = min(ys), max(ys)
        # Count vertical lines whose span lies within this y-range and inside
        # the cluster's x-range.
        n_v = sum(
            1 for x, vy0, vy1 in v_lines
            if x0 - 2 <= x <= x1 + 2
            and vy0 <= y1 + 2 and vy1 >= y0 - 2
        )
        if n_v < 2:
            continue
        rects.append(fitz.Rect(x0, y0, x1, y1))
    return rects


def _block_in_rects(block: TextBlock, rects: list) -> bool:
    """Check if a block overlaps significantly with any of the given rects.

    Uses intersection area rather than just centre-point to avoid missing
    blocks that sit at cell edges.
    """
    return _bbox_in_rects(block.bbox, rects, threshold=0.3)


def _bbox_in_rects(bbox: tuple, rects: list, threshold: float = 0.3) -> bool:
    """Same as `_block_in_rects` but accepts a raw bbox tuple.

    Used during initial extraction before TextBlock is constructed.
    """
    if not rects:
        return False
    bx0, by0, bx1, by1 = bbox
    barea = max((bx1 - bx0) * (by1 - by0), 1)
    for r in rects:
        ix0 = max(r.x0, bx0)
        iy0 = max(r.y0, by0)
        ix1 = min(r.x1, bx1)
        iy1 = min(r.y1, by1)
        if ix0 < ix1 and iy0 < iy1:
            overlap = (ix1 - ix0) * (iy1 - iy0)
            if overlap / barea > threshold:
                return True
    return False


def _skip_obstacles(
    cursor_y: float,
    col_left: float,
    col_right: float,
    obstacles: list,
    margin: float = 2.0,
) -> float:
    """Advance *cursor_y* past any obstacle that overlaps the column."""
    for _ in range(20):
        moved = False
        for obs in obstacles:
            # Check horizontal overlap with the column
            if obs.x1 <= col_left or obs.x0 >= col_right:
                continue
            # If cursor sits inside the obstacle, jump past it
            if obs.y0 - margin <= cursor_y < obs.y1:
                cursor_y = obs.y1 + margin
                moved = True
        if not moved:
            break
    return cursor_y




# ── LaTeX table compilation ──────────────────────────────────────


def _find_xelatex() -> str | None:
    """Locate the xelatex binary on the system (Windows + Linux + macOS)."""
    path = shutil.which("xelatex")
    if path:
        return path

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            for sub in ("Programs/MiKTeX/miktex/bin/x64",
                         "Programs/MiKTeX 2.9/miktex/bin/x64"):
                candidate = os.path.join(local_app, sub, "xelatex.exe")
                if os.path.isfile(candidate):
                    return candidate
        return None

    # Linux / macOS — TeX Live thuong cai vao /usr/bin hoac /usr/local/bin
    for candidate in (
        "/usr/bin/xelatex",
        "/usr/local/bin/xelatex",
        "/Library/TeX/texbin/xelatex",
        "/usr/local/texlive/2024/bin/x86_64-linux/xelatex",
        "/usr/local/texlive/2023/bin/x86_64-linux/xelatex",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _latex_escape(text: str) -> str:
    """Escape LaTeX special characters in *text*."""
    if not text:
        return ""
    text = text.replace("\\", "\\textbackslash{}")
    for ch in "&%$#_{}":
        text = text.replace(ch, "\\" + ch)
    text = text.replace("~", "\\textasciitilde{}")
    text = text.replace("^", "\\textasciicircum{}")
    return text


def _normalize(s: str) -> str:
    """Collapse whitespace for fuzzy text comparison."""
    return " ".join(s.split()).strip()


def _table_to_latex(table, table_blocks: list[TextBlock]) -> str:
    """Build LaTeX ``tabular`` from a PyMuPDF table, overlaying translations.

    Uses ``table.extract()`` as the structural ground truth (correct rows,
    columns, and original cell text).  For each cell, searches translated
    blocks by **text content similarity** rather than coordinates.
    """
    original_data = table.extract()  # list[list[str | None]]
    if not original_data:
        return ""

    row_count = len(original_data)
    col_count = max(len(row) for row in original_data)

    # ── Build translation lookup by normalized original text ──────
    # Map normalized original text → translated text
    trans_map: dict[str, str] = {}
    for b in table_blocks:
        if b.is_translatable and b.translated_text and b.text:
            key = _normalize(b.text)
            if key:
                trans_map[key] = b.translated_text

    # ── Detect header style ───────────────────────────────────────
    header_bold = False
    for b in table_blocks:
        if b.bbox[1] < table.bbox[1] + (table.bbox[3] - table.bbox[1]) * 0.15:
            is_b, _ = _classify_block_style(b.spans_info)
            if is_b:
                header_bold = True
                break

    # ── Build LaTeX ───────────────────────────────────────────────
    col_spec = "|" + "|".join(["l"] * col_count) + "|"
    lines = [
        f"\\begin{{tabular}}{{{col_spec}}}",
        "\\hline",
    ]

    for r, row in enumerate(original_data):
        cells_tex: list[str] = []
        for c in range(col_count):
            raw = row[c] if c < len(row) and row[c] else ""
            # Try to find translation by text content
            key = _normalize(raw)
            if key and key in trans_map:
                cell_text = trans_map[key]
            else:
                cell_text = raw
            escaped = _latex_escape(cell_text.strip())
            # Flatten to single line per cell
            escaped = " ".join(escaped.split())
            if r == 0 and header_bold:
                escaped = f"\\textbf{{{escaped}}}"
            cells_tex.append(escaped)

        lines.append(" & ".join(cells_tex) + " \\\\")
        lines.append("\\hline")

    lines.append("\\end{tabular}")
    return "\n".join(lines)


def _compile_table_to_image(
    latex_table: str,
    work_dir: str,
    target_width_pt: float,
) -> tuple[bytes, float, float] | None:
    """Compile a LaTeX table snippet → PNG.  Returns ``(png, w_pt, h_pt)``.

    Renders at fixed 288 DPI for crisp output.  The returned w_pt/h_pt
    are the natural dimensions of the compiled table in PDF points.
    """
    xelatex = _find_xelatex()
    if not xelatex:
        return None

    tmp = os.path.abspath(os.path.join(work_dir, "_table_compile"))
    os.makedirs(tmp, exist_ok=True)

    tex = (
        "\\documentclass[border=2pt]{standalone}\n"
        "\\usepackage{fontspec}\n"
        "\\setmainfont{Times New Roman}\n"
        "\\usepackage{array}\n"
        "\\renewcommand{\\arraystretch}{1.3}\n"
        "\\begin{document}\n"
        f"{latex_table}\n"
        "\\end{document}\n"
    )
    tex_path = os.path.join(tmp, "table.tex")
    pdf_path = os.path.join(tmp, "table.pdf")

    try:
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(tex)

        subprocess.run(
            [xelatex, "-interaction=nonstopmode", "table.tex"],
            capture_output=True, timeout=60, cwd=tmp,
        )

        if not os.path.exists(pdf_path):
            return None

        doc = fitz.open(pdf_path)
        pg = doc[0]
        # Natural dimensions of the compiled table
        w_pt = pg.rect.width
        h_pt = pg.rect.height
        # Render at fixed high DPI for quality
        dpi = 288
        pix = pg.get_pixmap(dpi=dpi)
        img = pix.tobytes("png")
        doc.close()
        return img, w_pt, h_pt

    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _find_page_tables(page) -> list[tuple]:
    """Return ``[(fitz.Rect, Table), ...]`` for every detected table."""
    try:
        finder = page.find_tables()
        return [(fitz.Rect(t.bbox), t) for t in finder.tables]
    except Exception:
        return []


# ── Band-based layout helpers ────────────────────────────────────


def _detect_layout_bands(
    content_blocks: list[TextBlock],
    page_width: float,
) -> list[tuple[bool, list[TextBlock]]]:
    """Split page blocks into vertical bands with consistent layout.

    Academic papers often have a mixed layout: full-width title/abstract
    at the top, then two-column body text below.  This function detects
    these regions so each band is reflowed with the correct column model.

    A block is "full-width" if its width exceeds 60 % of the page width.
    Consecutive blocks of the same type (full-width vs narrow) form a band.

    Returns:
        List of ``(is_full_width, [blocks])`` tuples, ordered top-to-bottom.
    """
    sorted_blocks = sorted(content_blocks, key=lambda b: b.bbox[1])
    if not sorted_blocks:
        return []

    bands: list[tuple[bool, list[TextBlock]]] = []
    current_fw: bool | None = None
    current_band: list[TextBlock] = []

    for b in sorted_blocks:
        is_fw = (b.bbox[2] - b.bbox[0]) > page_width * 0.6
        if is_fw != current_fw and current_band:
            bands.append((current_fw, current_band))  # type: ignore[arg-type]
            current_band = []
        current_fw = is_fw
        current_band.append(b)

    if current_band:
        bands.append((current_fw, current_band))  # type: ignore[arg-type]

    return bands


def _inter_block_gaps(blocks: list[TextBlock]) -> list[float]:
    """Calculate original vertical gaps between consecutive blocks."""
    gaps: list[float] = []
    for i in range(len(blocks) - 1):
        gap = blocks[i + 1].bbox[1] - blocks[i].bbox[3]
        gaps.append(max(gap, 1.5))
    return gaps


def _detect_body_font_size(blocks: list[TextBlock]) -> float:
    """Find the dominant body-text font size among translatable blocks.

    Rounds sizes to the nearest 0.5 pt, then returns the most frequent.
    Used to render all body text at a uniform size.
    """
    from collections import Counter

    sizes: list[float] = []
    for b in blocks:
        if b.is_translatable:
            sizes.append(round(b.font_size * 2) / 2)
    if not sizes:
        return 10.0
    return Counter(sizes).most_common(1)[0][0]


def _effective_fs(block: TextBlock, body_fs: float) -> float:
    """Return the font size to use for *block*.

    Blocks whose original size is close to *body_fs* get *body_fs* for
    visual consistency.  Larger blocks (headings, ≥ 1.3× body) keep
    their original size.
    """
    if block.font_size > body_fs * 1.2:
        return block.font_size  # heading — preserve original
    return body_fs


def _compute_scale(
    blocks: list[TextBlock],
    snapshots: dict[int, bytes],
    font_family: FontFamily,
    col_width: float,
    available_height: float,
    body_fs: float = 0,
) -> tuple[float, float]:
    """Compute uniform (scale, line_height) so blocks fit in *available_height*.

    E2/E3 escalation order:
        1. scale=1.0, line_height=1.35 — original layout.
        2. scale=1.0, line_height=1.20 — tighten lines first (typography).
        3. scale=1.0, line_height=1.10 — dense lines.
        4. binary-search scale ∈ [0.78, 1.0] at line_height=1.10.

    Image/snapshot blocks keep their original height (never scaled).
    Returns (scale, line_height); both consumed downstream by _render_block.
    """
    gaps = _inter_block_gaps(blocks)

    def _total_at(s: float, lh: float) -> float:
        total = sum(gaps)
        for b in blocks:
            if id(b) in snapshots:
                total += b.bbox[3] - b.bbox[1]
            else:
                is_b, is_i = _classify_block_style(b.spans_info)
                font = font_family.get(bold=is_b, italic=is_i)
                fs = _effective_fs(b, body_fs) * s if body_fs else b.font_size * s
                total += _estimate_textbox_height(
                    b.translated_text, font, fs, col_width, lh,
                )
        return total

    if available_height <= 0:
        return 1.0, _LINE_HEIGHT

    # Tier 1 — original
    if _total_at(1.0, _LINE_HEIGHT) <= available_height:
        return 1.0, _LINE_HEIGHT

    # Tier 2 — tighten line-height only
    if _total_at(1.0, _LINE_HEIGHT_TIGHT) <= available_height:
        return 1.0, _LINE_HEIGHT_TIGHT

    # Tier 3 — dense line-height
    if _total_at(1.0, _LINE_HEIGHT_DENSE) <= available_height:
        return 1.0, _LINE_HEIGHT_DENSE

    # Tier 4 — shrink font, dense line-height. Floor at 78 % for legibility.
    lo, hi = _FONT_SHRINK_FLOOR, 1.0
    for _ in range(12):
        mid = (lo + hi) / 2
        if _total_at(mid, _LINE_HEIGHT_DENSE) <= available_height:
            lo = mid
        else:
            hi = mid
    return lo, _LINE_HEIGHT_DENSE


_LINE_HEIGHT = 1.35           # default — matches _estimate_textbox_height
_LINE_HEIGHT_TIGHT = 1.20     # E2 first-tier compression
_LINE_HEIGHT_DENSE = 1.10     # E2 second-tier compression (last resort)
_FONT_SHRINK_FLOOR = 0.78     # E3 don't go below 78 % of body — readability


def _render_block(
    page,
    block: TextBlock,
    cursor_y: float,
    col_left: float,
    col_right: float,
    scale: float,
    snapshots: dict[int, bytes],
    font_family: FontFamily,
    body_fs: float = 0,
    line_height: float = _LINE_HEIGHT,
    max_y: float | None = None,
) -> float:
    """Render a single block at *cursor_y*.  Returns the new cursor_y.

    Overflow remedy ladder (E2 + E3):
        0. NEW — vertical expansion at same font size, if `max_y` headroom
           allows. Preferred because keeping font size constant avoids the
           visible inconsistency that bothers readers when individual blocks
           are scaled down inside an otherwise full-size page.
        1. Render at original font + caller-supplied line-height.
        2. Block-local fallback ladder:
             a. tighten to 1.20 line-height
             b. 90 % font at 1.20 line-height
             c. 85 % font at 1.10 line-height
             d. 78 % font (E3 floor) at 1.10 line-height
    Each tier's textbox is sized using the same line-height that will be
    used for actual rendering, so estimates stay accurate.

    Floor of 78 % keeps body text legible (smaller looks comically tiny
    next to figure captions and headings); residual overflow is
    accepted as a known tradeoff to avoid native reflow.
    """
    col_width = col_right - col_left

    if id(block) in snapshots:
        # Non-translatable: insert high-DPI snapshot image
        orig_w = block.bbox[2] - block.bbox[0]
        orig_h = block.bbox[3] - block.bbox[1]
        x0 = block.bbox[0]  # keep original X position
        target = fitz.Rect(x0, cursor_y, x0 + orig_w, cursor_y + orig_h)
        page.insert_image(target, stream=snapshots[id(block)])
        return cursor_y + orig_h

    # ── Translated text (TextWriter for line-height control) ─────
    text = block.translated_text
    is_bold, is_italic = _classify_block_style(block.spans_info)
    font = font_family.get(bold=is_bold, italic=is_italic)
    base_fs = _effective_fs(block, body_fs) if body_fs else block.font_size
    fs = base_fs * scale
    align = block.align  # detected from original

    # Simulate first-line indent by prepending spaces
    if block.indent > 0 and fs > 0:
        space_w = font.text_length(" ", fontsize=fs)
        if space_w > 0:
            n_spaces = max(1, round(block.indent / space_w))
            text = " " * n_spaces + text

    text_h = _estimate_textbox_height(text, font, fs, col_width, line_height)
    text_rect = fitz.Rect(col_left, cursor_y, col_right, cursor_y + text_h + 2)

    tw = fitz.TextWriter(page.rect)
    overflow = tw.fill_textbox(
        text_rect, text, font=font, fontsize=fs,
        align=align, lineheight=line_height,
    )
    tw.write_text(page, color=block.color)

    if not overflow:
        return cursor_y + text_h

    # ── Block-local overflow remedy ladder ────────────────────────
    # White-out the failed render once; subsequent tiers redact only if
    # they fall through too.
    page.add_redact_annot(text_rect + (-1, -1, 1, 1), fill=(1, 1, 1))
    page.apply_redactions()

    # ── Tier 0 — vertical expansion, keep font size ──────────────
    # Most overflows happen because `_estimate_textbox_height` underestimates
    # (word-wrap edge cases). Before resorting to font shrinking — which
    # produces visible size inconsistency between blocks — try giving the
    # textbox more vertical room at the SAME font size and SAME line-height.
    # Only fall through to shrinking when the column has no spare vertical
    # space left (next band/page boundary would otherwise be crossed).
    if max_y is not None and max_y > cursor_y + text_h + 5:
        available_y = max_y - cursor_y - 2
        # Cap growth at 2× the original estimate — beyond that, real
        # length issue, not just an estimator miss; better to shrink.
        target_h = min(text_h * 2.0, available_y)
        if target_h > text_h + 5:
            expanded_rect = fitz.Rect(
                col_left, cursor_y, col_right, cursor_y + target_h,
            )
            exp_tw = fitz.TextWriter(page.rect)
            residual = exp_tw.fill_textbox(
                expanded_rect, text, font=font, fontsize=fs,
                align=align, lineheight=line_height,
            )
            if not residual:
                exp_tw.write_text(page, color=block.color)
                # Re-estimate actual height with the same line-height so
                # the next block doesn't start with a phantom gap.
                actual_h = _estimate_textbox_height(
                    text, font, fs, col_width, line_height,
                )
                return cursor_y + min(actual_h + 2, target_h)
            # Tier 0 still overflowed — clear and fall through to shrink
            page.add_redact_annot(expanded_rect + (-1, -1, 1, 1), fill=(1, 1, 1))
            page.apply_redactions()

    # Tier definitions: (font_scale, line_height) — only run tiers
    # tighter than what the band already chose, otherwise we're not
    # gaining anything by retrying.
    tiers = [
        (1.00, _LINE_HEIGHT_TIGHT),
        (0.90, _LINE_HEIGHT_TIGHT),
        (0.85, _LINE_HEIGHT_DENSE),
        (_FONT_SHRINK_FLOOR, _LINE_HEIGHT_DENSE),
    ]

    last_text_h = text_h
    for fs_ratio, lh in tiers:
        if fs_ratio >= 1.0 and lh >= line_height:
            continue  # band-level was already at or tighter than this
        try_fs = fs * fs_ratio
        try_h = _estimate_textbox_height(text, font, try_fs, col_width, lh)
        try_rect = fitz.Rect(col_left, cursor_y, col_right, cursor_y + try_h + 2)
        try_tw = fitz.TextWriter(page.rect)
        residual = try_tw.fill_textbox(
            try_rect, text, font=font, fontsize=try_fs,
            align=align, lineheight=lh,
        )
        last_text_h = try_h
        if not residual:
            try_tw.write_text(page, color=block.color)
            return cursor_y + try_h
        # Tier didn't fit either — clear and try the next one
        page.add_redact_annot(try_rect + (-1, -1, 1, 1), fill=(1, 1, 1))
        page.apply_redactions()

    # Last resort: render at floor anyway (some clipping accepted)
    final_fs = fs * _FONT_SHRINK_FLOOR
    final_h = _estimate_textbox_height(text, font, final_fs, col_width, _LINE_HEIGHT_DENSE)
    final_rect = fitz.Rect(col_left, cursor_y, col_right, cursor_y + final_h + 2)
    final_tw = fitz.TextWriter(page.rect)
    final_tw.fill_textbox(
        final_rect, text, font=font, fontsize=final_fs,
        align=align, lineheight=_LINE_HEIGHT_DENSE,
    )
    final_tw.write_text(page, color=block.color)
    return cursor_y + max(final_h, last_text_h)


# ── Main rebuild function ────────────────────────────────────────


def _apply_translation_provenance(doc, meta: dict | None) -> None:
    """Stamp PDF metadata (Tier 1) + draw footer on every page (Tier 2).

    Footer: gray scriptsize text at bottom-left, page number at bottom-right.
    Dùng Times New Roman (qua FontFamily) để hiển thị đúng diacritics tiếng Việt;
    helv built-in không có glyph 'ả/ị/ờ' v.v.
    Vô hại nếu meta=None hoặc gặp lỗi — log warning, không raise.
    """
    if not meta:
        return
    try:
        doc.set_metadata(format_pdf_metadata(meta))
    except Exception as e:
        print(f"[translation_meta] set_metadata failed: {e}")

    footer_text = format_pdf_footer(meta)
    margin_x = 36.0   # 0.5 inch from left/right
    margin_y = 18.0   # 0.25 inch from bottom
    color = (0.5, 0.5, 0.5)  # gray
    fontsize = 7.0

    # Times New Roman (hoặc Liberation Serif fallback) — hỗ trợ tiếng Việt đầy đủ.
    # Nếu không tìm thấy font nào thì fallback về helv → footer hiển thị thiếu dấu
    # nhưng vẫn không crash pipeline.
    font_family = FontFamily()
    font_path = font_family.get_path(bold=False, italic=False)
    use_custom = bool(font_path)
    fontname = "TimesVN" if use_custom else "helv"
    fontfile = font_path if use_custom else None

    for page_idx in range(len(doc)):
        try:
            page = doc[page_idx]
            pw = page.rect.width
            ph = page.rect.height
            y_baseline = ph - margin_y

            tw = fitz.TextWriter(page.rect, color=color)
            page_label = f"{page_idx + 1}/{len(doc)}"

            if use_custom:
                font_obj = font_family.get(bold=False, italic=False)
                tw.append((margin_x, y_baseline), footer_text,
                          font=font_obj, fontsize=fontsize)
                label_w = font_obj.text_length(page_label, fontsize=fontsize)
                tw.append((pw - margin_x - label_w, y_baseline), page_label,
                          font=font_obj, fontsize=fontsize)
                tw.write_text(page)
            else:
                # Last-resort fallback (no Vietnamese-capable font found).
                page.insert_text((margin_x, y_baseline), footer_text,
                                 fontname="helv", fontsize=fontsize,
                                 color=color, overlay=True)
                label_w = len(page_label) * fontsize * 0.5
                page.insert_text((pw - margin_x - label_w, y_baseline),
                                 page_label, fontname="helv",
                                 fontsize=fontsize, color=color, overlay=True)
        except Exception as e:
            print(f"[translation_meta] footer draw failed on page {page_idx}: {e}")


def rebuild_pdf(
    original_pdf: str,
    blocks: list[TextBlock],
    output_path: str,
    vietnamese_font_path: str | None = None,
    translation_meta: dict | None = None,
) -> str:
    """Rebuild PDF with reflowed translated text using band-based layout.

    Key features:
    - **Band-based layout**: Detects full-width sections (title, abstract)
      vs multi-column body text and reflows each band correctly.
    - **Obstacle awareness**: Detects embedded images and table regions.
      Reflow cursor skips past images; table text is placed in its
      original cell position (not reflowed).
    - **Uniform scaling**: If a band overflows, all text in that band is
      scaled uniformly (no per-block font size variation).
    - **Line-height control**: Uses TextWriter with explicit lineheight
      for readable spacing.
    """
    doc = fitz.open(original_pdf)
    font_family = FontFamily()

    # Group blocks by page
    all_page_blocks: dict[int, list[TextBlock]] = {}
    for b in blocks:
        all_page_blocks.setdefault(b.page_num, []).append(b)

    for page_num in range(len(doc)):
        blocks_on_page = all_page_blocks.get(page_num, [])
        if not blocks_on_page:
            continue

        page = doc[page_num]
        pw = page.rect.width
        ph = page.rect.height

        # Separate headers/footers (untouched) from content
        content_blocks = [
            b for b in blocks_on_page
            if not _is_header_footer(b.bbox, ph, pw)
        ]
        if not content_blocks:
            continue

        has_translatable = any(
            b.is_translatable and b.translated_text for b in content_blocks
        )
        if not has_translatable:
            continue

        # ── Detect images and tables ─────────────────────────────────
        image_rects = _get_image_rects(page)
        page_tables = _find_page_tables(page)
        table_rects = [r for r, _ in page_tables]
        # Images are NO LONGER static obstacles — they reflow with cursor
        obstacles = []

        # Separate table blocks from reflow blocks
        table_blocks = [b for b in content_blocks
                        if _block_in_rects(b, table_rects)]
        reflow_blocks = [b for b in content_blocks
                         if not _block_in_rects(b, table_rects)]

        # ── Pre-compile table images via LaTeX ────────────────────────
        work_dir = os.path.dirname(output_path) or "."
        # table_images: table_rect index → (png_bytes, w_pt, h_pt)
        table_images: dict[int, tuple[bytes, float, float]] = {}
        has_xelatex = _find_xelatex() is not None

        if has_xelatex:
            for ti, (table_rect, table_obj) in enumerate(page_tables):
                tblks = [b for b in table_blocks
                         if _block_in_rects(b, [table_rect])]
                has_translated = any(
                    b.translated_text
                    for b in tblks if b.is_translatable
                )
                if not has_translated:
                    continue
                latex = _table_to_latex(table_obj, tblks)
                if not latex:
                    continue
                result = _compile_table_to_image(
                    latex, work_dir, table_rect.width,
                )
                if result:
                    table_images[ti] = result
                    print(f"[rebuild_pdf] Compiled table {ti} "
                          f"({table_obj.row_count}x{table_obj.col_count})")

        # ── Step 1a: snapshot non-translatable REFLOW blocks ──────────
        snapshots: dict[int, bytes] = {}
        for b in reflow_blocks:
            if not (b.is_translatable and b.translated_text):
                snapshots[id(b)] = _snapshot_block(page, b.bbox)

        # ── Step 1b: snapshot embedded images (before white-out) ─────
        # Capture each image as high-res PNG so we can re-insert at the
        # correct reflowed position (like tables), instead of leaving
        # them at fixed original coordinates.
        # image_snapshots: list of (y_mid, x_center, png_bytes, rect)
        image_snapshots: list[tuple[float, float, bytes, fitz.Rect]] = []
        for ir in image_rects:
            png = _snapshot_block(page, tuple(ir), dpi=200)
            y_mid = (ir.y0 + ir.y1) / 2
            x_center = (ir.x0 + ir.x1) / 2
            image_snapshots.append((y_mid, x_center, png, ir))
        image_snapshots.sort(key=lambda t: t[0])

        # ── Step 2: white-out ────────────────────────────────────────
        # White-out ALL content blocks (reflow + table)
        for b in content_blocks:
            page.add_redact_annot(
                fitz.Rect(b.bbox) + (-1, -1, 1, 1), fill=(1, 1, 1),
            )
        # White-out table borders/lines for compiled tables
        for ti in table_images:
            tr = table_rects[ti]
            page.add_redact_annot(
                fitz.Rect(tr) + (-2, -2, 2, 2), fill=(1, 1, 1),
            )
        # White-out original image positions (they'll be re-inserted at
        # reflowed cursor positions in Step 3)
        for ir in image_rects:
            page.add_redact_annot(
                fitz.Rect(ir) + (-1, -1, 1, 1), fill=(1, 1, 1),
            )

        page.apply_redactions()

        # ── Step 3: reflow ALL blocks (text + tables) with band layout
        if not reflow_blocks and not table_images:
            continue

        # Dominant body font size for this page (consistent sizing)
        body_fs = _detect_body_font_size(reflow_blocks)

        # Build column-aware table insertion list.
        # Each entry: (y_mid, x_center, ti, img_bytes, w_pt, h_pt)
        table_entries: list[tuple[float, float, int, bytes, float, float]] = []
        for ti, (img_bytes, img_w, img_h) in table_images.items():
            tr = table_rects[ti]
            y_mid = (tr.y0 + tr.y1) / 2
            x_center = (tr.x0 + tr.x1) / 2
            table_entries.append((y_mid, x_center, ti, img_bytes, img_w, img_h))
        table_entries.sort(key=lambda t: t[0])
        inserted_tables: set[int] = set()

        def _insert_table_image(cur_y: float, left: float, right: float,
                                ti: int, img_bytes: bytes,
                                img_w: float, img_h: float) -> float:
            """Insert a compiled table image, centred in [left, right]."""
            col_w = right - left
            target_w = min(img_w, col_w)
            sf = target_w / img_w if img_w > 0 else 1
            actual_w = img_w * sf
            actual_h = img_h * sf
            x0 = left + (col_w - actual_w) / 2
            cur_y += 6  # gap before table
            insert_rect = fitz.Rect(
                x0, cur_y, x0 + actual_w, cur_y + actual_h,
            )
            page.insert_image(insert_rect, stream=img_bytes)
            inserted_tables.add(ti)
            return cur_y + actual_h + 6  # gap after table

        def _insert_tables_for_region(cur_y: float, left: float,
                                      right: float,
                                      before_y: float) -> float:
            """Insert pending tables whose x-center is in [left,right]
            and y_mid < before_y."""
            for ym, xc, ti, ib, iw, ih in table_entries:
                if ti in inserted_tables:
                    continue
                if ym > before_y:
                    continue
                if left <= xc <= right:
                    cur_y = _insert_table_image(
                        cur_y, left, right, ti, ib, iw, ih)
            return cur_y

        inserted_images: set[int] = set()

        def _insert_image_at_cursor(cur_y: float, left: float,
                                    right: float, idx: int,
                                    png: bytes, rect: fitz.Rect) -> float:
            """Insert a captured image centred in [left, right] at cursor."""
            col_w = right - left
            orig_w = rect.width
            orig_h = rect.height
            target_w = min(orig_w, col_w)
            sf = target_w / orig_w if orig_w > 0 else 1
            actual_w = orig_w * sf
            actual_h = orig_h * sf
            x0 = left + (col_w - actual_w) / 2
            cur_y += 4  # gap before image
            insert_rect = fitz.Rect(
                x0, cur_y, x0 + actual_w, cur_y + actual_h,
            )
            page.insert_image(insert_rect, stream=png)
            inserted_images.add(idx)
            return cur_y + actual_h + 4  # gap after image

        def _insert_images_for_region(cur_y: float, left: float,
                                      right: float,
                                      before_y: float) -> float:
            """Insert pending images whose original y_mid < before_y
            and whose x_center is in [left, right]."""
            for ii, (ym, xc, png, rect) in enumerate(image_snapshots):
                if ii in inserted_images:
                    continue
                if ym > before_y:
                    continue
                # Image overlaps this column?
                if rect.x1 <= left or rect.x0 >= right:
                    continue
                cur_y = _insert_image_at_cursor(
                    cur_y, left, right, ii, png, rect)
            return cur_y

        def _insert_floats_for_region(cur_y: float, left: float,
                                      right: float,
                                      before_y: float) -> float:
            """Insert both pending tables and images before_y."""
            cur_y = _insert_tables_for_region(cur_y, left, right, before_y)
            cur_y = _insert_images_for_region(cur_y, left, right, before_y)
            return cur_y

        bands = _detect_layout_bands(reflow_blocks, pw)
        bottom_y = ph - ph * 0.06  # ~6 % bottom margin

        cursor_y: float | None = None

        for band_idx, (is_full_width, band_blocks) in enumerate(bands):
            if not band_blocks:
                continue

            if cursor_y is None:
                cursor_y = band_blocks[0].bbox[1]

            # Inter-band gap
            if band_idx > 0:
                prev_last = bands[band_idx - 1][1][-1]
                gap = band_blocks[0].bbox[1] - prev_last.bbox[3]
                cursor_y += max(gap, 2.0)

            if cursor_y >= bottom_y:
                break

            # ── Full-width band ──────────────────────────────────────
            if is_full_width:
                left = min(b.bbox[0] for b in band_blocks)
                right = max(b.bbox[2] for b in band_blocks)
                col_w = right - left

                remaining = bottom_y - cursor_y
                scale, line_h = _compute_scale(
                    band_blocks, snapshots, font_family,
                    col_w, remaining, body_fs=body_fs,
                )

                gaps = _inter_block_gaps(band_blocks)
                for i, b in enumerate(band_blocks):
                    if cursor_y >= bottom_y:
                        break
                    # Insert tables that belong before this block
                    cursor_y = _insert_floats_for_region(
                        cursor_y, left, right, b.bbox[1])
                    cursor_y = _skip_obstacles(
                        cursor_y, left, right, obstacles)
                    cursor_y = _render_block(
                        page, b, cursor_y, left, right,
                        scale, snapshots, font_family,
                        body_fs=body_fs, line_height=line_h,
                        max_y=bottom_y,
                    )
                    if i < len(gaps):
                        cursor_y += gaps[i]

                # Insert tables after the last block in this band
                next_y = (bands[band_idx + 1][1][0].bbox[1]
                          if band_idx + 1 < len(bands)
                          else float('inf'))
                cursor_y = _insert_floats_for_region(
                    cursor_y, left, right, next_y)

            # ── Multi-column band ────────────────────────────────────
            else:
                columns = _detect_columns(band_blocks, pw)

                col_block_map: dict[int, list[TextBlock]] = {}
                for b in band_blocks:
                    ci = _get_block_column(b, columns)
                    col_block_map.setdefault(ci, []).append(b)
                for ci in col_block_map:
                    col_block_map[ci].sort(key=lambda b: b.bbox[1])

                # Uniform scale across columns
                remaining = bottom_y - cursor_y
                col_results: list[tuple[float, float]] = []
                for ci, (cl, cr) in enumerate(columns):
                    cblocks = col_block_map.get(ci, [])
                    if cblocks:
                        result = _compute_scale(
                            cblocks, snapshots, font_family,
                            cr - cl, remaining, body_fs=body_fs,
                        )
                        col_results.append(result)
                # Worst-case scale and tightest line-height across columns
                scale = min((r[0] for r in col_results), default=1.0)
                line_h = min((r[1] for r in col_results), default=_LINE_HEIGHT)

                max_cursor = cursor_y
                for ci, (cl, cr) in enumerate(columns):
                    cblocks = col_block_map.get(ci, [])
                    if not cblocks:
                        continue

                    col_cursor = cursor_y
                    gaps = _inter_block_gaps(cblocks)
                    for i, b in enumerate(cblocks):
                        if col_cursor >= bottom_y:
                            break
                        # Insert tables in THIS column before block
                        col_cursor = _insert_floats_for_region(
                            col_cursor, cl, cr, b.bbox[1])
                        col_cursor = _skip_obstacles(
                            col_cursor, cl, cr, obstacles)
                        col_cursor = _render_block(
                            page, b, col_cursor, cl, cr,
                            scale, snapshots, font_family,
                            body_fs=body_fs, line_height=line_h,
                            max_y=bottom_y,
                        )
                        if i < len(gaps):
                            col_cursor += gaps[i]

                    # Insert tables after last block in this column
                    col_cursor = _insert_floats_for_region(
                        col_cursor, cl, cr, float('inf'))
                    max_cursor = max(max_cursor, col_cursor)

                cursor_y = max_cursor

        # Insert any remaining tables and images that weren't placed
        if cursor_y is not None:
            remaining_tables = [
                e for e in table_entries if e[2] not in inserted_tables
            ]
            for ym, xc, ti, ib, iw, ih in remaining_tables:
                cursor_y = _insert_table_image(
                    cursor_y, pw * 0.1, pw * 0.9, ti, ib, iw, ih)
            for ii, (ym, xc, png, rect) in enumerate(image_snapshots):
                if ii not in inserted_images:
                    cursor_y = _insert_image_at_cursor(
                        cursor_y, pw * 0.1, pw * 0.9, ii, png, rect)

    # Save
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    _apply_translation_provenance(doc, translation_meta)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    return output_path


# ── In-place rebuild quality helpers ─────────────────────────────
# Each helper addresses one of the four visible defects users hit when a long
# document was rebuilt by the inplace path:
#   Bug A — narrow tall blocks (sidebar / page-edge text) wrapped one
#           character per line, rendering Vietnamese unreadable.
#   Bug B — translated text overflowed downward and overlapped the next
#           block because expansion ignored neighbors.
#   Bug C — adjacent bullet items chose different shrink ratios independently,
#           making list items visibly mismatched in size.
#   Bug D — joined TOC entries wrapped on width, breaking the "X.Y Title"
#           per-line layout the original PDF used.

# Matches a section marker like "1.2 ", "12.3.4 ", "A.1 " preceded by text and
# followed by an uppercase Vietnamese word — i.e. the start of a TOC entry that
# got concatenated with the previous one.
_TOC_SECTION_MARKER = re.compile(
    r'(?<=\S)\s+(\d+(?:\.\d+){1,3}\s+)'
    r'(?=[A-ZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐĨŨƠƯẾỄỀỆỐỒỘỚỜỞỠỢỨỪỬỮỰ])'
)


def _split_toc_text(text: str) -> str:
    """Inject newlines before "X.Y" section markers inside a joined TOC block.

    Many TOC blocks come out of extraction as a single paragraph because the
    leader dots look like word separators. ``fill_textbox`` then wraps on
    width, not section boundaries, producing the broken-mosaic look. We only
    intervene when at least two section markers are present (single headings
    don't need splitting)."""
    if not text or len(text) < 30:
        return text
    if len(_TOC_SECTION_MARKER.findall(text)) < 2:
        return text
    return _TOC_SECTION_MARKER.sub(r'\n\1', text)


def _is_narrow_vertical_block(block: TextBlock) -> bool:
    """Detect blocks too narrow to hold a Vietnamese word — they would render
    one character per line. Threshold: ~25pt (≈4 Latin chars at 10pt). Also
    catches narrow tall sidebar/watermark blocks where the aspect ratio alone
    is a giveaway."""
    x0, y0, x1, y1 = block.bbox
    width = x1 - x0
    height = y1 - y0
    if width < 25:
        return True
    if width < 40 and height > width * 2.5:
        return True
    return False


def _region_has_colored_fill(
    page, rect: fitz.Rect, threshold: float = 0.05,
) -> bool:
    """Decide whether ``rect`` looks like a diagram (colored shapes or
    borders) rather than a plain text table.

    Plain academic tables have no fills and only thin black/grey grid lines.
    Figures like BABOK's process diagrams have boxes with colored backgrounds
    OR colored borders (teal, blue…). Triggers on either:
    - filled (non-white) coverage > ``threshold`` (5 %% default), OR
    - 2+ vector shapes with chromatic (non-greyscale) strokes inside ``rect``.
    """
    region_area = rect.width * rect.height
    if region_area <= 0:
        return False
    try:
        drawings = page.get_drawings()
    except Exception:
        return False

    def _is_chromatic(c) -> bool:
        """Non-white colour with some hue (R/G/B spread > 0.1). Pure
        greys/blacks return False — they're typical table grid lines."""
        if not isinstance(c, (tuple, list)) or len(c) < 3:
            return False
        r, g, b = c[0], c[1], c[2]
        if r >= 0.95 and g >= 0.95 and b >= 0.95:
            return False
        return (max(r, g, b) - min(r, g, b)) > 0.1

    filled_area = 0.0
    colored_stroke_shapes = 0

    for d in drawings:
        drect = d.get("rect")
        if drect is None:
            continue
        inter = fitz.Rect(drect) & rect
        if inter.is_empty:
            continue

        fill = d.get("fill")
        if fill and isinstance(fill, (tuple, list)) and len(fill) >= 3 \
                and not all(c >= 0.98 for c in fill[:3]):
            filled_area += inter.width * inter.height
            if filled_area / region_area > threshold:
                return True

        # Border-only diagrams (BABOK Figure 1.4.1 style): boxes are
        # white-filled but stroked in teal. Counting chromatic strokes
        # catches them where fill detection alone misses.
        if _is_chromatic(d.get("color")):
            colored_stroke_shapes += 1
            if colored_stroke_shapes >= 2:
                return True

    return (filled_area / region_area) > threshold


def _find_diagram_clusters(
    page,
    min_shapes: int = 3,
    min_size: float = 80.0,
    proximity: float = 30.0,
) -> list[fitz.Rect]:
    """Find clusters of coloured vector shapes that look like figures.

    BABOK-style diagrams (boxes connected by arrows) aren't grids, so
    ``page.find_tables()`` often misses them. This is a second line of
    defence: union-find clustering of chromatic drawings, returning the
    bounding rect of each cluster.

    A drawing joins the candidate set when it has a chromatic stroke OR
    a non-white fill. Two candidates merge into the same cluster when
    their rects (inflated by ``proximity``) intersect. Clusters with
    fewer than ``min_shapes`` members, or whose bounding box is smaller
    than ``min_size`` on either axis, are discarded so single-page
    decorations don't trigger snapshots.
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    def _is_chromatic(c):
        if not isinstance(c, (tuple, list)) or len(c) < 3:
            return False
        r, g, b = c[0], c[1], c[2]
        if r >= 0.95 and g >= 0.95 and b >= 0.95:
            return False
        return (max(r, g, b) - min(r, g, b)) > 0.1

    rects: list[fitz.Rect] = []
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        rect = fitz.Rect(r)
        if rect.is_empty or rect.width < 2 or rect.height < 2:
            continue
        fill = d.get("fill")
        has_non_white_fill = bool(
            fill and isinstance(fill, (tuple, list)) and len(fill) >= 3
            and not all(c >= 0.98 for c in fill[:3])
        )
        if _is_chromatic(d.get("color")) or has_non_white_fill:
            rects.append(rect)

    if len(rects) < min_shapes:
        return []

    parents = list(range(len(rects)))

    def find(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def union(i: int, j: int) -> None:
        pi, pj = find(i), find(j)
        if pi != pj:
            parents[pi] = pj

    for i in range(len(rects)):
        ai = rects[i] + (-proximity, -proximity, proximity, proximity)
        for j in range(i + 1, len(rects)):
            if ai.intersects(rects[j]):
                union(i, j)

    clusters: dict[int, list[fitz.Rect]] = {}
    for i, r in enumerate(rects):
        clusters.setdefault(find(i), []).append(r)

    result: list[fitz.Rect] = []
    for shapes in clusters.values():
        if len(shapes) < min_shapes:
            continue
        bb = fitz.Rect(shapes[0])
        for r in shapes[1:]:
            bb |= r
        if bb.width < min_size or bb.height < min_size:
            continue
        # Small padding so border strokes aren't clipped at snapshot edges.
        result.append(bb + (-4, -4, 4, 4))
    return result


def _compute_neighbor_y_bound(blocks: list[TextBlock]) -> dict:
    """For each block, find the y0 of the next block sharing >=30% of the
    column width. Returns dict[id(block)] → max_y. Used to cap downward
    expansion so translated text doesn't overlap the block below.

    Falls back to ``bbox[3] + 50% height`` when no neighbor is found — same
    as the previous hard-coded behaviour."""
    bounds = {}
    by_y = sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
    for i, b in enumerate(by_y):
        x0, y0, x1, y1 = b.bbox
        # Generous default: Vietnamese often needs 2-3 lines where English
        # used 1 (diacritics + longer words). Capped by neighbor_y, so this
        # only matters when there's whitespace below the block.
        default_max = y1 + (y1 - y0) * 2.0
        max_y = default_max
        for other in by_y[i+1:]:
            ox0, oy0, ox1, oy1 = other.bbox
            if oy0 <= y1:
                continue
            overlap_w = min(x1, ox1) - max(x0, ox0)
            min_w = min(x1 - x0, ox1 - ox0)
            if min_w <= 0 or overlap_w / min_w <= 0.3:
                continue
            max_y = min(default_max, oy0 - 1.5)
            break
        bounds[id(b)] = max_y
    return bounds


# Reduced ladder: snapping to fewer levels means two adjacent blocks at the
# same original size are far more likely to land on the same final size, so
# the visible "font jumps" in bullet lists goes away. 0.85 covers most mild
# overflow cases; 0.70 is the floor for very long translations.
_INPLACE_SHRINK_LEVELS = (1.00, 0.85, 0.70)


def rebuild_pdf_inplace(
    original_pdf: str,
    blocks: list[TextBlock],
    output_path: str,
    translation_meta: dict | None = None,
) -> str:
    """Rebuild PDF using in-place swap: keep original layout 100%, only replace text.

    Unlike the reflow-based rebuild_pdf():
    - Images, lines, backgrounds stay exactly where they are (never touched).
    - Each text block is white-outed at its original bbox, then translated text
      is inserted at the same position with the original font size.
    - No reflow, no band layout, no scale computation → fonts are uniform.
    - If translated text overflows the block height, font is shrunk in steps.

    Tables: detected via PyMuPDF, compiled as LaTeX ``tabular`` → PNG, pasted
    at the original table rect.  This preserves row/column structure that
    per-cell TextWriter often breaks.  If xelatex is unavailable or
    compilation fails, falls back to per-cell text replacement (the existing
    in-place behaviour) so the rebuild never aborts on missing toolchain.
    """
    doc = fitz.open(original_pdf)
    font_family = FontFamily()

    # Group blocks by page
    by_page: dict[int, list[TextBlock]] = {}
    for b in blocks:
        by_page.setdefault(b.page_num, []).append(b)

    work_dir = os.path.dirname(output_path) or "."
    has_xelatex = _find_xelatex() is not None

    for page_num, page_blocks in by_page.items():
        translatable = [b for b in page_blocks if b.is_translatable and b.translated_text]
        if not translatable:
            continue

        page = doc[page_num]

        # ── Step -1: snapshot narrow-vertical blocks BEFORE redaction ──
        # Bug A: blocks too narrow for Vietnamese text would render one char
        # per line. Capture their pixmap now, then paste back after redaction
        # so the original rendering is preserved untranslated. Skipping these
        # is preferable to producing unreadable output.
        narrow_blocks = [b for b in translatable if _is_narrow_vertical_block(b)]
        narrow_snapshots: dict[int, bytes] = {}
        for b in narrow_blocks:
            try:
                rect = fitz.Rect(b.bbox)
                if rect.width > 1 and rect.height > 1:
                    narrow_snapshots[id(b)] = page.get_pixmap(
                        clip=rect, dpi=300
                    ).tobytes("png")
            except Exception:
                pass

        # ── Step 0: compile tables to LaTeX → PNG (if xelatex available)
        # Any successfully compiled table will replace its entire region.
        # Failed tables fall through to per-cell TextWriter (no whiteout
        # of the table rect → original cell layout stays).
        compiled_tables: list[tuple[fitz.Rect, bytes]] = []  # (rect, png)
        covered_rects: list[fitz.Rect] = []

        table_candidates = _find_page_tables(page)

        # ── Step 0a: snapshot diagrams masquerading as tables ──
        # BABOK et al. have figures (colored boxes + arrows) that
        # PyMuPDF's table finder picks up. Translating them as LaTeX
        # tables strips the colour and shape, leaving an ugly grid.
        # Snapshot the page region at the original coordinates and
        # paste it back over the redacted area instead — same approach
        # as Bug A (narrow vertical blocks). Detection: any candidate
        # rect with colour fills covering > 5 % of its area is treated
        # as a figure rather than a text table.
        for table_rect, _table_obj in table_candidates:
            if not _region_has_colored_fill(page, table_rect):
                continue
            try:
                png_bytes = page.get_pixmap(
                    clip=table_rect, dpi=200
                ).tobytes("png")
                compiled_tables.append((table_rect, png_bytes))
                covered_rects.append(table_rect)
                print(
                    f"[rebuild_pdf_inplace] Snapshot figure on page "
                    f"{page_num} ({table_rect.width:.0f}x"
                    f"{table_rect.height:.0f}pt)"
                )
            except Exception as e:
                print(f"[rebuild_pdf_inplace] Figure snapshot failed: {e}")

        # ── Step 0a-bis: diagram clusters (BABOK Figure 1.4.1 style) ──
        # `find_tables` misses figures that aren't grids. Cluster chromatic
        # vector shapes and snapshot the bounding box. Skip clusters that
        # overlap a table candidate (Step 0a already handled those).
        for cluster_rect in _find_diagram_clusters(page):
            if any(
                cluster_rect.intersects(tr)
                and (cluster_rect & tr).get_area()
                > 0.3 * min(cluster_rect.get_area(), tr.get_area())
                for tr, _ in table_candidates
            ):
                continue
            if any(
                cluster_rect.intersects(cr)
                and (cluster_rect & cr).get_area()
                > 0.3 * min(cluster_rect.get_area(), cr.get_area())
                for cr in covered_rects
            ):
                continue
            try:
                png_bytes = page.get_pixmap(
                    clip=cluster_rect, dpi=200
                ).tobytes("png")
                compiled_tables.append((cluster_rect, png_bytes))
                covered_rects.append(cluster_rect)
                print(
                    f"[rebuild_pdf_inplace] Snapshot diagram cluster on "
                    f"page {page_num} ({cluster_rect.width:.0f}x"
                    f"{cluster_rect.height:.0f}pt)"
                )
            except Exception as e:
                print(f"[rebuild_pdf_inplace] Cluster snapshot failed: {e}")

        # ── Step 0b: compile remaining (real) tables to LaTeX → PNG ──
        if has_xelatex:
            for table_rect, table_obj in table_candidates:
                # Skip rects already captured as figure snapshots
                if any(
                    table_rect.intersects(cr)
                    and (table_rect & cr).get_area()
                    > 0.5 * table_rect.get_area()
                    for cr in covered_rects
                ):
                    continue
                tblks = [b for b in page_blocks
                         if _block_in_rects(b, [table_rect])]
                has_translated = any(
                    b.translated_text for b in tblks if b.is_translatable
                )
                if not has_translated:
                    continue
                # Skip text-heavy tables: long descriptive cells (e.g.
                # BABOK BACCM "Change/Description") flatten to a single
                # very wide LaTeX row that scales tiny when pasted back.
                # Per-cell TextWriter (the default fallback) keeps the
                # original cell rects and reads better.
                try:
                    cells = table_obj.extract()
                    max_cell_len = max(
                        (len(c or "") for row in cells for c in row),
                        default=0,
                    )
                except Exception:
                    max_cell_len = 0
                if max_cell_len > 80:
                    print(
                        f"[rebuild_pdf_inplace] Skip LaTeX for text-heavy "
                        f"table on page {page_num} "
                        f"(max cell {max_cell_len} chars)"
                    )
                    continue
                latex = _table_to_latex(table_obj, tblks)
                if not latex:
                    continue
                result = _compile_table_to_image(
                    latex, work_dir, table_rect.width,
                )
                if result:
                    png_bytes, _, _ = result
                    compiled_tables.append((table_rect, png_bytes))
                    covered_rects.append(table_rect)
                    print(
                        f"[rebuild_pdf_inplace] Compiled table on page "
                        f"{page_num} "
                        f"({table_obj.row_count}x{table_obj.col_count})"
                    )

        def _is_covered(b: TextBlock) -> bool:
            return bool(covered_rects) and _block_in_rects(b, covered_rects)

        # ── Display-text snapshot (before redaction) ──
        # Cover-page titles and other huge headings (>24pt) often can't fit
        # a Vietnamese translation in their tight original bbox, so
        # `fill_textbox` raises ValueError and the `insert_text` fallback
        # also fails silently — leaving the page blank. Snapshot these
        # blocks now so we can paste the original back over them when
        # Step 2b rendering fails. Body text isn't worth snapshotting
        # (it almost always fits and snapshots are slow).
        display_snapshots: dict[int, bytes] = {}
        for b in translatable:
            if _is_covered(b) or id(b) in narrow_snapshots:
                continue
            if float(b.font_size or 0) <= 24:
                continue
            try:
                br = fitz.Rect(b.bbox)
                if br.width > 1 and br.height > 1:
                    display_snapshots[id(b)] = page.get_pixmap(
                        clip=br, dpi=200
                    ).tobytes("png")
            except Exception:
                pass

        # ── Step 1: white-out text blocks not covered by a compiled table
        for b in translatable:
            if _is_covered(b):
                continue
            page.add_redact_annot(
                fitz.Rect(b.bbox) + (-0.5, -0.5, 0.5, 0.5),
                fill=(1, 1, 1),
            )
        # White-out the entire region of each compiled table (text + borders)
        for tr in covered_rects:
            page.add_redact_annot(
                fitz.Rect(tr) + (-2, -2, 2, 2),
                fill=(1, 1, 1),
            )
        page.apply_redactions()

        # ── Step 2a: paste compiled table images at original positions ──
        for table_rect, png_bytes in compiled_tables:
            page.insert_image(
                table_rect, stream=png_bytes, keep_proportion=True,
            )

        # ── Step 2a': paste narrow-vertical snapshots back (Bug A) ──
        for b in narrow_blocks:
            png = narrow_snapshots.get(id(b))
            if png is not None:
                try:
                    page.insert_image(fitz.Rect(b.bbox), stream=png)
                except Exception:
                    pass

        # Pre-compute downward expansion limits for every block (Bug B):
        # each block's max_y caps how far it can grow before colliding with
        # the next block in the same column.
        text_blocks = [
            b for b in translatable
            if not _is_covered(b) and id(b) not in narrow_snapshots
        ]
        neighbor_y = _compute_neighbor_y_bound(text_blocks)

        # Bug C — uniform body font size across the page. Per-block extraction
        # noise (anti-aliasing, span variants) produces slightly different
        # `b.font_size` values for what is visually the same paragraph style;
        # rendering at those raw values makes adjacent bullets look mismatched.
        # `_effective_fs` snaps non-heading blocks to the dominant body size.
        body_fs = _detect_body_font_size(text_blocks) or 10.0

        # Page-uniform body fs: pre-measure every non-heading block's needed
        # font size. Earlier we used min() — one demanding block dragged the
        # whole page down, leaving most blocks visibly smaller than they
        # needed to be. Switched to the 20th percentile: render at a size
        # that ~80% of blocks fit comfortably; the remaining ~20% (worst
        # outliers) truncate via silent fill_textbox cutoff. Floor at
        # body_fs*0.85 so the page never shrinks more than 15% even when
        # most blocks are demanding.
        needed_fs_list: list[float] = []
        for b in text_blocks:
            if b.font_size > body_fs * 1.2:
                continue  # heading — shrinks independently below
            text_pre = _split_toc_text(b.translated_text)
            is_b_pre, is_i_pre = _classify_block_style(b.spans_info)
            font_pre = font_family.get(bold=is_b_pre, italic=is_i_pre)
            x0p, y0p, x1p, y1p = b.bbox
            rect_w_pre = x1p - x0p
            if rect_w_pre <= 0:
                continue
            orig_h_pre = y1p - y0p
            max_y_pre = neighbor_y.get(id(b), y1p + orig_h_pre * 0.5)
            avail_h_pre = max(max_y_pre - y0p, orig_h_pre)
            fs_pre = body_fs
            while fs_pre > body_fs * 0.70:
                tw_pre = font_pre.text_length(text_pre, fontsize=fs_pre)
                est_lines_pre = max(1, math.ceil(
                    tw_pre * 1.1 / rect_w_pre
                ))
                needed_h_pre = est_lines_pre * fs_pre * _LINE_HEIGHT
                if needed_h_pre <= avail_h_pre:
                    break
                fs_pre *= 0.92
            needed_fs_list.append(fs_pre)

        if needed_fs_list:
            needed_fs_list.sort()
            # 20th percentile from bottom: 20% of blocks lie below this fs
            # (they'll truncate); 80% have slack at this fs.
            idx = max(0, min(
                len(needed_fs_list) - 1,
                int(len(needed_fs_list) * 0.2),
            ))
            page_body_fs = needed_fs_list[idx]
        else:
            page_body_fs = body_fs
        page_body_fs = max(page_body_fs, body_fs * 0.85)

        # ── Step 2b: insert translated text at exact original position ──
        # No shrink ladder — user wants visual consistency. Each block writes
        # at its effective size (body_fs for body text, original for headings).
        # If translated text overflows the expanded rect, residual content
        # is truncated rather than scaled down.
        failed_display_blocks: list[TextBlock] = []
        for b in text_blocks:
            # Bug D: split joined TOC entries before any wrapping happens.
            text = _split_toc_text(b.translated_text)
            is_bold, is_italic = _classify_block_style(b.spans_info)
            font = font_family.get(bold=is_bold, italic=is_italic)

            x0, y0, x1, y1 = b.bbox
            orig_h = y1 - y0
            max_y = neighbor_y.get(id(b), y1 + orig_h * 0.5)
            available_h = max(max_y - y0, orig_h)

            # Heading keeps its original size (shrinks-to-fit independently
            # below). Body uses the page-uniform pre-measured size so
            # adjacent paragraphs render at the same fs.
            if b.font_size > body_fs * 1.2:
                fs = b.font_size
            else:
                fs = page_body_fs
            rect = fitz.Rect(x0, y0, x1, y0 + available_h)

            # Heading shrink-to-fit: large headings (>1.3× body) often overflow
            # rect width because Vietnamese accents add girth (e.g. "BABOK"
            # 5 chars at 36pt vs "BABOK®" diacritics, or "Preface" → "Lời nói
            # đầu"). For headings only, scale fs down until the translation
            # fits on one rendered line. Body text is left alone — wrapping
            # is fine there and we want uniform body sizing.
            if fs > body_fs * 1.2 and rect.width > 0:
                target_w = rect.width - 2  # small margin
                while fs > page_body_fs and font.text_length(text, fontsize=fs) > target_w:
                    fs *= 0.92
                fs = max(fs, page_body_fs)

            # Guard: PyMuPDF refuses to start text when the rect is shorter
            # than one line at the requested font size. Shrink fs until the
            # rect can hold at least one line (floor at 5pt — anything
            # smaller is illegible and not worth rendering).
            min_line_h = fs * _LINE_HEIGHT
            while rect.height < min_line_h and fs > 5.0:
                fs *= 0.85
                min_line_h = fs * _LINE_HEIGHT
            if rect.height < min_line_h or rect.width < fs * 0.5:
                # Still too small even at floor — record for snapshot fallback.
                if id(b) in display_snapshots:
                    failed_display_blocks.append(b)
                continue

            # Multi-line fit: estimate wrapped height; shrink fs until text
            # fits in rect.height. Without this, `fill_textbox` silently
            # truncates trailing lines — losing the last word(s) of section
            # headings ("Hướng dẫn và Công" missing "cụ") and body
            # paragraphs ("Phân tích Kinh" missing "doanh"). Floor at 6pt
            # so we still render *something* readable for pathological
            # cases. Width-overflow safety margin (×1.1) gives word breaks
            # room to push a fragment to the next line.
            if rect.width > 0:
                # Floor at page_body_fs for both heading and body: heading
                # should never render smaller than body. If a heading can't
                # fit at page_body_fs (long heading in narrow column), it
                # truncates via fill_textbox rather than shrinking below
                # body — keeps the visual hierarchy intact.
                floor = page_body_fs
                while fs > floor:
                    total_w = font.text_length(text, fontsize=fs)
                    est_lines = max(1, math.ceil(
                        total_w * 1.1 / rect.width
                    ))
                    needed_h = est_lines * fs * _LINE_HEIGHT
                    if needed_h <= rect.height:
                        break
                    fs *= 0.92

            tw = fitz.TextWriter(page.rect)
            try:
                tw.fill_textbox(
                    rect, text, font=font, fontsize=fs,
                    align=b.align, lineheight=_LINE_HEIGHT,
                )
                tw.write_text(page, color=b.color)
            except ValueError:
                # "Text must start in rectangle" — fall back to a left-aligned
                # insert_text at the block origin so the page still has the
                # translation even if alignment isn't honored.
                try:
                    page.insert_text(
                        (x0, y0 + fs * 0.9), text,
                        fontname=font.name, fontfile=None,
                        fontsize=fs, color=b.color,
                    )
                except Exception:
                    if id(b) in display_snapshots:
                        failed_display_blocks.append(b)

        # Paste display snapshots back over blocks where rendering failed.
        # Preserves the original (English) display text rather than leaving
        # the cover/title page visually blank.
        for b in failed_display_blocks:
            png = display_snapshots.get(id(b))
            if png is None:
                continue
            try:
                page.insert_image(fitz.Rect(b.bbox), stream=png)
            except Exception:
                pass

    _apply_translation_provenance(doc, translation_meta)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
    return output_path


def get_pdf_info(pdf_path: str) -> dict:
    """Get basic info about a PDF (page count, has text, title, etc.)."""
    doc = fitz.open(pdf_path)
    info = {
        "page_count": len(doc),
        "has_text": False,
        "total_chars": 0,
        "title": "",
    }
    for page in doc:
        text = page.get_text().strip()
        if text:
            info["has_text"] = True
            info["total_chars"] += len(text)

    # Strategy 1: PDF metadata title
    meta_title = (doc.metadata or {}).get("title", "").strip()
    if meta_title and len(meta_title) > 5 and meta_title.lower() not in ("untitled", "microsoft word"):
        info["title"] = meta_title[:200]
        doc.close()
        return info

    # Strategy 2: Largest-font text block on page 1
    if len(doc) > 0:
        page = doc[0]
        page_width = page.rect.width
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        # Collect all blocks with their max font size and y-position
        block_candidates = []
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            max_size = 0.0
            parts = []
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    fs = span.get("size", 0)
                    t = span.get("text", "").strip()
                    if t:
                        max_size = max(max_size, fs)
                        parts.append(t)
            text = " ".join(parts).strip()
            if max_size > 0 and len(text) > 5:
                bbox = block.get("bbox", (0, 0, 0, 0))
                block_candidates.append((max_size, bbox[1], text, bbox))

        if block_candidates:
            # Find the maximum font size among all blocks
            max_font = max(c[0] for c in block_candidates)

            if max_font > 10:  # sanity check — some PDFs have tiny-font everything
                # Collect blocks with font size >= 85% of max, in top half of page
                page_height = page.rect.height
                threshold = max_font * 0.85
                title_blocks = [
                    c for c in block_candidates
                    if c[0] >= threshold and c[1] < page_height * 0.55
                ]
                # Sort by y position (top to bottom)
                title_blocks.sort(key=lambda c: c[1])

                if title_blocks:
                    # Merge adjacent title blocks (multi-line titles)
                    combined = " ".join(c[2] for c in title_blocks)
                    info["title"] = combined[:200]

    doc.close()
    return info
