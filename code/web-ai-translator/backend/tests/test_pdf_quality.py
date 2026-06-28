"""Tests for app/pdf/quality.py — heuristic quality scoring.

All tests construct minimal TextBlock-like objects directly.
No PDF files, no routes, no browser.

Coverage:
  check_translation_quality()   — score, issue count, issue categories
  _is_likely_untranslated()     — private helper via indirect testing
  _classify_untranslated()      — severity assignment
  _check_glossary_compliance()  — via check_translation_quality(glossary=...)
  find_fixable_blocks()         — which blocks warrant retranslation
"""

import pytest
from unittest.mock import MagicMock

from app.pdf.quality import (
    check_translation_quality,
    find_fixable_blocks,
    QualityReport,
)


# ── Fake TextBlock builder ────────────────────────────────────────────────────

def _block(
    text: str,
    translated: str = "",
    is_translatable: bool = True,
    page_num: int = 0,
    block_idx: int = 0,
) -> MagicMock:
    """Return a minimal TextBlock-like mock."""
    b = MagicMock()
    b.text = text
    b.translated_text = translated
    b.is_translatable = is_translatable
    b.page_num = page_num
    b.block_idx = block_idx
    return b


def _vi(text: str) -> str:
    """Wrap text with a Vietnamese character so it passes the Vietnamese detector."""
    return text + " (đây là bản dịch)"


# ── Baseline: perfect translation ────────────────────────────────────────────

def test_perfect_score_all_translated():
    """All blocks properly translated with Vietnamese text → score = 100."""
    blocks = [
        _block(
            "Machine learning models achieve state-of-the-art results.",
            translated=_vi("Các mô hình học máy đạt kết quả tốt nhất."),
        ),
        _block(
            "Deep neural networks have transformed natural language processing.",
            translated=_vi("Mạng nơ-ron sâu đã thay đổi xử lý ngôn ngữ tự nhiên."),
        ),
    ]
    report = check_translation_quality(blocks)
    assert report.score == 100.0
    assert report.untranslated_blocks == 0
    assert len(report.issues) == 0


def test_non_translatable_blocks_ignored():
    """Non-translatable blocks (math, headers) are excluded from scoring."""
    blocks = [
        _block("x = ∫ f(t) dt", translated="", is_translatable=False),
        _block(
            "The proposed method outperforms baselines significantly.",
            translated=_vi("Phương pháp đề xuất vượt trội so với các baseline."),
        ),
    ]
    report = check_translation_quality(blocks)
    assert report.score == 100.0
    assert report.translatable_blocks == 1


# ── Untranslated blocks ───────────────────────────────────────────────────────

def test_empty_translation_reduces_score():
    """Block with no translated text → score < 100."""
    blocks = [
        _block(
            "This is a long untranslated paragraph with many words that should have been "
            "converted to Vietnamese but was not.",
            translated="",
        )
    ]
    report = check_translation_quality(blocks)
    assert report.score < 100.0
    assert report.untranslated_blocks == 1


def test_english_translation_detected():
    """Block with identical English original/translated → untranslated."""
    text = "The gradient descent algorithm minimizes the loss function iteratively."
    blocks = [_block(text, translated=text)]
    report = check_translation_quality(blocks)
    assert report.untranslated_blocks == 1


def test_high_english_ratio_detected():
    """Translation still mostly English → flagged as untranslated."""
    original = "Neural networks learn feature representations automatically from data."
    translated = "Neural networks learn feature representations automatically from data here."
    blocks = [_block(original, translated=translated)]
    report = check_translation_quality(blocks)
    assert report.untranslated_blocks == 1


def test_short_untranslated_is_info_not_error():
    """Very short untranslated block (<= 4 words) → 'info', not 'error'."""
    blocks = [_block("Fig. 1", translated="")]
    report = check_translation_quality(blocks)
    info_issues = [i for i in report.issues if i.severity == "info"]
    assert len(info_issues) > 0


