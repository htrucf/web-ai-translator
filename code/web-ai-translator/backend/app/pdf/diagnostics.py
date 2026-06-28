"""Auto-diagnostics: detect root causes of low translation quality.

Analyzes chunk files, progress.json, and the heuristic quality report to
determine WHY translation quality is low — not just that it is.

Phase 1 detectors: TRUNCATED_RESPONSE, EMPTY_TRANSLATION, MATH_CONTAMINATION,
                   HALLUCINATION, BROWSER_CRASH, CHUNK_BOUNDARY_SPLIT
Phase 2 detectors: SESSION_LIMIT (length-ratio proxy), GLOSSARY_DRIFT
"""

import os
import re
import glob
from dataclasses import dataclass, field

# ─── Cause metadata ─────────────────────────────────────────────────────

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}

CAUSE_LABELS = {
    "TRUNCATED_RESPONSE":   "Gemini cắt phản hồi giữa chừng",
    "EMPTY_TRANSLATION":    "Chunk không được dịch",
    "SESSION_LIMIT":        "Gemini đạt giới hạn context",
    "MATH_CONTAMINATION":   "Công thức toán bị mix vào block text",
    "HALLUCINATION":        "Bản dịch dài bất thường (có thể thêm nội dung)",
    "GLOSSARY_DRIFT":       "Thuật ngữ dịch không nhất quán",
    "BROWSER_CRASH":        "Trình duyệt bị đóng giữa quá trình dịch",
    "CHUNK_BOUNDARY_SPLIT": "Câu bị cắt tại ranh giới chunk",
}

CAUSE_RECOMMENDATIONS = {
    "TRUNCATED_RESPONSE":   "Giảm kích thước chunk (từ 1500 xuống 1000 ký tự) và dịch lại các chunk bị ảnh hưởng.",
    "EMPTY_TRANSLATION":    "Retry các chunk thất bại. Kiểm tra kết nối Gemini.",
    "SESSION_LIMIT":        "Chuyển sang chế độ 'Sách dài' để xoay session thường xuyên hơn (mỗi 5 chunks thay vì 10).",
    "MATH_CONTAMINATION":   "Kiểm tra ngưỡng phân loại math block — một số công thức bị lọt vào text block.",
    "HALLUCINATION":        "Xem xét các chunk cụ thể — Gemini có thể đã thêm nội dung không có trong bản gốc.",
    "GLOSSARY_DRIFT":       "Bật/cập nhật glossary và dịch lại tài liệu để đồng nhất thuật ngữ.",
    "BROWSER_CRASH":        "Chạy lại pipeline — hệ thống sẽ tự resume từ chunk đã dịch dở.",
    "CHUNK_BOUNDARY_SPLIT": "Cải thiện logic chunking để tách tại ranh giới câu, không cắt giữa câu.",
}


# ─── Data structures ────────────────────────────────────────────────────

@dataclass
class DiagnosticFinding:
    cause: str
    severity: str                          # "critical" | "warning" | "info"
    confidence: float                      # 0.0 – 1.0
    evidence: list = field(default_factory=list)
    affected_chunks: list = field(default_factory=list)
    recommendation: str = ""
    auto_fixable: bool = False

    def weight(self) -> float:
        return SEVERITY_RANK.get(self.severity, 0) * self.confidence

    def to_dict(self) -> dict:
        return {
            "cause": self.cause,
            "cause_label": CAUSE_LABELS.get(self.cause, self.cause),
            "severity": self.severity,
            "confidence": round(self.confidence, 2),
            "evidence": self.evidence,
            "affected_chunks": self.affected_chunks,
            "recommendation": self.recommendation,
            "auto_fixable": self.auto_fixable,
        }


