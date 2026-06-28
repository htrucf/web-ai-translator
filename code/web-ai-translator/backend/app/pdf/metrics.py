"""Reference-free translation quality metrics.

Since we don't have human reference translations, these metrics evaluate
translation quality using source-only and target-only heuristics:

1. Translation Coverage — % of blocks actually translated
2. Vietnamese Ratio — % of translated text containing Vietnamese characters
3. Math Preservation — % of numbers/math symbols preserved after translation
4. Length Consistency — statistical analysis of orig/translated length ratios
5. Terminology Consistency — same English term → same Vietnamese translation
6. Fluency Score — heuristic check for natural Vietnamese sentence structure

These complement the existing quality.py (which focuses on error detection)
by providing continuous, comparable metrics suitable for evaluation chapters.
"""

import re
import math
from dataclasses import dataclass, field


# ── Vietnamese detection ─────────────────────────────────────────

_VIETNAMESE_CHARS = re.compile(
    r'[àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợ'
    r'ùúủũụưứừửữựỳýỷỹỵđÀÁẢÃẠĂẮẰẲẴẶÂẤẦẨẪẬÈÉẺẼẸÊẾỀỂỄỆ'
    r'ÌÍỈĨỊÒÓỎÕỌÔỐỒỔỖỘƠỚỜỞỠỢÙÚỦŨỤƯỨỪỬỮỰỲÝỶỸỴĐ]'
)

_ENGLISH_WORD = re.compile(r'\b[a-zA-Z]{4,}\b')

# Common Vietnamese function words — presence indicates actual Vietnamese text
_VN_FUNCTION_WORDS = re.compile(
    r'\b(của|và|là|trong|với|được|cho|các|này|đó|những|một|có|không'
    r'|từ|đến|trên|theo|bằng|về|như|để|khi|nếu|hoặc|nhưng|vì'
    r'|tại|cũng|đã|sẽ|đang|rằng|mà|hay|còn|hơn|nhất|rất)\b',
    re.IGNORECASE,
)

# Number/math patterns
_NUMBER_RE = re.compile(r'-?\d+\.?\d*(?:[eE][+-]?\d+)?')
_MATH_SYMBOL_RE = re.compile(
    r'[+\-*/=<>≤≥≠≈∞∑∏∫∂∇±×÷∈∉⊂⊃∪∩∀∃∧∨¬→←↔⇒⇐⇔'
    r'αβγδεζηθικλμνξπρστυφχψω'
    r'ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΠΡΣΤΥΦΧΨΩ]'
)


# ── Metrics dataclass ────────────────────────────────────────────

@dataclass
class MetricsReport:
    """Full metrics report for a translated document."""

    # Coverage
    total_translatable: int = 0
    actually_translated: int = 0
    coverage_score: float = 0.0           # 0-100

    # Vietnamese content
    vietnamese_ratio: float = 0.0          # 0-1, ratio of chars that are VN
    vn_function_word_ratio: float = 0.0    # 0-1, ratio of VN function words
    vietnamese_score: float = 0.0          # 0-100

    # Math preservation
    numbers_preserved_ratio: float = 0.0   # 0-1
    math_symbols_preserved_ratio: float = 0.0
    math_score: float = 0.0               # 0-100

    # Length consistency
    mean_length_ratio: float = 0.0
    length_ratio_std: float = 0.0
    outlier_count: int = 0                 # blocks with ratio < 0.3 or > 3.0
    length_score: float = 0.0             # 0-100

    # Terminology consistency
    unique_terms_checked: int = 0
    consistent_terms: int = 0
    consistency_ratio: float = 0.0         # 0-1
    terminology_score: float = 0.0        # 0-100

    # Fluency
    avg_sentence_length: float = 0.0
    fluency_score: float = 0.0            # 0-100

    # Aggregate
    overall_score: float = 0.0            # 0-100 weighted average

    # Details for debugging
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "coverage": {
                "total_translatable": self.total_translatable,
                "actually_translated": self.actually_translated,
                "score": round(self.coverage_score, 1),
            },
            "vietnamese": {
                "char_ratio": round(self.vietnamese_ratio, 3),
                "function_word_ratio": round(self.vn_function_word_ratio, 3),
                "score": round(self.vietnamese_score, 1),
            },
            "math_preservation": {
                "numbers_preserved": round(self.numbers_preserved_ratio, 3),
                "symbols_preserved": round(self.math_symbols_preserved_ratio, 3),
                "score": round(self.math_score, 1),
            },
            "length_consistency": {
                "mean_ratio": round(self.mean_length_ratio, 3),
                "std_ratio": round(self.length_ratio_std, 3),
                "outliers": self.outlier_count,
                "score": round(self.length_score, 1),
            },
            "terminology": {
                "terms_checked": self.unique_terms_checked,
                "consistent": self.consistent_terms,
                "consistency_ratio": round(self.consistency_ratio, 3),
                "score": round(self.terminology_score, 1),
            },
            "fluency": {
                "avg_sentence_length": round(self.avg_sentence_length, 1),
                "score": round(self.fluency_score, 1),
            },
            "overall_score": round(self.overall_score, 1),
            "details": self.details,
        }