def test_long_untranslated_is_error():
    """Long untranslated paragraph (>15 words) → 'error' severity."""
    long_text = (
        "This research paper presents a comprehensive evaluation of multiple "
        "deep learning architectures applied to the task of machine translation "
        "with a focus on low-resource language pairs."
    )
    blocks = [_block(long_text, translated="")]
    report = check_translation_quality(blocks)
    error_issues = [i for i in report.issues if i.severity == "error"]
    assert len(error_issues) > 0


def test_all_untranslated_heavy_penalty():
    """All blocks untranslated → score well below 100 (heavy penalty)."""
    long_text = (
        "The experimental results demonstrate that the proposed approach achieves "
        "significant improvements over the existing state-of-the-art methods."
    )
    blocks = [_block(long_text, translated="") for _ in range(5)]
    report = check_translation_quality(blocks)
    # 5 equal-weight error blocks: penalty = 5 × (30 × 0.2) = 30 → score = 70
    # When many blocks are untranslated, score should drop significantly
    assert report.score <= 70.0
    assert report.untranslated_blocks == 5


# ── Length ratio ─────────────────────────────────────────────────────────────

def test_too_short_translation_flagged():
    """Translation < 30% of original length → 'length' warning."""
    original = "A" * 100
    translated = _vi("B" * 10)  # 10% length
    blocks = [_block(original, translated=translated)]
    report = check_translation_quality(blocks)
    length_issues = [i for i in report.issues if i.category == "length"]
    assert len(length_issues) > 0


def test_too_long_translation_flagged():
    """Translation > 300% of original → 'length' warning (only for blocks > 30 chars)."""
    # Original must be > 30 chars for length ratio check to trigger
    original = "The proposed architecture achieves state-of-the-art performance on benchmarks."
    translated = _vi(original * 5)  # ~5× length → well over 300%
    blocks = [_block(original, translated=translated)]
    report = check_translation_quality(blocks)
    length_issues = [i for i in report.issues if i.category == "length"]
    assert len(length_issues) > 0


def test_normal_length_ratio_no_penalty():
    """Vietnamese translations are typically 1.0–1.5× the English length — no penalty."""
    original = "The transformer model processes sequences efficiently in parallel."
    translated = _vi("Mô hình transformer xử lý các chuỗi một cách hiệu quả song song.")
    blocks = [_block(original, translated=translated)]
    report = check_translation_quality(blocks)
    length_issues = [i for i in report.issues if i.category == "length"]
    assert len(length_issues) == 0


# ── Numbers preservation ──────────────────────────────────────────────────────

def test_missing_significant_number_flagged():
    """Number like '42.3' in original but absent in translation → warning."""
    original = "Our model achieves 42.3 BLEU score on the WMT14 benchmark."
    translated = _vi("Mô hình của chúng tôi đạt điểm BLEU trên benchmark WMT14.")
    # '42.3' missing from translation
    blocks = [_block(original, translated=translated)]
    report = check_translation_quality(blocks)
    number_issues = [i for i in report.issues if i.category == "numbers"]
    assert len(number_issues) > 0


def test_numbers_present_no_penalty():
    """All numbers preserved → no numbers issue."""
    original = "Accuracy improved from 87.5% to 92.1% after fine-tuning."
    translated = _vi("Độ chính xác cải thiện từ 87.5% lên 92.1% sau khi tinh chỉnh.")
    blocks = [_block(original, translated=translated)]
    report = check_translation_quality(blocks)
    number_issues = [i for i in report.issues if i.category == "numbers"]
    assert len(number_issues) == 0


def test_single_digit_numbers_not_flagged():
    """Single-digit numbers (1, 2, 3…) are common section numbers — not significant."""
    original = "Section 2 presents the proposed method in detail."
    translated = _vi("Phần 2 trình bày phương pháp đề xuất chi tiết.")
    blocks = [_block(original, translated=translated)]
    report = check_translation_quality(blocks)
    number_issues = [i for i in report.issues if i.category == "numbers"]
    assert len(number_issues) == 0


# ── Glossary compliance ───────────────────────────────────────────────────────

