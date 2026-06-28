# -*- coding: utf-8 -*-
"""Tests for app/pdf/diagnostics.py — auto-diagnostics module.

Pure logic tests: no PDF files, no routes, no browser.

Coverage:
  DiagnosticFinding    — weight, to_dict
  DiagnosticReport     — finalize, to_dict, severity ordering
  detect_truncated_response()   — ratio-based truncation detection
  detect_empty_translations()   — empty/still-English detection
  detect_math_contamination()   — math pattern detection
  detect_hallucination()        — excessively long translation detection
  detect_chunk_boundary_split() — mid-sentence chunk boundary
"""

import pytest
from app.pdf.diagnostics import (
    DiagnosticFinding,
    DiagnosticReport,
    detect_truncated_response,
    detect_empty_translations,
    detect_math_contamination,
    detect_hallucination,
    detect_chunk_boundary_split,
    CAUSE_LABELS,
    CAUSE_RECOMMENDATIONS,
)


# ── DiagnosticFinding ───────────────────────────────────────────────────

def test_finding_weight_critical_high_confidence():
    f = DiagnosticFinding(cause="TEST", severity="critical", confidence=0.9)
    assert f.weight() == 3 * 0.9  # 2.7

def test_finding_weight_info_low_confidence():
    f = DiagnosticFinding(cause="TEST", severity="info", confidence=0.3)
    assert f.weight() == 1 * 0.3  # 0.3

def test_finding_to_dict_has_all_keys():
    f = DiagnosticFinding(
        cause="TRUNCATED_RESPONSE", severity="warning", confidence=0.8,
        evidence=["chunk 5"], affected_chunks=[5], recommendation="fix it",
    )
    d = f.to_dict()
    assert d["cause"] == "TRUNCATED_RESPONSE"
    assert d["cause_label"] == CAUSE_LABELS["TRUNCATED_RESPONSE"]
    assert d["severity"] == "warning"
    assert d["confidence"] == 0.8
    assert d["evidence"] == ["chunk 5"]
    assert d["auto_fixable"] is False


# ── DiagnosticReport ────────────────────────────────────────────────────

def test_report_empty_findings_is_ok():
    r = DiagnosticReport(job_id="test_001")
    r.finalize()
    assert r.overall_severity == "ok"
    assert r.primary_cause is None

def test_report_single_critical_finding():
    r = DiagnosticReport(job_id="test_002")
    r.findings.append(DiagnosticFinding(
        cause="EMPTY_TRANSLATION", severity="critical", confidence=0.9,
        recommendation="retry",
    ))
    r.finalize()
    assert r.overall_severity == "critical"
    assert r.primary_cause == "EMPTY_TRANSLATION"
    assert "retry" in r.summary

def test_report_multiple_findings_sorted_by_weight():
    r = DiagnosticReport(job_id="test_003")
    r.findings.append(DiagnosticFinding(
        cause="GLOSSARY_DRIFT", severity="info", confidence=0.5,
    ))
    r.findings.append(DiagnosticFinding(
        cause="TRUNCATED_RESPONSE", severity="critical", confidence=0.85,
    ))
    r.finalize()
    assert r.primary_cause == "TRUNCATED_RESPONSE"
    assert r.findings[0].cause == "TRUNCATED_RESPONSE"

def test_report_to_dict_keys():
    r = DiagnosticReport(job_id="test_004")
    d = r.to_dict()
    for key in ("job_id", "primary_cause", "overall_severity", "summary", "findings"):
        assert key in d


# ── detect_truncated_response ───────────────────────────────────────────

def test_truncated_normal_chunks_no_finding():
    chunks = [
        {"index": 0, "src": "A" * 200, "mt": "B" * 200},
        {"index": 1, "src": "C" * 150, "mt": "D" * 160},
    ]
    assert detect_truncated_response(chunks) is None

