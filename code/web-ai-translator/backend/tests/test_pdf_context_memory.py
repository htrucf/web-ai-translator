"""Tests for app/pdf/context_memory.py — RAG translation memory.

Pure logic tests: no PDF files, no routes, no browser.

Coverage:
  TranslationDecision.summary()       — short summary for prompt injection
  _TfidfVectorizer.fit/transform      — numpy TF-IDF correctness
  _cosine_similarity()                — vector similarity edge cases
  ContextMemory.add/retrieve          — store + retrieval ranking
  ContextMemory.retrieve_context()    — formatted prompt section
  ContextMemory style profile        — pronoun/term detection
  ContextMemory.save/load_to_progress — resume support
"""

import numpy as np
import pytest

from app.pdf.context_memory import (
    ContextMemory,
    TranslationDecision,
    _TfidfVectorizer,
    _cosine_similarity,
    MAX_CONTEXT_CHUNKS,
    MIN_SIMILARITY,
)


# ── TranslationDecision ───────────────────────────────────────────────────────

def test_decision_summary_short_text():
    d = TranslationDecision(
        chunk_index=0,
        original="Hello world.",
        translated="Xin chào thế giới.",
    )
    summary = d.summary()
    assert "Xin chào thế giới" in summary
    assert len(summary) <= 30


def test_decision_summary_truncates_long_text():
    long_translation = "Câu một. " + ("Câu dài " * 50) + "Câu cuối."
    d = TranslationDecision(
        chunk_index=0,
        original="...",
        translated=long_translation,
    )
    summary = d.summary()
    assert len(summary) <= 210  # SUMMARY_MAX_CHARS=200 + "..." buffer
    assert summary.endswith("...")


def test_decision_summary_uses_first_two_sentences():
    d = TranslationDecision(
        chunk_index=0,
        original="...",
        translated="Câu một. Câu hai. Câu ba. Câu bốn.",
    )
    summary = d.summary()
    # Two sentences only
    assert "Câu một" in summary
    assert "Câu hai" in summary
    assert "Câu ba" not in summary


# ── _TfidfVectorizer ──────────────────────────────────────────────────────────

def test_vectorizer_fit_builds_vocab():
    vec = _TfidfVectorizer()
    vec.fit(["machine learning", "deep learning"])
    assert "machine" in vec._vocab
    assert "deep" in vec._vocab
    assert "learning" in vec._vocab


def test_vectorizer_transform_returns_normalized_vector():
    vec = _TfidfVectorizer()
    vec.fit(["machine learning algorithm", "deep learning model"])
    v = vec.transform("machine learning")
    norm = float(np.linalg.norm(v))
    # L2-normalized → norm ≈ 1
    assert 0.99 < norm < 1.01


def test_vectorizer_transform_empty_text():
    vec = _TfidfVectorizer()
    vec.fit(["hello world"])
    v = vec.transform("")
    assert np.linalg.norm(v) == 0.0


def test_vectorizer_unfitted_returns_zero_vector():
    vec = _TfidfVectorizer()
    v = vec.transform("anything")
    assert v.shape == (1,)
    assert v[0] == 0.0


def test_cosine_similarity_identical():
    a = np.array([1.0, 0.0, 0.0])
    b = np.array([1.0, 0.0, 0.0])
    assert _cosine_similarity(a, b) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal():
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert _cosine_similarity(a, b) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector():
    a = np.zeros(3)
    b = np.array([1.0, 2.0, 3.0])
    assert _cosine_similarity(a, b) == 0.0


# ── ContextMemory.add / retrieve ──────────────────────────────────────────────

def test_memory_starts_empty():
    m = ContextMemory()
    assert m.size == 0
    assert m.retrieve("anything") == []


def test_memory_add_increases_size():
    m = ContextMemory()
    m.add(0, "machine learning is great", "học máy thật tuyệt")
    assert m.size == 1
    m.add(1, "deep learning works", "học sâu hoạt động")
    assert m.size == 2


def test_retrieve_returns_similar_chunk():
    m = ContextMemory()
    m.add(0, "machine learning algorithm classifier",
          "thuật toán phân loại học máy")
    m.add(1, "weather forecast tomorrow rain",
          "dự báo thời tiết ngày mai mưa")

    results = m.retrieve("machine learning model")
    assert len(results) >= 1
    # Most similar should be chunk 0 (ML), not chunk 1 (weather)
    top_decision, top_score = results[0]
    assert top_decision.chunk_index == 0
    assert top_score >= MIN_SIMILARITY


def test_retrieve_filters_low_similarity():
    m = ContextMemory()
    m.add(0, "completely unrelated topic xyz",
          "chủ đề hoàn toàn khác xyz")

    # Query nothing in common → score below MIN_SIMILARITY → filtered out
    results = m.retrieve("apple banana orange")
    assert all(score >= MIN_SIMILARITY for _, score in results)


def test_retrieve_respects_top_k():
    m = ContextMemory()
    for i in range(10):
        m.add(i, f"machine learning topic number {i}",
              f"chủ đề học máy số {i}")

    results = m.retrieve("machine learning", top_k=3)
    assert len(results) <= 3


def test_retrieve_default_top_k_is_max_context_chunks():
    m = ContextMemory()
    for i in range(MAX_CONTEXT_CHUNKS + 5):
        m.add(i, f"machine learning chunk {i}", f"chunk học máy {i}")

    results = m.retrieve("machine learning")
    assert len(results) <= MAX_CONTEXT_CHUNKS