@dataclass
class DiagnosticReport:
    job_id: str
    findings: list = field(default_factory=list)
    primary_cause: str | None = None
    overall_severity: str = "ok"
    summary: str = ""
    _finalized: bool = False

    def finalize(self):
        if self._finalized:
            return
        self._finalized = True

        if not self.findings:
            self.primary_cause = None
            self.overall_severity = "ok"
            self.summary = "Không phát hiện nguyên nhân rõ ràng."
            return

        self.findings.sort(key=lambda f: f.weight(), reverse=True)
        top = self.findings[0]
        self.primary_cause = top.cause

        if any(f.severity == "critical" and f.confidence > 0.7 for f in self.findings):
            self.overall_severity = "critical"
        elif any(f.severity in ("critical", "warning") for f in self.findings):
            self.overall_severity = "warning"
        else:
            self.overall_severity = "info"

        label = CAUSE_LABELS.get(top.cause, top.cause)
        self.summary = (
            f"Nguyên nhân chính: {label} "
            f"(độ tin cậy {round(top.confidence * 100)}%). "
            f"{top.recommendation}"
        )

    def to_dict(self) -> dict:
        self.finalize()
        return {
            "job_id": self.job_id,
            "primary_cause": self.primary_cause,
            "primary_cause_label": CAUSE_LABELS.get(self.primary_cause, "") if self.primary_cause else "",
            "overall_severity": self.overall_severity,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
        }


# ─── Helpers ────────────────────────────────────────────────────────────

def _load_chunk_files(job_dir: str) -> list[dict]:
    """Load chunk_XXX_original.txt + _translated.txt pairs from job dir."""
    chunk_dir = os.path.join(job_dir, "chunks")
    if not os.path.isdir(chunk_dir):
        return []
    orig_files = sorted(glob.glob(os.path.join(chunk_dir, "chunk_*_original.txt")))
    result = []
    for orig_path in orig_files:
        m = re.search(r"chunk_(\d+)_original\.txt$", orig_path)
        if not m:
            continue
        idx = int(m.group(1))
        trans_path = orig_path.replace("_original.txt", "_translated.txt")
        try:
            with open(orig_path, encoding="utf-8") as f:
                src = f.read()
            mt = ""
            if os.path.exists(trans_path):
                with open(trans_path, encoding="utf-8") as f:
                    mt = f.read()
            result.append({"index": idx, "src": src, "mt": mt})
        except Exception:
            continue
    return result


_SENTENCE_END_RE = re.compile(r'[.!?。！？]\s*$')
_MATH_PATTERNS_RE = re.compile(
    r'(\$[^$]{3,}\$'
    r'|\\(?:frac|sum|int|alpha|beta|theta|gamma|delta|sigma|lambda|omega|pi|mu|nu|xi|zeta|eta)\b'
    r'|[∀∃∈∉⊆⊇∪∩∧∨¬→↔≤≥≠≈∞√∂∇∑∏∫±×÷]'
    r'|[\u0391-\u03C9]{2,})',
    re.IGNORECASE,
)
_ENGLISH_WORD_RE = re.compile(r'\b[A-Za-z]{4,}\b')
_VIET_CHAR_RE = re.compile(
    r'[àáâãèéêìíòóôõùúýăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỷỹỵ]',
    re.IGNORECASE,
)


def _is_english_heavy(text: str) -> bool:
    """True if text looks like untranslated English (many English words, few Vietnamese chars)."""
    if not text or len(text.strip()) < 20:
        return False
    eng_words = len(_ENGLISH_WORD_RE.findall(text))
    viet_chars = len(_VIET_CHAR_RE.findall(text))
    if eng_words < 5:
        return False
    return viet_chars < eng_words * 0.5


# ─── Phase 1 Detectors ──────────────────────────────────────────────────

def detect_truncated_response(chunks: list[dict]) -> DiagnosticFinding | None:
    """Detect chunks where Gemini cut the response mid-sentence.

    Signals:
    - length ratio (mt/src) < 0.4 AND no sentence-ending punctuation
    - length ratio < 0.25 (extremely short regardless of punctuation)
    """
    if not chunks:
        return None

    affected = []
    evidence = []

    for c in chunks:
        src = c["src"].strip()
        mt = c["mt"].strip()
        if not src or len(src) < 50 or not mt:
            continue

        ratio = len(mt) / max(len(src), 1)
        ends_clean = bool(_SENTENCE_END_RE.search(mt[-30:]))

        if ratio < 0.25 or (ratio < 0.40 and not ends_clean):
            affected.append(c["index"])
            tail = mt[-60:].replace("\n", " ").strip()
            evidence.append(
                f"Chunk {c['index']:03d}: ratio={ratio:.2f}, kết thúc='{tail}'"
            )

    if not affected:
        return None

    confidence = min(0.95, 0.55 + len(affected) * 0.08)
    severity = "critical" if len(affected) >= 3 else "warning"

    return DiagnosticFinding(
        cause="TRUNCATED_RESPONSE",
        severity=severity,
        confidence=confidence,
        evidence=evidence[:8],
        affected_chunks=affected,
        recommendation=CAUSE_RECOMMENDATIONS["TRUNCATED_RESPONSE"],
        auto_fixable=True,
    )


