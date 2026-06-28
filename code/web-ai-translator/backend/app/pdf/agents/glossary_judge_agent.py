"""GlossaryJudgeAgent — Soát thuật ngữ: nhất quán + phát hiện thuật ngữ mới. Chạy GỘP batch.

Vai trò (con người tương ứng): chuyên viên thuật ngữ — không soát toàn bộ câu chữ
mà tập trung hai việc: (1) thuật ngữ có dịch đúng/nhất quán với glossary không
(``lỗi dịch sai thuật ngữ``); (2) có thuật ngữ chuyên ngành MỚI nào nên lưu lại.
Vì là việc *xuyên đoạn*, chạy GỘP nhiều chunk một lượt (mặc định ~4-5) cho tiết kiệm.

Hai phần:
  - ``judge_chunk_local`` — kiểm tra tuân thủ glossary per-chunk (local, không AI),
    tái dùng HeuristicCritic lọc category ``terminology``.
  - ``extract_new_terms_batch`` — gửi 1 prompt cho cả batch để moi thuật ngữ mới
    (cần ``translator``+``page``; trả dict EN→VI để merge vào glossary).
"""

from __future__ import annotations

import re

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.critic import critique_blocks
from app.pdf.glossary import parse_extraction_response
from app.pdf.processor import chunk_to_text


# Số chunk gộp mỗi lượt judge thuật ngữ (token-efficient)
DEFAULT_BATCH_SIZE = 5


class GlossaryJudgeAgent(BaseAgent):
    name = "GlossaryJudgeAgent"

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE):
        self.batch_size = batch_size

    # ── Local: lỗi tuân thủ thuật ngữ (per-chunk, không AI) ────────────────────

    def judge_chunk_local(self, chunk: list, glossary: dict | None) -> list:
        """Trả list[CriticError] thuộc category ``terminology`` cho 1 chunk."""
        if not glossary:
            return []
        critiques = critique_blocks(chunk, glossary, use_llm=False)
        errors = []
        for crit in critiques.values():
            errors.extend(e for e in crit.errors if e.category == "terminology")
        return errors

    # ── Batched: moi thuật ngữ mới qua AI cho nhiều chunk một lượt ──────────────

    @staticmethod
    def build_batch_newterm_prompt(batch: list[tuple[int, str, str]]) -> str:
        """Gộp nhiều (index, NGUỒN-EN, DỊCH-VI) vào 1 prompt tìm thuật ngữ mới."""
        lines = [
            "So sánh từng cặp (NGUỒN, DỊCH) dưới đây, liệt kê thuật ngữ chuyên ngành "
            "MỚI được dịch (chưa có trong bảng thuật ngữ).",
            "Chỉ thuật ngữ chuyên ngành, KHÔNG liệt kê từ thông dụng.",
            "Mỗi dòng: English term → Thuật ngữ tiếng Việt. Không thêm chữ nào khác.",
            "Trả về DUY NHẤT trong một block ```text ... ```.",
            "",
        ]
        for idx, src, mt in batch:
            lines.append(f"[đoạn {idx}]")
            lines.append(f"NGUỒN: {src[:1500]}")
            lines.append(f"DỊCH: {mt[:1500]}")
            lines.append("")
        return "\n".join(lines)

    async def extract_new_terms_batch(
        self, batch: list[tuple[int, str, str]], translator, page
    ) -> dict[str, str]:
        """Gửi 1 prompt cho cả batch → dict thuật ngữ mới EN(lower)→VI."""
        if not batch:
            return {}
        prompt = self.build_batch_newterm_prompt(batch)
        try:
            raw = await translator._send_prompt_and_get_response(page, prompt)
        except Exception as e:
            self.log(f"new-term batch failed: {e}", "warn")
            return {}
        return parse_extraction_response(raw)

    # ── Run: 1 pass local trên toàn bộ chunk (báo cáo) ─────────────────────────

    async def run(self, ctx: AgentContext) -> AgentResult:
        if not ctx.chunks:
            return AgentResult.fail("No chunks in context", recoverable=False)
        glossary = ctx.glossary if ctx.glossary_enabled else None
        if not glossary:
            self.log("No glossary → skip terminology compliance")
            return AgentResult.ok(data={"violations": 0}, violation_count=0)

        total = 0
        bad_chunks: list[int] = []
        for i, chunk in enumerate(ctx.chunks):
            errs = self.judge_chunk_local(chunk, glossary)
            if errs:
                total += len(errs)
                bad_chunks.append(i)
        self.log(f"GlossaryJudge: {total} terminology violations in {len(bad_chunks)} chunks")
        return AgentResult.ok(
            data={"violations": total, "bad_chunks": bad_chunks},
            violation_count=total,
        )