# ── Individual metric functions ──────────────────────────────────

def _calc_coverage(blocks: list) -> tuple[int, int, float]:
    """Calculate translation coverage."""
    translatable = [b for b in blocks if b.is_translatable and (b.text or "").strip()]
    total = len(translatable)
    if total == 0:
        return 0, 0, 100.0

    translated = 0
    for b in translatable:
        t = (b.translated_text or "").strip()
        if t and _VIETNAMESE_CHARS.search(t):
            translated += 1

    score = (translated / total) * 100
    return total, translated, score


def _calc_vietnamese_ratio(blocks: list) -> tuple[float, float, float]:
    """Calculate how much of the translated text is actually Vietnamese.

    Returns (char_ratio, function_word_ratio, score).
    """
    all_translated = []
    for b in blocks:
        if b.is_translatable and b.translated_text:
            all_translated.append(b.translated_text.strip())

    if not all_translated:
        return 0.0, 0.0, 0.0

    full_text = " ".join(all_translated)
    total_alpha = sum(1 for c in full_text if c.isalpha())
    if total_alpha == 0:
        return 0.0, 0.0, 0.0

    vn_chars = len(_VIETNAMESE_CHARS.findall(full_text))
    char_ratio = vn_chars / total_alpha

    # Function word ratio — strong indicator of actual Vietnamese
    words = full_text.split()
    total_words = len(words)
    if total_words == 0:
        return char_ratio, 0.0, char_ratio * 100

    vn_fw = len(_VN_FUNCTION_WORDS.findall(full_text))
    fw_ratio = min(1.0, vn_fw / total_words)

    # Score: weighted combination
    # char_ratio alone can be misleading (diacritics on names),
    # function words are a stronger signal
    score = (char_ratio * 40 + fw_ratio * 60) * 100 / 100
    # Clamp
    score = min(100.0, max(0.0, score * 100 / max(char_ratio * 40 + fw_ratio * 60, 0.01) if char_ratio + fw_ratio > 0 else 0))

    # Simpler scoring: if both ratios are healthy, score is high
    score = min(100.0, (char_ratio * 0.4 + min(fw_ratio * 5, 0.6)) * 100)

    return char_ratio, fw_ratio, score


def _calc_math_preservation(blocks: list) -> tuple[float, float, float]:
    """Check if numbers and math symbols from original are preserved.

    Returns (numbers_ratio, symbols_ratio, score).
    """
    total_numbers = 0
    preserved_numbers = 0
    total_symbols = 0
    preserved_symbols = 0

    for b in blocks:
        if not b.is_translatable:
            continue
        orig = (b.text or "").strip()
        trans = (b.translated_text or "").strip()
        if not orig or not trans:
            continue

        # Numbers
        orig_nums = _NUMBER_RE.findall(orig)
        trans_nums = set(_NUMBER_RE.findall(trans))
        total_numbers += len(orig_nums)
        for n in orig_nums:
            if n in trans_nums:
                preserved_numbers += 1

        # Math symbols
        orig_syms = _MATH_SYMBOL_RE.findall(orig)
        trans_syms = set(_MATH_SYMBOL_RE.findall(trans))
        total_symbols += len(orig_syms)
        for s in orig_syms:
            if s in trans_syms:
                preserved_symbols += 1

    num_ratio = preserved_numbers / total_numbers if total_numbers > 0 else 1.0
    sym_ratio = preserved_symbols / total_symbols if total_symbols > 0 else 1.0

    # Weight: numbers matter more (data integrity)
    score = (num_ratio * 0.7 + sym_ratio * 0.3) * 100
    return num_ratio, sym_ratio, score