def detect_empty_translations(chunks: list[dict], progress: dict) -> DiagnosticFinding | None:
    """Detect chunks with zero/near-zero translation output, or still in English."""
    affected = []
    evidence = []

    for c in chunks:
        src = c["src"].strip()
        mt = c["mt"].strip()
        if len(src) < 30:
            continue

        if not mt or len(mt) < 10:
            affected.append(c["index"])
            evidence.append(f"Chunk {c['index']:03d}: src={len(src)} chars, bản dịch rỗng")
        elif _is_english_heavy(mt) and not _is_english_heavy(src[:200]):
            affected.append(c["index"])
            evidence.append(f"Chunk {c['index']:03d}: vẫn là tiếng Anh sau khi dịch")

    # Also check failed_chunks recorded by pipeline
    failed = progress.get("failed_chunks", [])
    for fc in failed:
        if fc not in affected:
            affected.append(fc)
            evidence.append(f"Chunk {fc}: đánh dấu failed trong progress.json")

    if not affected:
        return None

    confidence = min(0.98, 0.60 + len(affected) * 0.06)
    severity = "critical" if len(affected) >= 2 else "warning"

    return DiagnosticFinding(
        cause="EMPTY_TRANSLATION",
        severity=severity,
        confidence=confidence,
        evidence=evidence[:8],
        affected_chunks=sorted(set(affected)),
        recommendation=CAUSE_RECOMMENDATIONS["EMPTY_TRANSLATION"],
        auto_fixable=True,
    )


def detect_math_contamination(chunks: list[dict]) -> DiagnosticFinding | None:
    """Detect chunks where math formulas leaked into translatable text blocks."""
    if not chunks:
        return None

    affected = []
    evidence = []

    for c in chunks:
        src = c["src"]
        if not src:
            continue

        matches = _MATH_PATTERNS_RE.findall(src)
        math_chars = sum(len(str(m)) for m in matches)
        ratio = math_chars / max(len(src), 1)

        if ratio > 0.12 and len(matches) >= 3:
            affected.append(c["index"])
            examples = list(dict.fromkeys(str(m) for m in matches))[:4]
            evidence.append(
                f"Chunk {c['index']:03d}: {len(matches)} math patterns "
                f"({ratio:.0%} nội dung). VD: {', '.join(examples)}"
            )

    if not affected:
        return None

    confidence = min(0.90, 0.50 + len(affected) * 0.07)

    return DiagnosticFinding(
        cause="MATH_CONTAMINATION",
        severity="warning",
        confidence=confidence,
        evidence=evidence[:6],
        affected_chunks=affected,
        recommendation=CAUSE_RECOMMENDATIONS["MATH_CONTAMINATION"],
        auto_fixable=False,
    )


def detect_hallucination(chunks: list[dict]) -> DiagnosticFinding | None:
    """Detect chunks where translated text is suspiciously longer than source."""
    if not chunks:
        return None

    affected = []
    evidence = []

    for c in chunks:
        src = c["src"].strip()
        mt = c["mt"].strip()
        if not src or not mt or len(src) < 50:
            continue

        ratio = len(mt) / max(len(src), 1)
        if ratio > 3.0:
            affected.append(c["index"])
            evidence.append(
                f"Chunk {c['index']:03d}: bản dịch dài gấp {ratio:.1f}x bản gốc "
                f"({len(src)} → {len(mt)} ký tự)"
            )

    if not affected:
        return None

    confidence = min(0.85, 0.50 + len(affected) * 0.08)

    return DiagnosticFinding(
        cause="HALLUCINATION",
        severity="warning",
        confidence=confidence,
        evidence=evidence[:6],
        affected_chunks=affected,
        recommendation=CAUSE_RECOMMENDATIONS["HALLUCINATION"],
        auto_fixable=False,
    )