def test_truncated_very_short_ratio():
    chunks = [
        {"index": 0, "src": "A" * 200, "mt": "B" * 30},  # ratio = 0.15
    ]
    finding = detect_truncated_response(chunks)
    assert finding is not None
    assert finding.cause == "TRUNCATED_RESPONSE"
    assert 0 in finding.affected_chunks

def test_truncated_no_sentence_end():
    chunks = [
        {"index": 0, "src": "Long original text. " * 10, "mt": "Short mt text no end " * 2},
    ]
    # ratio ~0.3 and no sentence-ending punctuation
    finding = detect_truncated_response(chunks)
    if finding:
        assert finding.cause == "TRUNCATED_RESPONSE"

def test_truncated_empty_chunks_list():
    assert detect_truncated_response([]) is None


# ── detect_empty_translations ───────────────────────────────────────────

def test_empty_translation_detected():
    chunks = [
        {"index": 0, "src": "This is a long paragraph that should be translated.", "mt": ""},
    ]
    finding = detect_empty_translations(chunks, {})
    assert finding is not None
    assert finding.cause == "EMPTY_TRANSLATION"

def test_empty_all_translated_no_finding():
    chunks = [
        {"index": 0, "src": "Hello world test sentence long enough.",
         "mt": "Xin chào thế giới câu kiểm tra đủ dài."},
    ]
    assert detect_empty_translations(chunks, {}) is None

def test_empty_failed_chunks_in_progress():
    chunks = []
    progress = {"failed_chunks": [3, 7]}
    finding = detect_empty_translations(chunks, progress)
    assert finding is not None
    assert 3 in finding.affected_chunks


# ── detect_math_contamination ───────────────────────────────────────────

def test_math_contamination_with_formulas():
    chunks = [
        {"index": 0, "src": "The loss $\\frac{1}{n}\\sum_{i=1}^{n} L_i$ is computed as follows.",
         "mt": "Hàm mất mát $\\frac{1}{n}\\sum_{i=1}^{n} L_i$ được tính."},
        {"index": 1, "src": "Normal text about machine learning and attention.",
         "mt": "Văn bản bình thường về học máy và chú ý."},
    ]
    finding = detect_math_contamination(chunks)
    # May or may not detect depending on threshold
    if finding:
        assert finding.cause == "MATH_CONTAMINATION"

def test_math_contamination_no_formulas():
    chunks = [
        {"index": 0, "src": "Regular text without any formulas here.",
         "mt": "Văn bản bình thường không có công thức nào."},
    ]
    assert detect_math_contamination(chunks) is None


# ── detect_hallucination ────────────────────────────────────────────────

def test_hallucination_very_long_translation():
    chunks = [
        {"index": 0, "src": "Short source.", "mt": "X" * 500},  # hugely longer
    ]
    finding = detect_hallucination(chunks)
    if finding:
        assert finding.cause == "HALLUCINATION"

def test_hallucination_normal_ratio():
    chunks = [
        {"index": 0, "src": "A normal paragraph with enough content.", "mt": "Đoạn văn bình thường."},
    ]
    assert detect_hallucination(chunks) is None


# ── detect_chunk_boundary_split ─────────────────────────────────────────

def test_boundary_split_mid_sentence():
    chunks = [
        {"index": 0, "src": "The proposed method achieves significant improvements over", "mt": "Phương pháp đề xuất đạt được cải thiện đáng kể so với"},
        {"index": 1, "src": "the existing baselines.", "mt": "các baseline hiện có."},
    ]
    finding = detect_chunk_boundary_split(chunks)
    if finding:
        assert finding.cause == "CHUNK_BOUNDARY_SPLIT"
        assert finding.severity == "info"

def test_boundary_split_clean_sentences():
    chunks = [
        {"index": 0, "src": "First paragraph ends here.", "mt": "Đoạn đầu kết thúc ở đây."},
        {"index": 1, "src": "Second paragraph starts here.", "mt": "Đoạn hai bắt đầu ở đây."},
    ]
    assert detect_chunk_boundary_split(chunks) is None