def _calc_length_consistency(blocks: list) -> tuple[float, float, int, float]:
    """Analyze length ratios between original and translated text.

    Good translations typically have ratio 0.8-2.0 for EN→VI.
    Vietnamese is often slightly longer than English.

    Returns (mean_ratio, std_ratio, outlier_count, score).
    """
    ratios = []

    for b in blocks:
        if not b.is_translatable:
            continue
        orig = (b.text or "").strip()
        trans = (b.translated_text or "").strip()
        if len(orig) < 20 or not trans:
            continue
        ratio = len(trans) / len(orig)
        ratios.append(ratio)

    if not ratios:
        return 0.0, 0.0, 0, 100.0

    mean_r = sum(ratios) / len(ratios)
    variance = sum((r - mean_r) ** 2 for r in ratios) / len(ratios)
    std_r = math.sqrt(variance)

    # Count outliers (ratio < 0.3 or > 3.0)
    outliers = sum(1 for r in ratios if r < 0.3 or r > 3.0)

    # Score: penalize high variance and outliers
    # Ideal: mean ~1.0-1.3, std < 0.3, no outliers
    score = 100.0

    # Penalize mean far from ideal range (0.8-1.5)
    if mean_r < 0.8:
        score -= (0.8 - mean_r) * 50
    elif mean_r > 1.5:
        score -= (mean_r - 1.5) * 30

    # Penalize high variance
    score -= min(30, std_r * 40)

    # Penalize outliers
    outlier_ratio = outliers / len(ratios) if ratios else 0
    score -= outlier_ratio * 40

    return mean_r, std_r, outliers, max(0.0, score)


def _calc_terminology_consistency(blocks: list) -> tuple[int, int, float, float]:
    """Check if the same English terms are translated consistently.

    Extracts multi-word terms from original text, finds their translations,
    and checks if the same source term always maps to the same target.

    Returns (terms_checked, consistent_count, consistency_ratio, score).
    """
    # Build term → set of translations mapping
    # Look for capitalized multi-word terms (likely technical terms)
    term_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')

    term_translations: dict[str, list[str]] = {}

    for b in blocks:
        if not b.is_translatable:
            continue
        orig = (b.text or "").strip()
        trans = (b.translated_text or "").strip()
        if not orig or not trans:
            continue

        terms = term_pattern.findall(orig)
        for term in terms:
            term_lower = term.lower()
            if term_lower not in term_translations:
                term_translations[term_lower] = []

            # Try to find corresponding translation by position heuristic
            # Since we can't do word alignment, we just record the full
            # translation for this block — consistency is checked across blocks
            term_translations[term_lower].append(trans)

    # For terms appearing 2+ times, check if translations are similar
    checked = 0
    consistent = 0

    for term, trans_list in term_translations.items():
        if len(trans_list) < 2:
            continue
        checked += 1

        # Extract the likely translation of this term from each block
        # Heuristic: find common Vietnamese subsequences across translations
        # Simplified: check if translations share significant substrings
        all_words = [set(t.lower().split()) for t in trans_list]
        if len(all_words) >= 2:
            # Check pairwise overlap
            common = all_words[0]
            for ws in all_words[1:]:
                common = common & ws
            # If there's reasonable overlap, term is consistent
            avg_len = sum(len(ws) for ws in all_words) / len(all_words)
            if avg_len > 0 and len(common) / avg_len > 0.3:
                consistent += 1

    ratio = consistent / checked if checked > 0 else 1.0
    score = ratio * 100
    return checked, consistent, ratio, score