def detect_browser_crash(progress: dict) -> DiagnosticFinding | None:
    """Detect signs of browser crash or pipeline restart during translation."""
    failed = progress.get("failed_chunks", [])
    status = progress.get("status", "")
    retry_count = progress.get("retry_count", 0)
    retried = "retrying" in status or retry_count > 0

    if not failed and not retried:
        return None

    evidence = []
    if failed:
        evidence.append(f"{len(failed)} chunk(s) đánh dấu failed: {list(failed)[:6]}")
    if "retrying" in status:
        evidence.append(f"Pipeline ở trạng thái retry: '{status}'")
    if retry_count > 0:
        evidence.append(f"Đã retry {retry_count} lần")

    confidence = 0.85 if failed else 0.55
    severity = "critical" if len(failed) >= 3 else "warning"

    return DiagnosticFinding(
        cause="BROWSER_CRASH",
        severity=severity,
        confidence=confidence,
        evidence=evidence,
        affected_chunks=list(failed) if isinstance(failed, list) else [],
        recommendation=CAUSE_RECOMMENDATIONS["BROWSER_CRASH"],
        auto_fixable=True,
    )


def detect_chunk_boundary_split(chunks: list[dict]) -> DiagnosticFinding | None:
    """Detect chunks that begin or end mid-sentence (bad chunking)."""
    if not chunks:
        return None

    _STARTS_MID = re.compile(r'^[a-z,;:\-–—(]')
    _ENDS_MID = re.compile(r'[,;:\-–—a-z(]\s*$')

    affected = []
    evidence = []

    for c in chunks:
        src = c["src"].strip()
        if not src or len(src) < 50:
            continue

        issues = []
        first_80 = src[:80].lstrip()
        last_80 = src[-80:].rstrip()

        if _STARTS_MID.match(first_80):
            issues.append(f"bắt đầu bằng '{first_80[:30].strip()}…'")
        if _ENDS_MID.search(last_80) and not _SENTENCE_END_RE.search(last_80):
            issues.append(f"kết thúc bằng '…{last_80[-30:].strip()}'")

        if issues:
            affected.append(c["index"])
            evidence.append(f"Chunk {c['index']:03d}: {'; '.join(issues)}")

    if len(affected) < 2:   # a few splits are normal
        return None

    confidence = min(0.75, 0.40 + len(affected) * 0.05)

    return DiagnosticFinding(
        cause="CHUNK_BOUNDARY_SPLIT",
        severity="info",
        confidence=confidence,
        evidence=evidence[:6],
        affected_chunks=affected,
        recommendation=CAUSE_RECOMMENDATIONS["CHUNK_BOUNDARY_SPLIT"],
        auto_fixable=False,
    )


# ─── Phase 2 Detectors ──────────────────────────────────────────────────

