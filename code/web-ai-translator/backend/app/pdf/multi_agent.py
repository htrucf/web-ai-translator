"""Multi-agent translation orchestration.

Two patterns implemented:

Pattern A — Post-hoc disagreement analysis (recommended for thesis):
  Given existing Gemini translations (chunk files), retranslate the same
  EN source with Ollama, compute ChrF++ agreement between the two VI outputs.
  Chunks with low agreement → flagged as "uncertain" → Ollama synthesizes best.

  Gemini output  ──┐
                   ├─ ChrF++(VI_A, VI_B) ─ if < threshold → Ollama synthesis
  Ollama output  ──┘

Pattern B — Real-time dual translation (future work, not implemented here):
  Would require two concurrent browser sessions; too expensive for local dev.

Why ChrF++ between two VI translations works as an agreement metric:
  Two strong models translating the same sentence should produce outputs with
  ChrF++ ≥ 65 (same meaning, different wording). ChrF++ < 45 between two good
  models strongly suggests the source sentence is ambiguous or one model erred.

Metrics produced:
  - agreement_score: ChrF++ between Gemini and Ollama translations (per chunk)
  - synthesis_used: True if Ollama synthesized from both (agreement < threshold)
  - final_translation: The "best" output (Gemini if agreement OK, synthesis otherwise)

References:
  Wang et al. (2024) "Mixture-of-Agents Enhances Large Language Model Capabilities"
  Du et al. (2023) "Improving Factuality via Multi-Agent Debate" (MIT/Google Brain)
"""

import os
import re
import json
import logging
import time

from app.audit import log_event
import httpx
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_ARBITER_MODEL = "qwen2.5:7b"

# Agreement thresholds
HIGH_AGREEMENT = 65.0    # Both models agree → use Gemini (primary)
LOW_AGREEMENT  = 40.0    # Strong disagreement → synthesize
# Between 40-65: mild disagreement → Ollama picks better one (no synthesis)

SEGMENT_TIMEOUT = 120.0


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class AgentSegmentResult:
    index: int
    en_source: str
    vi_primary: str           # Gemini translation
    vi_secondary: str         # Ollama translation
    vi_final: str             # Final chosen/synthesized
    agreement_score: float    # ChrF++(vi_primary, vi_secondary)  0-100
    verdict: str              # "consensus" | "mild_disagreement" | "synthesized"
    synthesis_used: bool = False
    arbiter_reasoning: str = ""


@dataclass
class MultiAgentReport:
    """Report from multi-agent translation comparison."""
    num_segments: int = 0
    mean_agreement: float = 0.0
    high_agreement_count: int = 0    # ChrF++ >= 65
    mild_disagreement_count: int = 0 # 40 <= ChrF++ < 65
    synthesized_count: int = 0       # ChrF++ < 40, Ollama synthesized
    arbiter_model: str = ""
    available: bool = True
    error: str | None = None
    segments: list = field(default_factory=list)   # list of AgentSegmentResult dicts
    agreement_distribution: list = field(default_factory=list)  # histogram buckets

    def to_dict(self) -> dict:
        return {
            "num_segments": self.num_segments,
            "mean_agreement": round(self.mean_agreement, 1),
            "high_agreement_count": self.high_agreement_count,
            "mild_disagreement_count": self.mild_disagreement_count,
            "synthesized_count": self.synthesized_count,
            "arbiter_model": self.arbiter_model,
            "available": self.available,
            "error": self.error,
            "agreement_distribution": self.agreement_distribution,
            "segments": [
                {
                    "index": s["index"],
                    "agreement_score": round(s["agreement_score"], 1),
                    "verdict": s["verdict"],
                    "synthesis_used": s["synthesis_used"],
                    "en_source": s["en_source"][:200],
                    "vi_primary": s["vi_primary"][:200],
                    "vi_secondary": s["vi_secondary"][:200],
                    "vi_final": s["vi_final"][:200],
                    "arbiter_reasoning": s.get("arbiter_reasoning", ""),
                }
                for s in self.segments
            ],
        }

    def interpretation(self) -> str:
        if self.num_segments == 0:
            return "Chưa có dữ liệu"
        pct_consensus = self.high_agreement_count / self.num_segments * 100
        if pct_consensus >= 80:
            return f"{pct_consensus:.0f}% chunks đồng thuận cao — bản dịch Gemini đáng tin cậy"
        if pct_consensus >= 60:
            return f"{pct_consensus:.0f}% chunks đồng thuận — chất lượng ổn, một số chunk cần kiểm tra"
        return (
            f"Chỉ {pct_consensus:.0f}% đồng thuận — nhiều chunks có bất đồng giữa 2 model, "
            f"cần review thủ công"
        )


