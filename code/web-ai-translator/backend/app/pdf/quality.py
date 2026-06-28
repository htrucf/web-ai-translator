"""Translation quality checker — heuristic-based, no AI calls.

Runs locally after translation to flag potential issues:
- Untranslated blocks (still English)
- Suspiciously short/long translations
- Glossary compliance
- Numbers preservation

Separate module — called by pipeline after rebuild, does not affect translation.
"""

import re
from dataclasses import dataclass, field


@dataclass
class QualityIssue:
    """A single quality issue found in translation."""
    severity: str          # "error", "warning", "info"
    category: str          # "untranslated", "length", "glossary", "numbers"
    page: int              # 0-based page number
    block_idx: int         # block index on page
    message: str           # human-readable description
    original: str = ""     # original text snippet
    translated: str = ""   # translated text snippet


@dataclass
class QualityReport:
    """Full quality report for a translated document."""
    total_blocks: int = 0
    translatable_blocks: int = 0
    translated_blocks: int = 0
    untranslated_blocks: int = 0
    issues: list = field(default_factory=list)
    score: float = 100.0   # 0-100, 100 = perfect

    def to_dict(self) -> dict:
        return {
            "total_blocks": self.total_blocks,
            "translatable_blocks": self.translatable_blocks,
            "translated_blocks": self.translated_blocks,
            "untranslated_blocks": self.untranslated_blocks,
            "score": round(self.score, 1),
            "issue_count": len(self.issues),
            "issues_by_severity": {
                "error": sum(1 for i in self.issues if i.severity == "error"),
                "warning": sum(1 for i in self.issues if i.severity == "warning"),
                "info": sum(1 for i in self.issues if i.severity == "info"),
            },
            "issues_by_category": _count_by_category(self.issues),
            "issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "page": i.page + 1,  # 1-based for display
                    "message": i.message,
                    "original": i.original[:200],
                    "translated": i.translated[:200],
                }
                for i in self.issues
            ],
        }


def _count_by_category(issues: list) -> dict:
    counts = {}
    for i in issues:
        counts[i.category] = counts.get(i.category, 0) + 1
    return counts


# ── Detection helpers ─────────────────────────────────────────

_VIETNAMESE_RE = re.compile(
    r'[àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợ'
    r'ùúủũụưứừửữựỳýỷỹỵđÀÁẢÃẠĂẮẰẲẴẶÂẤẦẨẪẬÈÉẺẼẸÊẾỀỂỄỆ'
    r'ÌÍỈĨỊÒÓỎÕỌÔỐỒỔỖỘƠỚỜỞỠỢÙÚỦŨỤƯỨỪỬỮỰỲÝỶỸỴĐ]'
)

_ENGLISH_WORD_RE = re.compile(r'\b[a-zA-Z]{4,}\b')

# Patterns that are OK to leave untranslated
_SECTION_HEADING_RE = re.compile(
    r'^\d+(\.\d+)*\s+\w', re.IGNORECASE
)
_FIGURE_TABLE_RE = re.compile(
    r'^(Figure|Fig\.|Table|Algorithm|Appendix)\s', re.IGNORECASE
)
_TECHNICAL_LABEL_RE = re.compile(
    r'^[A-Z][a-z]+ [a-z]+ [a-z]+$'  # short descriptive labels
)


def _has_vietnamese(text: str) -> bool:
    """Check if text contains Vietnamese characters."""
    return bool(_VIETNAMESE_RE.search(text))


def _english_word_ratio(text: str) -> float:
    """Ratio of English words (4+ chars) in text."""
    words = text.split()
    if not words:
        return 0.0
    english_words = _ENGLISH_WORD_RE.findall(text)
    return len(english_words) / len(words)


def _is_likely_untranslated(original: str, translated: str) -> bool:
    """Check if a block was not actually translated.

    Compares original and translated — if they're essentially the same
    English text, the block wasn't translated.
    """
    if not translated:
        return True

    # If translated text contains Vietnamese, it was translated
    if _has_vietnamese(translated):
        return False

    # If original and translated are identical or nearly identical
    orig_norm = " ".join(original.lower().split())
    trans_norm = " ".join(translated.lower().split())
    if orig_norm == trans_norm:
        return True

    # High English word ratio with no Vietnamese = not translated
    if _english_word_ratio(translated) > 0.6:
        return True

    return False


