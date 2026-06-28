"""ChrF++ metric for Vietnamese MT evaluation.

Based on: Popović (2015) "chrF: character n-gram F-score for automatic MT evaluation"
         WMT 2015, ACL Anthology W15-3049

Why ChrF for Vietnamese (not BLEU):
- Vietnamese is an isolating language — word segmentation is ambiguous
- ChrF operates at character level → no tokenization needed
- Validated on Chinese/Thai (same typological family) to outperform BLEU
- Popović (2015): ChrF correlates better with human judgments on Asian languages

ChrF formula (β=1, recall=precision balanced, char n-gram order 6):
    precision_n = |matched char-ngrams in hyp| / |total char-ngrams in hyp|
    recall_n    = |matched char-ngrams in hyp| / |total char-ngrams in ref|
    F_n = (1+β²) * P_n * R_n / (β²*P_n + R_n)
    ChrF = mean(F_n) for n in 1..max_order

ChrF++ also incorporates word n-grams (word_order=2 by default).

Implementation uses sacrebleu — the standard reference tool for MT evaluation.
If sacrebleu is not installed, falls back to a pure-Python ChrF implementation.
"""

import re
import logging
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Try sacrebleu first (standard tool) ─────────────────────────────

def _try_sacrebleu_chrf(hypotheses: list[str], references: list[str],
                         char_order: int = 6, word_order: int = 2,
                         beta: float = 1.0) -> float | None:
    """Compute corpus-level ChrF++ using sacrebleu. Returns None if unavailable."""
    try:
        from sacrebleu.metrics import CHRF
        metric = CHRF(char_order=char_order, word_order=word_order, beta=beta)
        result = metric.corpus_score(hypotheses, [references])
        return result.score  # already 0-100
    except ImportError:
        return None
    except Exception as e:
        logger.warning(f"[ChrF] sacrebleu error: {e}")
        return None


# ── Pure-Python ChrF fallback ────────────────────────────────────────

def _char_ngrams(text: str, n: int) -> Counter:
    """Extract character n-grams from text."""
    text = text.replace(" ", "▁")  # treat spaces as characters
    return Counter(text[i:i+n] for i in range(len(text) - n + 1))


def _word_ngrams(text: str, n: int) -> Counter:
    """Extract word n-grams from text."""
    words = text.split()
    return Counter(tuple(words[i:i+n]) for i in range(len(words) - n + 1))


def _f_score(prec: float, rec: float, beta: float = 1.0) -> float:
    if prec + rec == 0:
        return 0.0
    beta2 = beta * beta
    return (1 + beta2) * prec * rec / (beta2 * prec + rec)


def _sentence_chrf(hyp: str, ref: str,
                   char_order: int = 6,
                   word_order: int = 2,
                   beta: float = 1.0) -> float:
    """Compute sentence-level ChrF++ (pure Python fallback)."""
    if not hyp.strip() or not ref.strip():
        return 0.0

    scores = []

    # Character n-grams
    for n in range(1, char_order + 1):
        hyp_ngrams = _char_ngrams(hyp, n)
        ref_ngrams = _char_ngrams(ref, n)

        matched = sum((hyp_ngrams & ref_ngrams).values())
        total_hyp = sum(hyp_ngrams.values())
        total_ref = sum(ref_ngrams.values())

        prec = matched / total_hyp if total_hyp > 0 else 0.0
        rec  = matched / total_ref if total_ref > 0 else 0.0
        scores.append(_f_score(prec, rec, beta))

    # Word n-grams (ChrF++)
    for n in range(1, word_order + 1):
        hyp_ngrams = _word_ngrams(hyp, n)
        ref_ngrams = _word_ngrams(ref, n)

        matched = sum((hyp_ngrams & ref_ngrams).values())
        total_hyp = sum(hyp_ngrams.values())
        total_ref = sum(ref_ngrams.values())

        prec = matched / total_hyp if total_hyp > 0 else 0.0
        rec  = matched / total_ref if total_ref > 0 else 0.0
        scores.append(_f_score(prec, rec, beta))

    return (sum(scores) / len(scores)) * 100 if scores else 0.0


def _corpus_chrf_pure(hypotheses: list[str], references: list[str],
                       char_order: int = 6, word_order: int = 2,
                       beta: float = 1.0) -> float:
    """Corpus-level ChrF++ (arithmetic mean of sentence scores)."""
    if not hypotheses:
        return 0.0
    scores = [
        _sentence_chrf(h, r, char_order, word_order, beta)
        for h, r in zip(hypotheses, references)
    ]
    return sum(scores) / len(scores)


