"""Critic module — đánh giá chất lượng từng block dịch và sinh error list cụ thể.

Hai tầng:
1. HeuristicCritic — không cần AI, chạy nhanh, luôn available
2. LLMCritic — dùng Ollama, cho error chi tiết hơn (optional)

Output là list[CriticError] per block — được inject vào prompt Refiner để sửa đúng chỗ.
Đây là mắt xích còn thiếu trong vòng lặp Translator → Critic → Refiner.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class CriticError:
    """Một lỗi cụ thể trong bản dịch của một block."""
    category: str        # accuracy | fluency | terminology | completeness | numbers
    severity: str        # minor | major | critical
    description: str     # mô tả lỗi bằng tiếng Việt
    source_span: str = ""    # đoạn gốc bị lỗi (nếu xác định được)
    translation_span: str = ""  # đoạn dịch bị lỗi (nếu xác định được)
    suggestion: str = ""     # gợi ý sửa cụ thể

    def format_for_prompt(self) -> str:
        """Format để inject vào prompt Refiner."""
        parts = [f"- [{self.severity.upper()}] {self.category}: {self.description}"]
        if self.source_span:
            parts.append(f"  Gốc: \"{self.source_span}\"")
        if self.translation_span:
            parts.append(f"  Dịch sai: \"{self.translation_span}\"")
        if self.suggestion:
            parts.append(f"  Gợi ý: {self.suggestion}")
        return "\n".join(parts)


@dataclass
class BlockCritique:
    """Kết quả critique cho một block."""
    block_id: int                          # index trong mini-chunk
    original: str
    translation: str
    errors: list[CriticError] = field(default_factory=list)
    overall_severity: str = "ok"           # ok | minor | major | critical

    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def format_errors_for_prompt(self) -> str:
        if not self.errors:
            return ""
        return "\n".join(e.format_for_prompt() for e in self.errors)

    def compute_severity(self):
        if not self.errors:
            self.overall_severity = "ok"
            return
        order = {"critical": 3, "major": 2, "minor": 1}
        worst = max(self.errors, key=lambda e: order.get(e.severity, 0))
        self.overall_severity = worst.severity


# ─── Vietnamese detection ─────────────────────────────────────────────────────

_VI_RE = re.compile(
    r'[àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợ'
    r'ùúủũụưứừửữựỳýỷỹỵđÀÁẢÃẠĂẮẰẲẴẶÂẤẦẨẪẬÈÉẺẼẸÊẾỀỂỄỆ'
    r'ÌÍỈĨỊÒÓỎÕỌÔỐỒỔỖỘƠỚỜỞỠỢÙÚỦŨỤƯỨỪỬỮỰỲÝỶỸỴĐ]'
)
_EN_WORD_RE = re.compile(r'\b[a-zA-Z]{3,}\b')


def _vi_ratio(text: str) -> float:
    if not text:
        return 0.0
    vi_chars = len(_VI_RE.findall(text))
    alpha = sum(1 for c in text if c.isalpha())
    return vi_chars / max(alpha, 1)


def _en_word_ratio(text: str) -> float:
    words = text.split()
    if not words:
        return 0.0
    en = len(_EN_WORD_RE.findall(text))
    return en / max(len(words), 1)


def _extract_numbers(text: str) -> set[str]:
    """Trích xuất tất cả số có nghĩa (bỏ số đơn 0-9 lẻ)."""
    return set(re.findall(r'\b\d+(?:[.,]\d+)*\b', text))


# ─── Heuristic Critic ─────────────────────────────────────────────────────────

class HeuristicCritic:
    """Critic không dùng AI — phát hiện lỗi bằng rule-based checks.

    Chạy nhanh, không cần Ollama, luôn available. Phát hiện được:
    - Block không được dịch (vẫn tiếng Anh)
    - Bản dịch quá ngắn so với gốc (truncation)
    - Bản dịch quá dài (hallucination)
    - Số liệu bị mất
    - Thuật ngữ không theo glossary
    """

    def critique(
        self,
        original: str,
        translation: str,
        glossary: dict[str, str] | None = None,
        block_id: int = 0,
    ) -> BlockCritique:
        result = BlockCritique(
            block_id=block_id,
            original=original,
            translation=translation,
        )

        if not original.strip():
            return result

        if not translation.strip():
            result.errors.append(CriticError(
                category="completeness",
                severity="critical",
                description="Bản dịch rỗng — block chưa được dịch",
                source_span=original[:100],
                suggestion="Dịch toàn bộ đoạn văn này sang tiếng Việt",
            ))
            result.compute_severity()
            return result

        orig = original.strip()
        trans = translation.strip()

        # ── 1. Untranslated detection ──────────────────────────────────────
        vi = _vi_ratio(trans)
        en = _en_word_ratio(trans)
        orig_en = _en_word_ratio(orig)

        if vi < 0.05 and en > 0.5 and orig_en > 0.4 and len(orig.split()) >= 8:
            # Không có ký tự tiếng Việt, còn nhiều từ tiếng Anh
            result.errors.append(CriticError(
                category="accuracy",
                severity="critical",
                description="Block chưa được dịch — vẫn là tiếng Anh",
                source_span=orig[:80],
                translation_span=trans[:80],
                suggestion="Dịch toàn bộ nội dung này sang tiếng Việt",
            ))

        # ── 2. Length ratio ────────────────────────────────────────────────
        ratio = len(trans) / max(len(orig), 1)

        if ratio < 0.3 and len(orig) > 50:
            result.errors.append(CriticError(
                category="completeness",
                severity="major",
                description=f"Bản dịch quá ngắn ({len(trans)} ký tự so với gốc {len(orig)} ký tự, tỉ lệ {ratio:.0%})",
                suggestion="Dịch đầy đủ toàn bộ nội dung, không bỏ sót câu nào",
            ))
        elif ratio > 3.5 and len(orig) > 30:
            result.errors.append(CriticError(
                category="fluency",
                severity="major",
                description=f"Bản dịch quá dài ({ratio:.1f}× gốc) — có thể thêm nội dung không có trong gốc",
                suggestion="Chỉ dịch nội dung có trong bản gốc, không thêm giải thích hay bình luận",
            ))

        # ── 3. Numbers preservation ────────────────────────────────────────
        orig_nums = _extract_numbers(orig)
        trans_nums = _extract_numbers(trans)
        missing_nums = orig_nums - trans_nums

        # Filter ra số thực sự quan trọng (không phải 1-9 lẻ)
        important_missing = {n for n in missing_nums if len(n) > 1 or '.' in n or ',' in n}
        if important_missing:
            sample = ", ".join(sorted(important_missing)[:5])
            result.errors.append(CriticError(
                category="accuracy",
                severity="minor",
                description=f"Số liệu bị mất trong bản dịch: {sample}",
                suggestion=f"Giữ nguyên các số liệu: {sample}",
            ))

        # ── 4. Glossary compliance ─────────────────────────────────────────
        if glossary:
            violations = []
            orig_lower = orig.lower()
            trans_lower = trans.lower()
            for en_term, vi_term in glossary.items():
                en_lower = en_term.lower()
                if en_lower in orig_lower and vi_term.lower() not in trans_lower:
                    violations.append((en_term, vi_term))
            if violations:
                v_sample = "; ".join(f'"{e}" → "{v}"' for e, v in violations[:3])
                result.errors.append(CriticError(
                    category="terminology",
                    severity="minor",
                    description=f"Thuật ngữ không theo glossary: {v_sample}",
                    suggestion=f"Dùng đúng thuật ngữ đã quy định: {v_sample}",
                ))

        result.compute_severity()
        return result


# ─── LLM Critic ───────────────────────────────────────────────────────────────

class LLMCritic:
    """Critic dùng Ollama để phát hiện lỗi sâu hơn: accuracy, fluency, style.

    Chỉ chạy khi Ollama available và block vượt ngưỡng độ dài tối thiểu.
    Fallback về HeuristicCritic nếu Ollama không phản hồi.
    """

    def __init__(self, model: str = "qwen2.5:7b", timeout: float = 60.0):
        self.model = model
        self.timeout = timeout
        self._heuristic = HeuristicCritic()

    def _build_critic_prompt(self, original: str, translation: str) -> str:
        return (
            "Bạn là chuyên gia đánh giá chất lượng dịch thuật học thuật Anh-Việt.\n\n"
            "Phân tích bản dịch sau và liệt kê CỤ THỂ các lỗi cần sửa.\n\n"
            "=== VĂN BẢN GỐC (EN) ===\n"
            f"{original}\n\n"
            "=== BẢN DỊCH (VI) ===\n"
            f"{translation}\n\n"
            "=== YÊU CẦU ===\n"
            "Trả về JSON (KHÔNG có text ngoài JSON):\n"
            "{\n"
            '  "errors": [\n'
            "    {\n"
            '      "category": "<accuracy|fluency|terminology|completeness|numbers>",\n'
            '      "severity": "<minor|major|critical>",\n'
            '      "source_span": "<đoạn gốc bị lỗi hoặc null>",\n'
            '      "translation_span": "<đoạn dịch sai hoặc null>",\n'
            '      "description": "<mô tả lỗi ngắn gọn tiếng Việt>",\n'
            '      "suggestion": "<cách sửa cụ thể>"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Nếu bản dịch tốt, trả về: {\"errors\": []}\n"
            "CHỈ liệt kê lỗi thực sự nghiêm trọng (major/critical), bỏ qua lỗi nhỏ."
        )

    def _parse_errors(self, raw: str) -> list[CriticError]:
        """Parse JSON response từ LLM thành list CriticError."""
        import json

        # Tìm JSON block trong response
        match = re.search(r'\{[\s\S]*\}', raw)
        if not match:
            return []
        try:
            data = json.loads(match.group())
        except Exception:
            return []

        errors = []
        for e in data.get("errors", []):
            if not isinstance(e, dict):
                continue
            errors.append(CriticError(
                category=e.get("category", "accuracy"),
                severity=e.get("severity", "minor"),
                description=e.get("description", ""),
                source_span=e.get("source_span") or "",
                translation_span=e.get("translation_span") or "",
                suggestion=e.get("suggestion") or "",
            ))
        return errors

    def critique(
        self,
        original: str,
        translation: str,
        glossary: dict[str, str] | None = None,
        block_id: int = 0,
    ) -> BlockCritique:
        # Chạy heuristic trước — luôn có kết quả
        heuristic_result = self._heuristic.critique(original, translation, glossary, block_id)

        # Không gọi LLM nếu block quá ngắn (< 40 từ) hoặc đã có lỗi critical từ heuristic
        if len(original.split()) < 40 or heuristic_result.overall_severity == "critical":
            return heuristic_result

        # Gọi Ollama
        try:
            import httpx
            prompt = self._build_critic_prompt(original, translation)
            r = httpx.post(
                f"{os.getenv('OLLAMA_URL', 'http://localhost:11434')}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 1024},
                },
                timeout=self.timeout,
            )
            if not r.is_success:
                return heuristic_result

            raw = r.json().get("response", "")
            llm_errors = self._parse_errors(raw)

            # Merge: LLM errors + heuristic errors (dedup by category+severity)
            merged = list(heuristic_result.errors)
            existing_keys = {(e.category, e.severity) for e in merged}
            for le in llm_errors:
                key = (le.category, le.severity)
                if key not in existing_keys:
                    merged.append(le)
                    existing_keys.add(key)

            result = BlockCritique(
                block_id=block_id,
                original=original,
                translation=translation,
                errors=merged,
            )
            result.compute_severity()
            return result

        except Exception:
            # Ollama unavailable — fallback về heuristic
            return heuristic_result


# ─── Public API ───────────────────────────────────────────────────────────────

def critique_blocks(
    blocks: list,
    glossary: dict[str, str] | None = None,
    use_llm: bool = False,
    llm_model: str = "qwen2.5:7b",
) -> dict[int, BlockCritique]:
    """Chạy critic trên list blocks, trả về dict {block_index: BlockCritique}.

    Args:
        blocks: list TextBlock (có .text và .translated_text)
        glossary: bảng thuật ngữ hiện tại
        use_llm: True để dùng LLMCritic (cần Ollama), False = chỉ heuristic
        llm_model: model Ollama nếu use_llm=True

    Returns:
        dict mapping block index → BlockCritique (chỉ chứa blocks có lỗi)
    """
    critic = LLMCritic(model=llm_model) if use_llm else HeuristicCritic()
    results: dict[int, BlockCritique] = {}

    for idx, block in enumerate(blocks):
        original = (block.text or "").strip()
        translation = (block.translated_text or "").strip()

        if not original or not block.is_translatable:
            continue

        result = critic.critique(original, translation, glossary, block_id=idx)
        if result.has_errors():
            results[idx] = result

    return results


def format_critique_for_prompt(critiques: dict[int, BlockCritique]) -> str:
    """Format toàn bộ critique thành text để inject vào prompt Refiner.

    Chỉ dùng khi có nhiều blocks trong 1 mini-chunk.
    Với 1 block đơn, dùng BlockCritique.format_errors_for_prompt() trực tiếp.
    """
    if not critiques:
        return ""
    lines = []
    for block_id, critique in sorted(critiques.items()):
        if critique.has_errors():
            lines.append(f"Block [{block_id + 1}]:")
            lines.append(critique.format_errors_for_prompt())
    return "\n".join(lines)