def _classify_untranslated(original: str) -> tuple[str, str]:
    """Classify an untranslated block to determine severity.

    Returns (severity, reason).
    Short headings, labels, and technical terms get lower severity.
    Long paragraphs that weren't translated are errors.
    """
    text = original.strip()
    word_count = len(text.split())

    # Very short text (< 5 words): likely a label, heading, or term
    if word_count <= 4:
        return "info", "ngắn (tiêu đề/nhãn)"

    # Section headings: "2.1 Feature Attribution Methods"
    if _SECTION_HEADING_RE.match(text):
        return "warning", "tiêu đề phần"

    # Figure/Table captions
    if _FIGURE_TABLE_RE.match(text):
        return "info", "chú thích hình/bảng"

    # Short blocks (5-15 words): warning
    if word_count <= 15:
        return "warning", "đoạn ngắn"

    # Long paragraph: this is a real problem
    return "error", "đoạn dài chưa dịch"


# ── Main quality check ────────────────────────────────────────

def check_translation_quality(
    blocks: list,
    glossary: dict | None = None,
) -> QualityReport:
    """Run heuristic quality checks on translated blocks.

    Args:
        blocks: List of TextBlock objects (after translation applied).
        glossary: Optional EN→VI glossary dict for compliance checking.

    Returns:
        QualityReport with issues and score.
    """
    report = QualityReport()
    report.total_blocks = len(blocks)

    translatable = [b for b in blocks if b.is_translatable]
    report.translatable_blocks = len(translatable)

    if not translatable:
        return report

    penalty = 0.0  # Total penalty to subtract from 100
    # Weight penalties by block text length (long blocks matter more)
    total_chars = sum(len((b.text or "").strip()) for b in translatable)
    if total_chars == 0:
        total_chars = 1  # avoid division by zero

    for b in translatable:
        original = (b.text or "").strip()
        translated = (b.translated_text or "").strip()

        if not original:
            continue

        block_weight = len(original) / total_chars  # how important is this block

        # ── Check 1: Untranslated blocks ──────────────────
        if _is_likely_untranslated(original, translated):
            report.untranslated_blocks += 1
            severity, reason = _classify_untranslated(original)

            # Penalty scales with block importance
            if severity == "error":
                p = 30 * block_weight  # long untranslated paragraphs hurt a lot
            elif severity == "warning":
                p = 10 * block_weight
            else:
                p = 2 * block_weight   # short labels barely matter

            penalty += p

            if not translated:
                msg = f"Block không được dịch — trống ({reason})"
            else:
                msg = f"Block có vẻ chưa được dịch ({reason})"

            report.issues.append(QualityIssue(
                severity=severity,
                category="untranslated",
                page=b.page_num,
                block_idx=b.block_idx,
                message=msg,
                original=original[:100],
                translated=translated[:100],
            ))
            continue

        report.translated_blocks += 1

        # ── Check 2: Length ratio ─────────────────────────
        if len(original) > 30:  # only check for substantial blocks
            ratio = len(translated) / len(original)
            if ratio < 0.3:
                report.issues.append(QualityIssue(
                    severity="warning",
                    category="length",
                    page=b.page_num,
                    block_idx=b.block_idx,
                    message=f"Bản dịch quá ngắn ({ratio:.0%} so với gốc)",
                    original=original[:100],
                    translated=translated[:100],
                ))
                penalty += 15 * block_weight
            elif ratio > 3.0:
                report.issues.append(QualityIssue(
                    severity="warning",
                    category="length",
                    page=b.page_num,
                    block_idx=b.block_idx,
                    message=f"Bản dịch quá dài ({ratio:.0%} so với gốc)",
                    original=original[:100],
                    translated=translated[:100],
                ))
                penalty += 10 * block_weight

        # ── Check 3: Numbers preservation ─────────────────
        if len(original) > 20:
            orig_numbers = set(re.findall(r'\d+\.?\d*', original))
            trans_numbers = set(re.findall(r'\d+\.?\d*', translated))
            missing_numbers = orig_numbers - trans_numbers
            # Only flag significant numbers (not section numbers like "1", "2")
            significant = [n for n in missing_numbers if len(n) > 1 or float(n) > 9]
            if significant:
                report.issues.append(QualityIssue(
                    severity="warning",
                    category="numbers",
                    page=b.page_num,
                    block_idx=b.block_idx,
                    message=f"Số liệu có thể bị thiếu: {', '.join(sorted(significant)[:5])}",
                    original=original[:100],
                    translated=translated[:100],
                ))
                penalty += 5 * block_weight

    # ── Check 4: Glossary compliance ──────────────────────
    if glossary:
        glossary_issues = _check_glossary_compliance(translatable, glossary)
        report.issues.extend(glossary_issues)
        penalty += len(glossary_issues) * 0.5

    # Cap penalty at 100
    report.score = max(0.0, 100.0 - penalty)
    return report


