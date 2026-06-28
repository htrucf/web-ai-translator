"""BERTScore with PhoBERT backbone for Vietnamese MT evaluation.

Based on:
- Zhang et al. (2020) "BERTScore: Evaluating Text Generation with BERT"
- Dat Quoc Nguyen & Anh Tuan Nguyen (2020) "PhoBERT: Pre-trained language
  models for Vietnamese", EMNLP Findings 2020 (ACL Anthology 2020.findings-emnlp.92)

Why PhoBERT for Vietnamese:
- PhoBERT is trained on 20GB Vietnamese text — outperforms multilingual models
- BERTScore uses contextual embeddings → captures semantic similarity
- Handles paraphrasing that ChrF/BLEU miss ("học" vs "nghiên cứu" same meaning)
- Đinh Điền et al. (2019) showed Vietnamese needs language-specific tools

Model: vinai/phobert-base (PhoBERT-base, 135M params)
       or vinai/phobert-large (PhoBERT-large, 370M params)

Requires (optional, graceful fallback):
    pip install bert-score transformers
    pip install underthesea  # for Vietnamese word segmentation (improves accuracy)

Note: bert-score can run on CPU (slow but works). First run downloads PhoBERT (~540MB).
"""

import logging
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default model — PhoBERT-base (balanced quality/speed)
DEFAULT_MODEL = "vinai/phobert-base"
# Larger model for higher quality (use when GPU available)
LARGE_MODEL = "vinai/phobert-large"


@dataclass
class BertScoreReport:
    """BERTScore evaluation report using PhoBERT."""
    precision: float = 0.0         # Corpus-level precision (0-1)
    recall: float = 0.0            # Corpus-level recall (0-1)
    f1: float = 0.0                # Corpus-level F1 (0-1)
    f1_percent: float = 0.0        # F1 as percentage (0-100)
    segment_scores: list = field(default_factory=list)   # per-segment F1
    low_quality_segments: list = field(default_factory=list)
    num_segments: int = 0
    model_name: str = DEFAULT_MODEL
    available: bool = True
    unavailable_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "f1_percent": round(self.f1_percent, 1),
            "num_segments": self.num_segments,
            "low_quality_count": len(self.low_quality_segments),
            "model": self.model_name,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "low_quality_segments": [
                {
                    "index": s["index"],
                    "f1": round(s["f1"], 4),
                    "hypothesis": s["hypothesis"][:200],
                    "reference": s["reference"][:200],
                }
                for s in self.low_quality_segments
            ],
            "interpretation": _interpret_f1(self.f1),
        }


def _interpret_f1(f1: float) -> str:
    """Human-readable interpretation of BERTScore F1."""
    if f1 >= 0.92:
        return "Xuất sắc — ngữ nghĩa tương đương hoàn toàn với reference"
    if f1 >= 0.87:
        return "Tốt — ngữ nghĩa đúng, có khác biệt nhỏ về cách diễn đạt"
    if f1 >= 0.80:
        return "Khá — nội dung chính đúng, một số lỗi ngữ nghĩa"
    if f1 >= 0.70:
        return "Trung bình — mất nhiều thông tin hoặc sai nghĩa ở một số đoạn"
    return "Thấp — bản dịch có nhiều lỗi ngữ nghĩa nghiêm trọng"


def is_available() -> bool:
    """Check if bert_score + transformers are installed."""
    try:
        import bert_score  # noqa: F401
        import transformers  # noqa: F401
        return True
    except ImportError:
        return False


def unavailable_reason() -> str | None:
    """Return reason why BERTScore-VI is unavailable, or None if available."""
    missing = []
    try:
        import bert_score  # noqa: F401
    except ImportError:
        missing.append("bert-score")
    try:
        import transformers  # noqa: F401
    except ImportError:
        missing.append("transformers")
    if missing:
        return f"Cần cài: pip install {' '.join(missing)}"
    return None