# ── Ollama helpers ────────────────────────────────────────────────────

def _ollama_translate(en_text: str, model: str, timeout: float = SEGMENT_TIMEOUT) -> str | None:
    """Translate EN → VI using Ollama (secondary translator)."""
    prompt = (
        "Dịch đoạn văn bản học thuật sau sang tiếng Việt.\n"
        "Quy tắc:\n"
        "1. Giữ nguyên số thứ tự [1], [2]... nếu có.\n"
        "2. Giữ nguyên công thức toán, số liệu, tên riêng, citations.\n"
        "3. CHỈ trả về bản dịch, không giải thích.\n\n"
        f"=== Văn bản gốc (Tiếng Anh) ===\n{en_text}\n\n"
        "=== Bản dịch (Tiếng Việt) ==="
    )
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 2048},
            },
            timeout=timeout,
        )
        if not r.is_success:
            logger.warning(f"[MultiAgent] Ollama translate error: {r.status_code}")
            return None
        raw = r.json().get("response", "").strip()
        return raw if raw else None
    except httpx.TimeoutException:
        logger.warning("[MultiAgent] Ollama translate timeout")
        return None
    except Exception as e:
        logger.error(f"[MultiAgent] Ollama translate: {e}")
        return None


def _ollama_pick_or_synthesize(
    en_source: str,
    vi_a: str,
    vi_b: str,
    model: str,
    synthesize: bool = True,
    timeout: float = SEGMENT_TIMEOUT,
) -> tuple[str, str]:
    """Use Ollama to pick the better translation or synthesize from both.

    Returns (final_translation, reasoning).
    """
    if synthesize:
        task = "Tạo bản dịch TỐT NHẤT kết hợp ưu điểm của cả hai bản."
    else:
        task = "Chọn bản dịch TỐT HƠN (A hoặc B) và trả về nguyên văn bản đó."

    prompt = (
        "Bạn là chuyên gia dịch thuật học thuật Anh-Việt.\n"
        "Hai AI đã dịch cùng một đoạn văn và cho kết quả khác nhau.\n\n"
        f"=== Văn bản gốc (Tiếng Anh) ===\n{en_source[:800]}\n\n"
        f"=== Bản A (Gemini) ===\n{vi_a[:800]}\n\n"
        f"=== Bản B (Ollama) ===\n{vi_b[:800]}\n\n"
        f"Nhiệm vụ: {task}\n\n"
        "Trả về JSON với cấu trúc:\n"
        '{"translation": "<bản dịch cuối cùng>", '
        '"reasoning": "<1-2 câu giải thích ngắn>"}'
    )
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 2048},
            },
            timeout=timeout,
        )
        if not r.is_success:
            return vi_a, ""  # fallback to primary
        raw = r.json().get("response", "").strip()

        # Parse JSON response
        import json as _json
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                obj = _json.loads(match.group())
                trans = obj.get("translation", "").strip()
                reasoning = obj.get("reasoning", "").strip()
                if trans:
                    return trans, reasoning
            except Exception:
                pass
        # If JSON parse failed, try to extract translation directly
        return vi_a, ""  # fallback to primary
    except Exception as e:
        logger.error(f"[MultiAgent] Arbiter error: {e}")
        return vi_a, ""


# ── ChrF++ agreement ─────────────────────────────────────────────────

def _agreement_chrf(vi_a: str, vi_b: str) -> float:
    """Compute ChrF++ between two VI translations as agreement metric."""
    try:
        from sacrebleu.metrics import CHRF
        metric = CHRF(char_order=6, word_order=2)
        return metric.sentence_score(vi_a, [vi_b]).score
    except ImportError:
        pass
    # Fallback: simple character overlap
    from collections import Counter
    def _char_ngrams(text, n):
        text = text.replace(" ", "▁")
        return Counter(text[i:i+n] for i in range(max(0, len(text) - n + 1)))
    scores = []
    for n in range(1, 7):
        a_ng = _char_ngrams(vi_a, n)
        b_ng = _char_ngrams(vi_b, n)
        total_a = sum(a_ng.values())
        total_b = sum(b_ng.values())
        if total_a == 0 or total_b == 0:
            continue
        matched = sum((a_ng & b_ng).values())
        p = matched / total_a
        r = matched / total_b
        if p + r > 0:
            scores.append(2 * p * r / (p + r))
    return sum(scores) / len(scores) * 100 if scores else 0.0