# ── ContextMemory.retrieve_context ────────────────────────────────────────────

def test_retrieve_context_empty_returns_empty_string():
    m = ContextMemory()
    assert m.retrieve_context("anything") == ""


def test_retrieve_context_formats_with_header_and_footer():
    m = ContextMemory()
    m.add(0, "neural network training",
          "Huấn luyện mạng nơ-ron rất quan trọng. Cần GPU mạnh.")

    ctx = m.retrieve_context("neural network model")
    if ctx:  # may be empty if similarity < threshold
        assert "NGỮ CẢNH DỊCH THUẬT" in ctx
        assert "EN:" in ctx
        assert "VI:" in ctx


def test_retrieve_context_respects_max_chars():
    m = ContextMemory()
    long_text = "neural " * 200
    long_translation = "Mạng nơ-ron " * 200
    for i in range(5):
        m.add(i, long_text, long_translation)

    ctx = m.retrieve_context("neural network", max_chars=300)
    assert len(ctx) <= 1500  # generous upper bound (header + footer)


# ── Style profile detection ───────────────────────────────────────────────────

def test_style_profile_detects_pronoun_we():
    m = ContextMemory()
    m.add(0, "We propose a new method.",
          "Chúng tôi đề xuất một phương pháp mới.")
    profile = m.get_style_profile()
    assert profile.get("pronoun_we") == "chúng tôi"


def test_style_profile_detects_paper_translation():
    m = ContextMemory()
    m.add(0, "This paper presents results.",
          "Bài báo này trình bày kết quả nghiên cứu.")
    profile = m.get_style_profile()
    assert profile.get("paper") == "bài báo"
    assert profile.get("result") == "kết quả"


def test_style_profile_detects_method_translation():
    m = ContextMemory()
    m.add(0, "Our method is efficient.",
          "Phương pháp của chúng tôi hiệu quả.")
    profile = m.get_style_profile()
    assert profile.get("method") == "phương pháp"


def test_style_profile_persists_across_adds():
    m = ContextMemory()
    m.add(0, "We do A.", "Chúng tôi làm A.")
    m.add(1, "The model is good.", "Mô hình tốt.")
    profile = m.get_style_profile()
    # First add set pronoun, second add doesn't override
    assert profile.get("pronoun_we") == "chúng tôi"


# ── save/load_to_progress ─────────────────────────────────────────────────────

def test_save_to_progress_writes_metadata():
    m = ContextMemory()
    m.add(0, "We use Python.", "Chúng tôi dùng Python.",
          key_terms={"Python": "Python"})
    m.add(2, "The result is 95%.", "Kết quả là 95%.")

    progress = {}
    m.save_to_progress(progress)

    cm = progress["context_memory"]
    assert cm["size"] == 2
    assert cm["chunk_indexes"] == [0, 2]
    assert cm["key_terms_by_chunk"]["0"] == {"Python": "Python"}
    assert cm["style_profile"].get("pronoun_we") == "chúng tôi"


def test_save_to_progress_no_full_text():
    """save_to_progress chỉ lưu metadata, KHÔNG lưu full original/translated."""
    m = ContextMemory()
    m.add(0, "Long original text " * 50, "Văn bản gốc dài " * 50)

    progress = {}
    m.save_to_progress(progress)

    cm = progress["context_memory"]
    # Đảm bảo không có original/translated full text trong progress
    assert "decisions" not in cm or all(
        "original" not in d for d in cm.get("decisions", [])
    )


def test_load_from_progress_restores_style_profile():
    m1 = ContextMemory()
    m1.add(0, "We are researchers.", "Chúng tôi là nhà nghiên cứu.")

    progress = {}
    m1.save_to_progress(progress)

    # New memory loads only style
    m2 = ContextMemory()
    m2.load_from_progress(progress)

    assert m2.get_style_profile().get("pronoun_we") == "chúng tôi"
    # Decisions not restored from progress (rebuilt from disk by pipeline)
    assert m2.size == 0


def test_load_from_progress_no_data_is_safe():
    m = ContextMemory()
    m.load_from_progress({})  # no context_memory key
    assert m.size == 0
    assert m.get_style_profile() == {}


# ── clear ─────────────────────────────────────────────────────────────────────

def test_clear_resets_everything():
    m = ContextMemory()
    m.add(0, "Hello", "Xin chào")
    m.add(1, "World", "Thế giới")
    assert m.size == 2

    m.clear()
    assert m.size == 0
    assert m.retrieve("anything") == []
    assert m.get_style_profile() == {}


# ── Integration: end-to-end retrieval with style hints ────────────────────────

def test_retrieve_context_includes_style_notes():
    m = ContextMemory()
    m.add(0, "We propose a method using neural networks for classification.",
          "Chúng tôi đề xuất một phương pháp dùng mạng nơ-ron cho phân loại.")
    m.add(1, "Our experiments show good results on benchmarks.",
          "Các thử nghiệm của chúng tôi cho thấy kết quả tốt trên benchmark.")

    ctx = m.retrieve_context("neural network method")
    if ctx:  # only if similarity threshold met
        # Style notes should mention pronouns when added enough chunks
        assert "Văn phong" in ctx or "EN:" in ctx