def test_glossary_violation_flagged_at_threshold():
    """Term violated 3+ times → glossary issue flagged."""
    glossary = {"neural network": "mạng nơ-ron"}
    text = "The neural network architecture is based on transformer layers."
    # 5 blocks all containing the term but not the Vietnamese translation
    blocks = [
        _block(text, translated=_vi("Kiến trúc này dựa trên các lớp transformer."))
        for _ in range(5)
    ]
    report = check_translation_quality(blocks, glossary=glossary)
    glossary_issues = [i for i in report.issues if i.category == "glossary"]
    assert len(glossary_issues) > 0


def test_glossary_violation_below_threshold_not_flagged():
    """Term violated only 2 times → not flagged (threshold is 3)."""
    glossary = {"gradient descent": "hạ gradient"}
    text = "We use gradient descent to optimize the loss."
    blocks = [
        _block(text, translated=_vi("Chúng tôi tối ưu hóa hàm mất mát."))
        for _ in range(2)
    ]
    report = check_translation_quality(blocks, glossary=glossary)
    glossary_issues = [i for i in report.issues if i.category == "glossary"]
    assert len(glossary_issues) == 0


def test_glossary_compliant_no_penalty():
    """All blocks follow the glossary → no glossary issues."""
    glossary = {"neural network": "mạng nơ-ron"}
    text = "The neural network converges quickly."
    blocks = [
        _block(text, translated=_vi("Mạng nơ-ron hội tụ nhanh chóng."))
        for _ in range(5)
    ]
    report = check_translation_quality(blocks, glossary=glossary)
    glossary_issues = [i for i in report.issues if i.category == "glossary"]
    assert len(glossary_issues) == 0


# ── report.to_dict() ──────────────────────────────────────────────────────────

def test_to_dict_keys():
    blocks = [
        _block(
            "Short paragraph with some words to check.",
            translated=_vi("Đoạn văn ngắn để kiểm tra."),
        )
    ]
    report = check_translation_quality(blocks)
    d = report.to_dict()
    for key in ("score", "total_blocks", "translatable_blocks",
                "translated_blocks", "untranslated_blocks",
                "issue_count", "issues_by_severity", "issues"):
        assert key in d, f"Missing key: {key}"


def test_score_capped_at_zero():
    """Score cannot go below 0."""
    # Many large untranslated blocks
    long = (
        "This comprehensive study investigates the impact of attention mechanisms "
        "on the quality of machine translation across diverse language pairs."
    )
    blocks = [_block(long, translated="") for _ in range(10)]
    report = check_translation_quality(blocks)
    assert report.score >= 0.0


# ── find_fixable_blocks ───────────────────────────────────────────────────────

def test_find_fixable_returns_long_untranslated():
    """Long untranslated blocks (8+ words) should be fixable."""
    long_text = (
        "The proposed architecture achieves superior performance on all standard "
        "benchmarks while using significantly fewer parameters than baseline models."
    )
    blocks = [_block(long_text, translated="")]
    fixable = find_fixable_blocks(blocks)
    assert len(fixable) == 1


def test_find_fixable_skips_short_blocks():
    """Short blocks (<8 words) are not worth retranslating."""
    blocks = [_block("Fig. 1", translated="")]
    fixable = find_fixable_blocks(blocks)
    assert len(fixable) == 0


def test_find_fixable_skips_well_translated():
    """Properly translated blocks should not be in fixable list."""
    blocks = [
        _block(
            "Our experimental results confirm the effectiveness of the approach.",
            translated=_vi("Kết quả thực nghiệm xác nhận hiệu quả của phương pháp."),
        )
    ]
    fixable = find_fixable_blocks(blocks)
    assert len(fixable) == 0


def test_find_fixable_includes_truncated():
    """Truncated translation (< 30% length) should be fixable."""
    original = "X" * 100
    truncated = _vi("Y" * 10)  # 10% of original
    blocks = [_block("word " * 10 + original, translated=truncated)]
    fixable = find_fixable_blocks(blocks)
    assert len(fixable) > 0