# ── Availability check ────────────────────────────────────────────────

def is_available(model: str = DEFAULT_ARBITER_MODEL) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        if not r.is_success:
            return False
        models = [m["name"] for m in r.json().get("models", [])]
        base = model.split(":")[0]
        return any(m.startswith(base) for m in models)
    except Exception:
        return False


# ── Main evaluation ───────────────────────────────────────────────────

def run_multi_agent_evaluation(
    job_dir: str,
    arbiter_model: str = DEFAULT_ARBITER_MODEL,
    max_chunks: int = 20,
    high_agreement_threshold: float = HIGH_AGREEMENT,
    low_agreement_threshold: float = LOW_AGREEMENT,
    run_synthesis: bool = True,
    on_progress=None,
) -> MultiAgentReport:
    """Post-hoc multi-agent analysis on existing chunk files.

    For each chunk:
    1. Read Gemini translation from chunk_XXX_translated.txt (primary)
    2. Retranslate EN source via Ollama (secondary)
    3. Compute ChrF++ agreement between the two VI outputs
    4. If agreement < low_threshold and run_synthesis=True:
       → Ollama synthesizes "best of both" translation
    5. If low_threshold <= agreement < high_threshold:
       → Ollama picks the better translation

    Args:
        job_dir: Path to workspace/jobs/{job_id}
        arbiter_model: Ollama model used for secondary translation + arbitration
        max_chunks: Max chunks to evaluate
        high_agreement_threshold: ChrF++ >= this → consensus, use Gemini
        low_agreement_threshold: ChrF++ < this → synthesize
        run_synthesis: Whether to run Ollama synthesis for low-agreement chunks
        on_progress: Optional callback(current, total)

    Returns:
        MultiAgentReport with per-chunk results and agreement distribution
    """
    report = MultiAgentReport(arbiter_model=arbiter_model)
    started_at = time.time()

    if not is_available(arbiter_model):
        report.available = False
        report.error = (
            f"Ollama không chạy hoặc model '{arbiter_model}' chưa pull. "
            f"Chạy: ollama pull {arbiter_model}"
        )
        log_event("multi_agent.unavailable",
                  arbiter_model=arbiter_model, reason="ollama_or_model_missing")
        return report

    chunks_dir = os.path.join(job_dir, "chunks")
    if not os.path.isdir(chunks_dir):
        report.available = False
        report.error = "Không tìm thấy chunks/ — job chưa hoàn thành?"
        log_event("multi_agent.unavailable",
                  arbiter_model=arbiter_model, reason="chunks_dir_missing")
        return report

    # Load chunk pairs
    pairs = []
    for fname in sorted(os.listdir(chunks_dir)):
        if not fname.endswith("_original.txt"):
            continue
        m = re.match(r"chunk_(\d+)_original\.txt", fname)
        if not m:
            continue
        idx = int(m.group(1))
        trans_f = os.path.join(chunks_dir, f"chunk_{idx:03d}_translated.txt")
        orig_f  = os.path.join(chunks_dir, fname)
        if not os.path.exists(trans_f):
            continue
        try:
            en = open(orig_f, encoding="utf-8").read().strip()
            vi_gemini = open(trans_f, encoding="utf-8").read().strip()
            if en and vi_gemini and len(en) >= 30:
                pairs.append({"index": idx, "en": en, "vi_primary": vi_gemini})
        except Exception as e:
            logger.warning(f"[MultiAgent] Could not read chunk {idx}: {e}")

    if not pairs:
        report.error = "Không có chunk files để đánh giá"
        return report

    pairs = sorted(pairs, key=lambda p: p["index"])[:max_chunks]
    report.num_segments = len(pairs)

    logger.info(
        f"[MultiAgent] Evaluating {len(pairs)} chunks, "
        f"model={arbiter_model}, synthesis={run_synthesis}"
    )
    log_event("multi_agent.started",
              arbiter_model=arbiter_model,
              num_segments=len(pairs),
              run_synthesis=run_synthesis,
              high_agreement_threshold=high_agreement_threshold,
              low_agreement_threshold=low_agreement_threshold,
              max_chunks=max_chunks)

    all_scores = []
    for i, pair in enumerate(pairs):
        if on_progress:
            on_progress(i + 1, len(pairs))

        logger.info(f"[MultiAgent] Processing chunk {pair['index']} ({i+1}/{len(pairs)})...")

        # Step 1: Secondary translation via Ollama
        vi_ollama = _ollama_translate(pair["en"], arbiter_model)
        if vi_ollama is None:
            logger.warning(f"[MultiAgent] Chunk {pair['index']} secondary translation failed")
            vi_ollama = ""

        # Step 2: Agreement score
        if vi_ollama:
            agreement = _agreement_chrf(pair["vi_primary"], vi_ollama)
        else:
            agreement = 0.0

        # Step 3: Arbitration
        if not vi_ollama:
            # No secondary translation — keep primary
            vi_final = pair["vi_primary"]
            verdict = "consensus"
            synthesis_used = False
            reasoning = ""
        elif agreement >= high_agreement_threshold:
            # High agreement → use primary (Gemini usually better)
            vi_final = pair["vi_primary"]
            verdict = "consensus"
            synthesis_used = False
            reasoning = ""
            report.high_agreement_count += 1
        elif agreement >= low_agreement_threshold:
            # Mild disagreement → Ollama picks better one
            verdict = "mild_disagreement"
            report.mild_disagreement_count += 1
            if run_synthesis:
                vi_final, reasoning = _ollama_pick_or_synthesize(
                    pair["en"], pair["vi_primary"], vi_ollama,
                    arbiter_model, synthesize=False
                )
            else:
                vi_final = pair["vi_primary"]
                reasoning = ""
            synthesis_used = False
        else:
            # Strong disagreement → synthesize
            verdict = "synthesized"
            report.synthesized_count += 1
            if run_synthesis:
                vi_final, reasoning = _ollama_pick_or_synthesize(
                    pair["en"], pair["vi_primary"], vi_ollama,
                    arbiter_model, synthesize=True
                )
                synthesis_used = True
            else:
                vi_final = pair["vi_primary"]
                reasoning = ""
                synthesis_used = False

        seg = {
            "index": pair["index"],
            "en_source": pair["en"],
            "vi_primary": pair["vi_primary"],
            "vi_secondary": vi_ollama,
            "vi_final": vi_final,
            "agreement_score": agreement,
            "verdict": verdict,
            "synthesis_used": synthesis_used,
            "arbiter_reasoning": reasoning,
        }
        report.segments.append(seg)
        all_scores.append(agreement)
        log_event("multi_agent.segment_done",
                  chunk_index=pair["index"],
                  agreement_score=round(agreement, 2),
                  verdict=verdict,
                  synthesis_used=synthesis_used,
                  secondary_available=bool(vi_ollama))

    # Aggregate
    if all_scores:
        report.mean_agreement = sum(all_scores) / len(all_scores)

    # Agreement distribution (0-10, 10-20, ..., 90-100)
    buckets = [0] * 10
    for s in all_scores:
        bucket = min(9, int(s / 10))
        buckets[bucket] += 1
    report.agreement_distribution = buckets

    # Sort segments by agreement ascending (worst first for display)
    report.segments.sort(key=lambda s: s["agreement_score"])

    logger.info(
        f"[MultiAgent] Done. Mean agreement: {report.mean_agreement:.1f}, "
        f"consensus={report.high_agreement_count}, "
        f"mild={report.mild_disagreement_count}, "
        f"synthesized={report.synthesized_count}"
    )
    log_event("multi_agent.done",
              arbiter_model=arbiter_model,
              mean_agreement=round(report.mean_agreement, 2),
              high_agreement_count=report.high_agreement_count,
              mild_disagreement_count=report.mild_disagreement_count,
              synthesized_count=report.synthesized_count,
              num_segments=report.num_segments,
              latency_seconds=round(time.time() - started_at, 3))
    return report


def apply_synthesis_to_chunks(job_dir: str, report: MultiAgentReport) -> int:
    """Write synthesized translations back to chunk files.

    For chunks where synthesis was used, overwrites the _translated.txt file
    with the synthesized version. This allows the rebuilt PDF to use the
    multi-agent improved translations.

    Returns: number of chunks updated.
    """
    if not report.available or not report.segments:
        return 0

    updated = 0
    chunks_dir = os.path.join(job_dir, "chunks")

    for seg in report.segments:
        if not seg.get("synthesis_used") and seg.get("verdict") == "consensus":
            continue  # No change needed
        if not seg.get("vi_final"):
            continue

        trans_file = os.path.join(chunks_dir, f"chunk_{seg['index']:03d}_translated.txt")
        try:
            with open(trans_file, "w", encoding="utf-8") as f:
                f.write(seg["vi_final"])
            updated += 1
        except Exception as e:
            logger.warning(f"[MultiAgent] Could not write chunk {seg['index']}: {e}")

    logger.info(f"[MultiAgent] Applied synthesis to {updated} chunks")
    return updated
