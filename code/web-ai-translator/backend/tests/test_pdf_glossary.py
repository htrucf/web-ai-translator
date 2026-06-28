"""Tests for app/pdf/glossary.py — terminology management.

Pure logic tests: no PDF files, no routes, no browser.

Coverage:
  parse_extraction_response()    — parse Gemini's ```text``` block
  filter_glossary_for_chunk()    — only keep terms present in chunk text
  format_glossary_for_prompt()   — output format for injection into prompt
  merge_glossary()               — existing terms not overwritten
  build_extraction_prompt()      — prompt contains the sample text
  extract_new_terms_prompt()     — prompt contains original/translated snippets
"""

import pytest

from app.pdf.glossary import (
    parse_extraction_response,
    filter_glossary_for_chunk,
    format_glossary_for_prompt,
    merge_glossary,
    build_extraction_prompt,
    extract_new_terms_prompt,
    MAX_TERMS_PER_PROMPT,
)


# ── parse_extraction_response ─────────────────────────────────────────────────

def test_parse_basic_arrow_format():
    response = "```text\nmachine learning → học máy\ngradient descent → hạ gradient\n```"
    result = parse_extraction_response(response)
    assert result["machine learning"] == "học máy"
    assert result["gradient descent"] == "hạ gradient"


def test_parse_arrow_with_spaces():
    response = "```text\ntransformer  →   bộ biến đổi\n```"
    result = parse_extraction_response(response)
    assert result["transformer"] == "bộ biến đổi"


def test_parse_ascii_arrow():
    """Also accepts -> (ASCII arrow)."""
    response = "```text\noverfitting -> quá khớp\n```"
    result = parse_extraction_response(response)
    assert "overfitting" in result


def test_parse_stores_lowercase_keys():
    """Keys are stored lowercase for case-insensitive lookup."""
    response = "```text\nNeural Network → Mạng nơ-ron\n```"
    result = parse_extraction_response(response)
    assert "neural network" in result


def test_parse_skips_blank_lines():
    response = "```text\n\nmachine learning → học máy\n\n```"
    result = parse_extraction_response(response)
    assert len(result) == 1


def test_parse_skips_invalid_lines():
    """Lines without arrow separator are ignored."""
    response = "```text\nmachine learning → học máy\nthis line has no arrow\n```"
    result = parse_extraction_response(response)
    assert len(result) == 1


def test_parse_empty_response():
    assert parse_extraction_response("") == {}
    assert parse_extraction_response(None) == {}


def test_parse_response_without_code_block():
    """Response without ```text``` fences — falls back to parsing raw lines."""
    response = "machine learning → học máy\ngradient → gradient"
    result = parse_extraction_response(response)
    assert "machine learning" in result


def test_parse_strips_quotes():
    """Quoted terms like '"attention mechanism" → "cơ chế chú ý"' are parsed."""
    response = '```text\n"attention mechanism" → "cơ chế chú ý"\n```'
    result = parse_extraction_response(response)
    assert "attention mechanism" in result
    assert result["attention mechanism"] == "cơ chế chú ý"


def test_parse_up_to_60_terms():
    """Up to 60 terms can be extracted; extras are just included in output."""
    lines = "\n".join(f"term{i} → thuật ngữ {i}" for i in range(70))
    response = f"```text\n{lines}\n```"
    result = parse_extraction_response(response)
    assert len(result) == 70  # parse_extraction_response does not cap at 60


# ── filter_glossary_for_chunk ─────────────────────────────────────────────────

def test_filter_includes_matching_term():
    glossary = {"neural network": "mạng nơ-ron", "attention": "chú ý"}
    chunk = "We propose a neural network with attention mechanisms."
    result = filter_glossary_for_chunk(glossary, chunk)
    assert "neural network" in result
    assert "attention" in result


def test_filter_excludes_absent_term():
    glossary = {"transformer": "bộ biến đổi", "attention": "chú ý"}
    chunk = "This paper focuses on recurrent networks only."
    result = filter_glossary_for_chunk(glossary, chunk)
    assert "transformer" not in result
    assert "attention" not in result


def test_filter_case_insensitive():
    """Matching is case-insensitive (glossary keys are lowercase)."""
    glossary = {"neural network": "mạng nơ-ron"}
    chunk = "The Neural Network was trained on GPU."
    result = filter_glossary_for_chunk(glossary, chunk)
    assert "neural network" in result


def test_filter_max_terms_enforced():
    """When glossary has >MAX_TERMS_PER_PROMPT matching terms, only max are returned."""
    # Build a chunk that contains all 60 terms
    glossary = {f"term{i}": f"thuật ngữ {i}" for i in range(MAX_TERMS_PER_PROMPT + 20)}
    chunk = " ".join(f"term{i}" for i in range(MAX_TERMS_PER_PROMPT + 20))
    result = filter_glossary_for_chunk(glossary, chunk)
    assert len(result) == MAX_TERMS_PER_PROMPT