def _try_segment_vi(text: str) -> str:
    """Try to segment Vietnamese text with underthesea (improves BERTScore accuracy)."""
    try:
        from underthesea import word_tokenize
        return word_tokenize(text, format="text")
    except Exception:
        return text  # fallback: use raw text


def compute_bertscore(
    hypotheses: list[str],
    references: list[str],
    model_name: str = DEFAULT_MODEL,
    use_segmentation: bool = False,
    low_threshold: float = 0.80,
    batch_size: int = 32,
    device: str = "cpu",
) -> BertScoreReport:
    """Compute BERTScore using PhoBERT for Vietnamese MT evaluation.

    Args:
        hypotheses: List of machine-translated Vietnamese strings.
        references: List of human reference Vietnamese strings.
        model_name: HuggingFace model ID (default: vinai/phobert-base).
        use_segmentation: If True, apply Vietnamese word segmentation first.
                          Requires underthesea. Slightly improves accuracy.
        low_threshold: F1 scores below this flag the segment as low quality.
        batch_size: Batch size for model inference.
        device: "cpu" or "cuda".

    Returns:
        BertScoreReport with P/R/F1 at corpus and segment level.
    """
    assert len(hypotheses) == len(references), \
        "hypotheses and references must have the same length"

    report = BertScoreReport(model_name=model_name)
    report.num_segments = len(hypotheses)

    reason = unavailable_reason()
    if reason:
        report.available = False
        report.unavailable_reason = reason
        logger.warning(f"[BertScore-VI] Not available: {reason}")
        return report

    if not hypotheses:
        return report

    try:
        from bert_score import score as bert_score_fn

        # Optional Vietnamese segmentation
        if use_segmentation:
            hyps = [_try_segment_vi(h) for h in hypotheses]
            refs = [_try_segment_vi(r) for r in references]
        else:
            hyps = hypotheses
            refs = references

        logger.info(
            f"[BertScore-VI] Scoring {len(hyps)} segments "
            f"with {model_name} on {device}..."
        )

        P, R, F1 = bert_score_fn(
            hyps, refs,
            model_type=model_name,
            lang="vi",          # tells bert_score to use Vietnamese rescaling
            device=device,
            batch_size=batch_size,
            verbose=False,
        )

        # Corpus-level scores (mean)
        report.precision = float(P.mean())
        report.recall = float(R.mean())
        report.f1 = float(F1.mean())
        report.f1_percent = report.f1 * 100

        # Per-segment scores
        for i, (p, r, f) in enumerate(zip(P.tolist(), R.tolist(), F1.tolist())):
            report.segment_scores.append({
                "index": i, "precision": round(p, 4),
                "recall": round(r, 4), "f1": round(f, 4),
            })
            if f < low_threshold:
                report.low_quality_segments.append({
                    "index": i,
                    "f1": f,
                    "hypothesis": hypotheses[i],
                    "reference": references[i],
                })

        report.low_quality_segments.sort(key=lambda x: x["f1"])

        logger.info(
            f"[BertScore-VI] F1={report.f1:.4f} "
            f"({len(report.low_quality_segments)} low-quality segments)"
        )

    except Exception as e:
        logger.error(f"[BertScore-VI] Failed: {e}")
        report.available = False
        report.unavailable_reason = str(e)

    return report


def compute_bertscore_from_blocks(
    blocks: list,
    reference_map: dict[tuple[int, int], str],
    **kwargs,
) -> BertScoreReport:
    """Compute BERTScore from TextBlock objects with a reference map.

    Args:
        blocks: List of TextBlock objects (after translation).
        reference_map: Dict mapping (page_num, block_idx) -> reference VI string.
        **kwargs: Passed to compute_bertscore.
    """
    hypotheses = []
    references = []

    for b in blocks:
        if not b.is_translatable:
            continue
        key = (b.page_num, b.block_idx)
        ref = reference_map.get(key)
        if ref and b.translated_text:
            hypotheses.append(b.translated_text.strip())
            references.append(ref.strip())

    return compute_bertscore(hypotheses, references, **kwargs)