def _calc_fluency(blocks: list) -> tuple[float, float]:
    """Heuristic fluency check for Vietnamese output.

    Checks:
    - Average sentence length (Vietnamese prefers shorter sentences)
    - Presence of Vietnamese function words (indicates natural structure)
    - No English-structure artifacts (subject directly before verb without Vietnamese markers)

    Returns (avg_sentence_length, score).
    """
    sentences = []
    for b in blocks:
        if not b.is_translatable or not b.translated_text:
            continue
        text = b.translated_text.strip()
        # Split on sentence boundaries
        sents = re.split(r'[.!?]\s+', text)
        sentences.extend(s for s in sents if len(s.split()) >= 3)

    if not sentences:
        return 0.0, 100.0

    # Average sentence length (in words)
    lengths = [len(s.split()) for s in sentences]
    avg_len = sum(lengths) / len(lengths)

    score = 100.0

    # Vietnamese academic text: 15-30 words per sentence is normal
    # Too short (<8) might indicate fragmentation
    # Too long (>50) might indicate untranslated English or run-on
    if avg_len < 8:
        score -= (8 - avg_len) * 3
    elif avg_len > 40:
        score -= (avg_len - 40) * 2

    # Check for Vietnamese function word presence across all sentences
    all_text = " ".join(sentences)
    total_words = len(all_text.split())
    vn_fw_count = len(_VN_FUNCTION_WORDS.findall(all_text))

    # Vietnamese text should have ~15-25% function words
    if total_words > 0:
        fw_ratio = vn_fw_count / total_words
        if fw_ratio < 0.05:
            # Very few Vietnamese function words — likely not properly translated
            score -= 30
        elif fw_ratio < 0.10:
            score -= 15

    # Check for English-heavy sentences (translation failures)
    en_heavy = 0
    for s in sentences:
        words = s.split()
        en_words = _ENGLISH_WORD.findall(s)
        if len(words) > 5 and len(en_words) / len(words) > 0.7:
            en_heavy += 1

    if sentences:
        en_heavy_ratio = en_heavy / len(sentences)
        score -= en_heavy_ratio * 40

    return avg_len, max(0.0, score)


# ── Main compute function ────────────────────────────────────────

# Weights for overall score (must sum to 1.0)
_WEIGHTS = {
    "coverage": 0.25,
    "vietnamese": 0.25,
    "math": 0.15,
    "length": 0.10,
    "terminology": 0.10,
    "fluency": 0.15,
}


def compute_metrics(blocks: list, glossary: dict | None = None) -> MetricsReport:
    """Compute all translation quality metrics.

    Args:
        blocks: List of TextBlock objects (after translation).
        glossary: Optional glossary dict for terminology checking.

    Returns:
        MetricsReport with all sub-metrics and overall score.
    """
    report = MetricsReport()

    # 1. Coverage
    total, translated, cov_score = _calc_coverage(blocks)
    report.total_translatable = total
    report.actually_translated = translated
    report.coverage_score = cov_score

    # 2. Vietnamese content
    vn_ratio, fw_ratio, vn_score = _calc_vietnamese_ratio(blocks)
    report.vietnamese_ratio = vn_ratio
    report.vn_function_word_ratio = fw_ratio
    report.vietnamese_score = vn_score

    # 3. Math preservation
    num_r, sym_r, math_score = _calc_math_preservation(blocks)
    report.numbers_preserved_ratio = num_r
    report.math_symbols_preserved_ratio = sym_r
    report.math_score = math_score

    # 4. Length consistency
    mean_r, std_r, outliers, len_score = _calc_length_consistency(blocks)
    report.mean_length_ratio = mean_r
    report.length_ratio_std = std_r
    report.outlier_count = outliers
    report.length_score = len_score

    # 5. Terminology consistency
    checked, consistent, con_ratio, term_score = _calc_terminology_consistency(blocks)
    report.unique_terms_checked = checked
    report.consistent_terms = consistent
    report.consistency_ratio = con_ratio
    report.terminology_score = term_score

    # 6. Fluency
    avg_sent, flu_score = _calc_fluency(blocks)
    report.avg_sentence_length = avg_sent
    report.fluency_score = flu_score

    # Overall weighted score
    report.overall_score = (
        report.coverage_score * _WEIGHTS["coverage"]
        + report.vietnamese_score * _WEIGHTS["vietnamese"]
        + report.math_score * _WEIGHTS["math"]
        + report.length_score * _WEIGHTS["length"]
        + report.terminology_score * _WEIGHTS["terminology"]
        + report.fluency_score * _WEIGHTS["fluency"]
    )

    return report
