"""eval_codec.py — Adapter ĐỊNH DẠNG cho vòng khép kín (EvalCodec).

`EvalPipeline` (scheduler trong eval_pipeline.py) đã ĐỘC LẬP định dạng — nó chỉ
nhận các callable thuần. Phần CÒN gắn với định dạng nằm ở 4 thao tác trên một
"chunk": render thành text đánh số `[N]`, áp bản dịch ngược lại, và chấm chất
lượng / sinh error-list. Gom 4 thao tác đó vào một **codec** để mỗi định dạng
(PDF / Office docx / LaTeX) cắm vào CÙNG một lõi eval-loop.

Hợp đồng (Protocol) — xem `EvalCodec`. Hiện có `PdfEvalCodec` cho chunk dạng
``list[TextBlock]`` (PDF; Office cũng block-based nên dùng lại được). LaTeX
(chunk là chuỗi) sẽ có codec riêng bọc mỗi chunk thành 1 block ảo.

Module này CHƯA đổi hành vi đang chạy — là nền để parametrize `run_eval_loop`
(PDF codec làm mặc định) rồi nối Office/LaTeX ở bước sau.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EvalCodec(Protocol):
    """Bộ thao tác phụ-thuộc-định-dạng mà lõi eval-loop cần trên một chunk."""

    def to_source_text(self, chunk) -> str:
        """Render chunk thành text nguồn đánh số `[N]` (cho prompt dịch + judge)."""
        ...

    def to_translation_text(self, chunk) -> str:
        """Render bản dịch HIỆN TẠI của chunk thành text `[N]` (cho refine)."""
        ...

    def apply(self, text: str, chunk) -> None:
        """Parse text `[N]` đã dịch → gán ngược vào chunk (in-place)."""
        ...

    def evaluate(self, chunk, glossary) -> tuple[float, list]:
        """Chấm chunk → (điểm 0..100 cho Gate 1, error-list cho Critic-hub)."""
        ...


class PdfEvalCodec:
    """Codec cho chunk = ``list[TextBlock]`` (PDF, và Office vốn cũng block-based).

    Bọc lại các primitive sẵn có: `chunk_to_text`, `parse_translated_chunk`,
    `LocalJudgeAgent` (lỗi cấu trúc + điểm), `GlossaryJudgeAgent` (lỗi thuật ngữ).
    """

    def __init__(self):
        from app.pdf.agents.local_judge_agent import LocalJudgeAgent
        from app.pdf.agents.glossary_judge_agent import GlossaryJudgeAgent
        self._local = LocalJudgeAgent()
        self._gloss = GlossaryJudgeAgent()

    def to_source_text(self, chunk) -> str:
        from app.pdf.processor import chunk_to_text
        return chunk_to_text(chunk)

    def to_translation_text(self, chunk) -> str:
        from app.pdf.agents.critic_agent import CriticAgent
        return CriticAgent._blocks_to_numbered(chunk, "translation")

    def apply(self, text: str, chunk) -> None:
        from app.pdf.processor import parse_translated_chunk
        try:
            parse_translated_chunk(text, chunk)
        except Exception:
            pass

    def evaluate(self, chunk, glossary) -> tuple[float, list]:
        score, struct_errors = self._local.judge_chunk(chunk, glossary)
        term_errors = self._gloss.judge_chunk_local(chunk, glossary)
        return score, struct_errors + term_errors