def detect_session_limit(chunks: list[dict]) -> DiagnosticFinding | None:
    """Detect quality degradation over chunk index (Gemini context window limit).

    Uses translation length ratio as a quality proxy, then splits chunks into
    early/mid/late thirds and checks for monotonic decline.

    Requires strict monotonic decline (q1 > q2 > q3), a minimum total drop
    (>12%), and low late-quality (<72%). Acceleration in the second half
    boosts confidence — consistent with LLM context decay.
    """
    # ── Collect (chunk_index, quality_proxy) pairs from length ratio ────────
    scored: list[tuple[int, float]] = []
    if chunks:
        for c in chunks:
            src, mt = c["src"].strip(), c["mt"].strip()
            if src and mt and len(src) > 30:
                ratio = len(mt) / max(len(src), 1)
                proxy = max(0.0, min(1.0, 1.0 - abs(ratio - 1.0) * 0.5))
                scored.append((c["index"], proxy))

    if len(scored) < 6:
        return None

    # ── Split into early / mid / late thirds ────────────────────────────────
    scored_sorted = sorted(scored, key=lambda t: t[0])
    n = len(scored_sorted)
    t1 = n // 3
    t2 = 2 * n // 3

    early = [y for _, y in scored_sorted[:t1]]
    mid   = [y for _, y in scored_sorted[t1:t2]]
    late  = [y for _, y in scored_sorted[t2:]]

    if not early or not mid or not late:
        return None

    q1 = sum(early) / len(early)
    q2 = sum(mid)   / len(mid)
    q3 = sum(late)  / len(late)

    # ── Three required conditions ────────────────────────────────────────────
    # 1. Strict monotonic decline across all three thirds
    monotonic = q1 > q2 > q3
    # 2. Total drop early→late is meaningful (>12 pp)
    total_drop = q1 - q3
    # 3. Late quality is actually bad
    late_bad = q3 < 0.72

    if not (monotonic and total_drop > 0.12 and late_bad):
        return None

    # ── Confidence ──────────────────────────────────────────────────────────
    # Base: 0.55 for meeting all three conditions
    # +bonus if decline accelerates in second half (pattern of context saturation)
    drop_first_half  = q1 - q2   # early → mid
    drop_second_half = q2 - q3   # mid   → late
    accelerating = drop_second_half > drop_first_half
    acceleration_bonus = 0.15 if accelerating else 0.0

    # +bonus for larger total drop and more data points
    drop_bonus = min(0.15, (total_drop - 0.12) * 1.5)
    size_bonus  = min(0.10, (n - 6) * 0.01)

    confidence = min(0.90, 0.55 + acceleration_bonus + drop_bonus + size_bonus)

    # ── Evidence ─────────────────────────────────────────────────────────────
    bad_chunks = [x for x, y in scored_sorted if y < 0.65]
    evidence = [
        f"Điểm trung bình: đầu {q1:.0%} → giữa {q2:.0%} → cuối {q3:.0%}",
        f"Tổng mức giảm: {total_drop:.0%} (ngưỡng: 12%)",
    ]
    if accelerating:
        evidence.append(
            f"Tốc độ giảm tăng dần: nửa đầu -{drop_first_half:.0%}, nửa sau -{drop_second_half:.0%}"
            " — đặc trưng của context limit"
        )
    if bad_chunks:
        evidence.append(f"Chunks chất lượng thấp (<65%): {bad_chunks[:8]}")
    evidence.append(f"Số điểm dữ liệu: {n} chunks")

    return DiagnosticFinding(
        cause="SESSION_LIMIT",
        severity="warning",
        confidence=confidence,
        evidence=evidence,
        affected_chunks=bad_chunks,
        recommendation=CAUSE_RECOMMENDATIONS["SESSION_LIMIT"],
        auto_fixable=False,
    )


def detect_glossary_drift(chunks: list[dict], progress: dict) -> DiagnosticFinding | None:
    """Detect the same EN term being left untranslated across multiple chunks."""
    glossary = progress.get("glossary", {}).get("terms", {})
    if not glossary or not chunks:
        return None

    en_terms = {k.lower(): v for k, v in glossary.items() if v}
    violations: dict[str, list] = {}

    for c in chunks:
        src_lower = c["src"].lower()
        mt_lower = c["mt"].lower()
        if not c["mt"]:
            continue

        for en_term, vi_term in en_terms.items():
            if en_term not in src_lower:
                continue
            # Violation: English term present in translation (untranslated) but VI term absent
            if en_term in mt_lower and vi_term.lower() not in mt_lower:
                violations.setdefault(en_term, []).append(c["index"])

    # Only flag if same violation appears in ≥ 2 chunks
    significant = {k: v for k, v in violations.items() if len(v) >= 2}
    if not significant:
        return None

    evidence = []
    affected = []
    for term, idxs in list(significant.items())[:5]:
        vi_expected = glossary.get(term, "?")
        evidence.append(
            f"'{term}' → '{vi_expected}': không nhất quán ở chunks {idxs[:4]}"
        )
        affected.extend(idxs)

    affected = sorted(set(affected))
    confidence = min(0.85, 0.55 + len(significant) * 0.06)

    return DiagnosticFinding(
        cause="GLOSSARY_DRIFT",
        severity="info",
        confidence=confidence,
        evidence=evidence,
        affected_chunks=affected,
        recommendation=CAUSE_RECOMMENDATIONS["GLOSSARY_DRIFT"],
        auto_fixable=False,
    )


# ─── Main entry point ───────────────────────────────────────────────────

def run_diagnostics(job_id: str, job_dir: str, progress: dict) -> DiagnosticReport:
    """Run all detectors and return a DiagnosticReport."""
    report = DiagnosticReport(job_id=job_id)

    chunks = _load_chunk_files(job_dir)

    findings = [
        detect_truncated_response(chunks),
        detect_empty_translations(chunks, progress),
        detect_math_contamination(chunks),
        detect_hallucination(chunks),
        detect_browser_crash(progress),
        detect_chunk_boundary_split(chunks),
        detect_session_limit(chunks),
        detect_glossary_drift(chunks, progress),
    ]

    report.findings = [f for f in findings if f is not None]
    report.finalize()
    return report
