"""OfficeEvalCodec — adapter cho vòng khép kín (eval-loop) cho .docx.

Office vốn đã block-based + dùng format `[N]` (xem `office/_common.py`), nên codec
chỉ bọc lại các hàm sẵn có. Block office là duck-typed (`.text`, `.translated_text`)
— KHÔNG có `.is_translatable`/`.page_num`/`.block_idx` mà LocalJudge/Quality cần,
nên `evaluate` bọc tạm mỗi block thành SimpleNamespace đủ thuộc tính để chấm.

Khác PdfEvalCodec: `apply` GHI ĐÈ (eval-loop dịch lại nhiều lần) thay vì giữ bản
cũ như `parse_numbered_response` (vốn resume-safe, không ghi đè).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from app.office._common import (
    chunk_to_numbered_text,
    build_translation_prompt,
    clean_response,
    strip_inline_tags,
)

_NUM_RE = re.compile(r"\[(\d+)\]\s*(.*?)(?=\n\[\d+\]|\Z)", re.DOTALL)


class OfficeEvalCodec:
    def __init__(self):
        from app.pdf.agents.local_judge_agent import LocalJudgeAgent
        from app.pdf.agents.glossary_judge_agent import GlossaryJudgeAgent
        self._local = LocalJudgeAgent()
        self._gloss = GlossaryJudgeAgent()

    # ── render / apply ────────────────────────────────────────────────────────

    def to_source_text(self, chunk) -> str:
        return chunk_to_numbered_text(chunk)

    def to_translation_text(self, chunk) -> str:
        parts = []
        for i, b in enumerate(chunk):
            t = " ".join(
                (getattr(b, "translated_text", "") or getattr(b, "text", "") or "").split()
            )
            parts.append(f"[{i + 1}] {t}")
        return "\n\n".join(parts)

    def apply(self, text: str, chunk) -> None:
        """GHI ĐÈ translated_text từ text `[N]` (eval-loop dịch lại nhiều lần)."""
        for num_str, t in _NUM_RE.findall(text or ""):
            try:
                idx = int(num_str) - 1
            except ValueError:
                continue
            if 0 <= idx < len(chunk):
                tr = t.strip()
                if tr:
                    chunk[idx].translated_text = tr

    def evaluate(self, chunk, glossary) -> tuple[float, list]:
        wrapped = [
            SimpleNamespace(
                text=strip_inline_tags(getattr(b, "text", "") or ""),
                translated_text=strip_inline_tags(getattr(b, "translated_text", "") or ""),
                is_translatable=True, page_num=0, block_idx=i,
            )
            for i, b in enumerate(chunk)
        ]
        score, struct_errors = self._local.judge_chunk(wrapped, glossary)
        term_errors = self._gloss.judge_chunk_local(wrapped, glossary)
        return score, struct_errors + term_errors

    # ── generic translate support (cho make_generic_translate_factory) ──────────

    def translate_prompt(self, source_text: str) -> str:
        return build_translation_prompt(source_text)

    def extract(self, raw: str) -> str:
        return clean_response(raw)