def test_filter_prefers_longer_terms():
    """When capping, longer (more specific) terms are preferred."""
    glossary = {
        "network": "mạng",
        "neural network": "mạng nơ-ron",
        "deep neural network": "mạng nơ-ron sâu",
    }
    # Make the glossary exceed the cap by using many short terms
    for i in range(MAX_TERMS_PER_PROMPT):
        glossary[f"x{i}"] = f"y{i}"
    chunk = " ".join(glossary.keys())
    result = filter_glossary_for_chunk(glossary, chunk)
    # The three specific terms should survive over many short ones
    assert len(result) == MAX_TERMS_PER_PROMPT
    # Longer terms should be in the result
    assert "deep neural network" in result or "neural network" in result


def test_filter_empty_glossary():
    assert filter_glossary_for_chunk({}, "any text") == {}


def test_filter_empty_chunk():
    glossary = {"machine learning": "học máy"}
    assert filter_glossary_for_chunk(glossary, "") == {}


# ── format_glossary_for_prompt ────────────────────────────────────────────────

def test_format_contains_arrow_separator():
    glossary = {"machine learning": "học máy", "attention": "chú ý"}
    result = format_glossary_for_prompt(glossary)
    assert "→" in result


def test_format_contains_all_terms():
    glossary = {"transformer": "bộ biến đổi", "overfitting": "quá khớp"}
    result = format_glossary_for_prompt(glossary)
    assert "transformer" in result
    assert "overfitting" in result
    assert "bộ biến đổi" in result
    assert "quá khớp" in result


def test_format_empty_glossary_returns_empty():
    assert format_glossary_for_prompt({}) == ""


def test_format_has_header():
    """The formatted glossary should have an instructional header."""
    glossary = {"attention": "chú ý"}
    result = format_glossary_for_prompt(glossary)
    assert "THUẬT NGỮ" in result or "glossary" in result.lower() or "BẮT BUỘC" in result


def test_format_each_term_on_own_line():
    glossary = {"term1": "thuật ngữ 1", "term2": "thuật ngữ 2"}
    result = format_glossary_for_prompt(glossary)
    lines = [l for l in result.split("\n") if "→" in l]
    assert len(lines) == 2


# ── merge_glossary ────────────────────────────────────────────────────────────

def test_merge_adds_new_terms():
    existing = {"machine learning": "học máy"}
    new = {"attention": "chú ý", "transformer": "bộ biến đổi"}
    merged = merge_glossary(existing, new)
    assert "attention" in merged
    assert "transformer" in merged
    assert "machine learning" in merged


def test_merge_does_not_overwrite_existing():
    """First translation wins — existing terms are not overwritten."""
    existing = {"neural network": "mạng nơ-ron"}
    new = {"neural network": "mạng thần kinh nhân tạo"}  # different translation
    merged = merge_glossary(existing, new)
    assert merged["neural network"] == "mạng nơ-ron"  # original preserved


def test_merge_returns_new_dict():
    """merge_glossary does not mutate the existing dict."""
    existing = {"a": "1"}
    new = {"b": "2"}
    merged = merge_glossary(existing, new)
    assert "b" not in existing
    assert merged is not existing


def test_merge_empty_new():
    existing = {"a": "1"}
    merged = merge_glossary(existing, {})
    assert merged == existing


def test_merge_empty_existing():
    new = {"a": "1"}
    merged = merge_glossary({}, new)
    assert merged == new


def test_merge_key_normalised_lowercase():
    """Keys from new_terms are lowercased before merging."""
    existing = {}
    new = {"Neural Network": "mạng nơ-ron"}
    merged = merge_glossary(existing, new)
    assert "neural network" in merged


# ── build_extraction_prompt ───────────────────────────────────────────────────

def test_extraction_prompt_contains_sample_text():
    sample = "The transformer model uses multi-head attention."
    prompt = build_extraction_prompt(sample)
    assert sample in prompt


def test_extraction_prompt_has_format_instruction():
    prompt = build_extraction_prompt("sample")
    assert "→" in prompt or "->" in prompt


def test_extraction_prompt_has_code_fence():
    """Prompt instructs Gemini to return output in a code block."""
    prompt = build_extraction_prompt("sample")
    assert "```" in prompt


# ── extract_new_terms_prompt ──────────────────────────────────────────────────

def test_new_terms_prompt_contains_original():
    prompt = extract_new_terms_prompt("original text here", "translated text here")
    assert "original text here" in prompt


def test_new_terms_prompt_contains_translated():
    prompt = extract_new_terms_prompt("original", "translated")
    assert "translated" in prompt


def test_new_terms_prompt_has_format_instruction():
    prompt = extract_new_terms_prompt("a", "b")
    assert "→" in prompt or "->" in prompt
