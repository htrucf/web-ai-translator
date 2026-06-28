"""LatexEvalCodec — adapter eval-loop cho chunk LaTeX (chuỗi .tex).

Chunk LaTeX là CHUỖI bất biến → bọc trong holder mutable ``LatexUnit`` để eval-loop
ghi bản dịch vào. Mỗi unit = 1 "đoạn" .tex; KHÔNG dùng đánh số `[N]` (dịch nguyên
đoạn). `evaluate` chỉ dùng LocalJudge (lỗi cấu trúc) — glossary/thuật ngữ của LaTeX
được pipeline LaTeX lo riêng (translator.glossary).

LƯU Ý FIT: pipeline LaTeX (services/pipeline.py) có xử lý per-chunk tinh vi (strip
comment, dịch inline cho chunk structural, glossary-aware translate, input files).
Eval-loop dùng codec này KHÔNG tái hiện các tối ưu đó → chỉ nên dùng như đường
opt-in, không thay mặc định luồng arXiv. Đây là NỀN; việc nối là quyết định riêng.
"""

from __future__ import annotations

from types import SimpleNamespace


class LatexUnit:
    """Holder mutable cho 1 chunk .tex (vì chuỗi bất biến không ghi lại được)."""
    __slots__ = ("source", "translated_text")

    def __init__(self, source: str):
        self.source = source
        self.translated_text = ""


class LatexEvalCodec:
    def __init__(self):
        from app.pdf.agents.local_judge_agent import LocalJudgeAgent
        self._local = LocalJudgeAgent()

    def to_source_text(self, unit: LatexUnit) -> str:
        return unit.source

    def to_translation_text(self, unit: LatexUnit) -> str:
        return unit.translated_text or ""

    def apply(self, text: str, unit: LatexUnit) -> None:
        unit.translated_text = self.extract(text)

    def evaluate(self, unit: LatexUnit, glossary) -> tuple[float, list]:
        blk = SimpleNamespace(
            text=unit.source, translated_text=(unit.translated_text or ""),
            is_translatable=True, page_num=0, block_idx=0,
        )
        return self._local.judge_chunk([blk], glossary)

    # ── generic translate support ──────────────────────────────────────────────

    def translate_prompt(self, source_text: str) -> str:
        return (
            "Dịch đoạn LaTeX sau sang tiếng Việt. GIỮ NGUYÊN 100% mọi lệnh \\..., "
            "môi trường \\begin{}/\\end{}, công thức toán, nhãn \\label/\\ref, "
            "citations. CHỈ dịch phần văn bản tiếng Anh. KHÔNG thêm giải thích.\n\n"
            f"{source_text}"
        )

    def extract(self, raw: str) -> str:
        s = (raw or "").strip()
        if s.startswith("```"):
            lines = s.splitlines()
            if len(lines) >= 2 and lines[-1].lstrip().startswith("```"):
                s = "\n".join(lines[1:-1]).strip()
        return s
