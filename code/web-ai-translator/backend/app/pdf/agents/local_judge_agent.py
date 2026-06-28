"""LocalJudgeAgent — Soát lỗi đơn giản theo cấu trúc, chấm MỖI chunk (local, không AI).

Vai trò (con người tương ứng): QC viên tầng 1 — đọc nhanh từng đoạn, bắt các lỗi
hiển nhiên (đoạn còn tiếng Anh, độ dài bất thường, mất số liệu) mà không cần
chuyên môn sâu và không gọi AI. Vì rẻ nên chấm TỪNG chunk.

Tách vai với GlossaryJudgeAgent: LocalJudge lo lỗi *cấu trúc* (mọi category trừ
``terminology``); GlossaryJudge lo riêng *thuật ngữ*. Cả hai tái dùng HeuristicCritic
nên không trùng tính toán — chỉ lọc theo category.

Dùng trong vòng khép kín như "Gate 1": trả (điểm, error-list) cho Critic gom.
"""

from __future__ import annotations

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.critic import critique_blocks
from app.pdf.quality import check_translation_quality


class LocalJudgeAgent(BaseAgent):
    name = "LocalJudgeAgent"

    def judge_chunk(self, chunk: list, glossary: dict | None = None) -> tuple[float, list]:
        """Chấm 1 chunk (local). Trả (điểm 0..100, list[CriticError] lỗi cấu trúc).

        ``chunk`` là list[TextBlock] đã có ``translated_text``. Không gọi AI.
        """
        score = check_translation_quality(chunk, glossary or None).score
        critiques = critique_blocks(chunk, glossary or None, use_llm=False)
        errors = []
        for crit in critiques.values():
            errors.extend(e for e in crit.errors if e.category != "terminology")
        return score, errors

    async def run(self, ctx: AgentContext) -> AgentResult:
        """Soát toàn bộ chunk một lượt (báo cáo) — dùng khi muốn 1 pass local độc lập."""
        if not ctx.chunks:
            return AgentResult.fail("No chunks in context", recoverable=False)

        glossary = ctx.glossary if ctx.glossary_enabled else None
        total_errors = 0
        low_chunks: list[int] = []
        for i, chunk in enumerate(ctx.chunks):
            score, errors = self.judge_chunk(chunk, glossary)
            total_errors += len(errors)
            if score < 60:
                low_chunks.append(i)

        self.log(
            f"LocalJudge: {total_errors} structural errors, "
            f"{len(low_chunks)} low-score chunks"
        )
        return AgentResult.ok(
            data={"structural_errors": total_errors, "low_chunks": low_chunks},
            error_count=total_errors,
            low_count=len(low_chunks),
        )