# ── Public API ───────────────────────────────────────────────────────

@dataclass
class ChrFReport:
    """ChrF++ evaluation report."""
    corpus_score: float = 0.0          # 0-100, corpus-level ChrF++
    segment_scores: list = field(default_factory=list)  # per-segment scores
    num_segments: int = 0
    low_quality_segments: list = field(default_factory=list)
    char_order: int = 6
    word_order: int = 2
    backend: str = "unknown"           # "sacrebleu" or "pure-python"

    def to_dict(self) -> dict:
        return {
            "corpus_score": round(self.corpus_score, 2),
            "num_segments": self.num_segments,
            "low_quality_count": len(self.low_quality_segments),
            "char_order": self.char_order,
            "word_order": self.word_order,
            "backend": self.backend,
            "segment_scores": [
                {
                    "index": s["index"],
                    "score": round(s["score"], 2),
                    "hypothesis": s["hypothesis"][:200],
                    "reference": s["reference"][:200],
                }
                for s in self.low_quality_segments
            ],
            "interpretation": _interpret_score(self.corpus_score),
        }


def _interpret_score(score: float) -> str:
    """Human-readable interpretation of ChrF++ score (0-100)."""
    if score >= 70:
        return "Chất lượng tốt — bản dịch tương đương human reference"
    if score >= 55:
        return "Chất lượng khá — đọc hiểu được, có lỗi nhỏ"
    if score >= 40:
        return "Chất lượng trung bình — nội dung chính đúng, cần chỉnh sửa"
    if score >= 25:
        return "Chất lượng thấp — nhiều lỗi, khó đọc"
    return "Rất thấp — bản dịch gần như không dùng được"


def compute_chrf(
    hypotheses: list[str],
    references: list[str],
    char_order: int = 6,
    word_order: int = 2,
    beta: float = 1.0,
    low_threshold: float = 40.0,
) -> ChrFReport:
    """Compute ChrF++ for a list of (hypothesis, reference) pairs.

    Args:
        hypotheses: List of machine-translated Vietnamese strings.
        references: List of human reference Vietnamese strings.
        char_order: Max character n-gram order (default 6, per Popović 2015).
        word_order: Word n-gram order for ChrF++ (default 2; 0 = ChrF only).
        beta: Recall weight (default 1.0 = balanced; 2.0 = recall-heavy).
        low_threshold: Segment scores below this are flagged as low quality.

    Returns:
        ChrFReport with corpus and segment-level scores.
    """
    assert len(hypotheses) == len(references), \
        "hypotheses and references must have the same length"

    report = ChrFReport(char_order=char_order, word_order=word_order)
    report.num_segments = len(hypotheses)

    if not hypotheses:
        return report

    # Corpus score
    corpus_score = _try_sacrebleu_chrf(hypotheses, references, char_order, word_order, beta)
    if corpus_score is not None:
        report.backend = "sacrebleu"
        report.corpus_score = corpus_score
    else:
        report.backend = "pure-python"
        report.corpus_score = _corpus_chrf_pure(hypotheses, references, char_order, word_order, beta)

    # Per-segment scores (always use pure-python for per-segment)
    for i, (hyp, ref) in enumerate(zip(hypotheses, references)):
        seg_score = _sentence_chrf(hyp, ref, char_order, word_order, beta)
        report.segment_scores.append({"index": i, "score": seg_score})
        if seg_score < low_threshold:
            report.low_quality_segments.append({
                "index": i,
                "score": seg_score,
                "hypothesis": hyp,
                "reference": ref,
            })

    # Sort low quality by score ascending (worst first)
    report.low_quality_segments.sort(key=lambda x: x["score"])

    logger.info(
        f"[ChrF] corpus={report.corpus_score:.2f} "
        f"({report.num_segments} segments, backend={report.backend})"
    )
    return report


def compute_chrf_from_blocks(
    blocks: list,
    reference_map: dict[tuple[int, int], str],
    **kwargs,
) -> ChrFReport:
    """Compute ChrF++ from TextBlock objects with a reference map.

    Args:
        blocks: List of TextBlock objects (after translation).
        reference_map: Dict mapping (page_num, block_idx) -> reference VI string.
        **kwargs: Passed to compute_chrf.
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

    return compute_chrf(hypotheses, references, **kwargs)