def _check_glossary_compliance(
    blocks: list, glossary: dict[str, str]
) -> list[QualityIssue]:
    """Check if translations follow the glossary consistently.

    For each glossary term that appears in the original, check if
    the expected Vietnamese translation appears in the translated text.
    """
    issues = []

    # Only check terms that appear multiple times (consistency matters)
    term_violations: dict[str, int] = {}  # term -> violation count

    for b in blocks:
        original = (b.text or "").strip().lower()
        translated = (b.translated_text or "").strip()

        if not original or not translated:
            continue

        for en_term, vi_term in glossary.items():
            if en_term.lower() in original:
                # Check if the expected Vietnamese term is in the translation
                if vi_term.lower() not in translated.lower():
                    key = en_term
                    term_violations[key] = term_violations.get(key, 0) + 1

    # Only report terms with 3+ violations (occasional misses are OK)
    for term, count in sorted(term_violations.items(), key=lambda x: -x[1]):
        if count >= 3:
            vi = glossary[term]
            issues.append(QualityIssue(
                severity="info",
                category="glossary",
                page=0,
                block_idx=0,
                message=(
                    f"Thuật ngữ \"{term}\" → \"{vi}\" "
                    f"không nhất quán ({count} lần không khớp)"
                ),
            ))

    return issues


# ── Fixable block detection ───────────────────────────────────

def find_fixable_blocks(
    blocks: list,
    glossary: dict | None = None,
) -> list:
    """Find blocks that have quality issues worth retranslating.

    Returns list of TextBlock objects that should be re-sent to Gemini.
    Only returns blocks where retranslation is likely to help:
    - Long untranslated paragraphs (error severity, 8+ words)
    - Truncated translations (length < 30% of original, 30+ chars)

    Skips: short labels, headings, author names, metadata, figure captions.
    """
    fixable = []

    for b in blocks:
        if not b.is_translatable:
            continue

        original = (b.text or "").strip()
        translated = (b.translated_text or "").strip()

        if not original:
            continue

        word_count = len(original.split())

        # Skip short blocks — headings, labels, names, metadata
        # These are rarely worth retranslating and often contain
        # proper nouns, emails, arXiv IDs that should stay as-is
        if word_count < 8:
            continue

        # Skip blocks that look like metadata / author info
        if _is_metadata_block(original):
            continue

        # Skip blocks that are mostly numbers/data (table rows)
        alpha_chars = sum(1 for c in original if c.isalpha())
        if len(original) > 0 and alpha_chars / len(original) < 0.3:
            continue

        # Case 1: Empty translation for substantial block
        if not translated:
            fixable.append(b)
            continue

        # Case 2: Not actually translated (still English, long enough)
        if _is_likely_untranslated(original, translated):
            fixable.append(b)
            continue

        # Case 3: Truncated translation
        if len(original) > 30:
            ratio = len(translated) / len(original)
            if ratio < 0.3:
                fixable.append(b)
                continue

    return fixable


_METADATA_RE = re.compile(
    r'(arxiv:|@[\w.]+\.(com|edu|org)|university of|'
    r'department of|proceedings of|conference on|'
    r'et al\.|©|copyright)',
    re.IGNORECASE,
)


def _is_metadata_block(text: str) -> bool:
    """Detect metadata blocks: author info, affiliations, arXiv IDs, etc."""
    return bool(_METADATA_RE.search(text))
