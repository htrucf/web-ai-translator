"""ExtractorAgent — Tác tử trích xuất text blocks từ PDF.

Vai trò (con người tương ứng): nhân viên scan/sao chụp tài liệu, phụ trách
giai đoạn 1 trong quy trình dịch chuyên nghiệp.

Wrap `processor.extract_text_blocks` + `processor.get_pdf_info` thành agent
chuẩn theo `BaseAgent` interface — không thay đổi logic gốc, chỉ chuẩn hóa
input/output để Coordinator orchestrate đồng nhất với các agent khác.

Ghi state:
  - ctx.blocks            ← list[TextBlock]
  - ctx.progress["title"], ["page_count"], ["total_chars"]
"""

from __future__ import annotations

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.processor import extract_text_blocks, get_pdf_info


class ExtractorAgent(BaseAgent):
    """Agent đầu tiên trong pipeline — đọc PDF và phân loại block.

    Tách block thành 3 nhóm (xử lý sau):
      - text translatable → đưa vào TranslatorAgent
      - math/figure       → giữ nguyên ở rebuild
      - header/footer     → bỏ qua
    """

    name = "ExtractorAgent"

    async def run(self, ctx: AgentContext) -> AgentResult:
        if ctx.is_cancelled():
            return AgentResult.fail("Cancelled before extraction", recoverable=True)

        self.log(f"Extracting blocks from {ctx.pdf_path}")
        try:
            blocks = extract_text_blocks(ctx.pdf_path)
        except Exception as e:
            return AgentResult.fail(
                f"PDF extraction failed: {e}", recoverable=False
            )

        if not blocks:
            return AgentResult.fail("No blocks extracted from PDF", recoverable=False)

        translatable = sum(1 for b in blocks if b.is_translatable)
        math_blocks = sum(1 for b in blocks if b.is_math)
        if translatable == 0:
            return AgentResult.fail(
                "No translatable text found in PDF", recoverable=False
            )

        ctx.blocks = blocks

        if "title" not in ctx.progress or "page_count" not in ctx.progress:
            try:
                info = get_pdf_info(ctx.pdf_path)
                ctx.progress["title"] = info.get("title", "")
                ctx.progress["page_count"] = info.get("page_count", 0)
                ctx.progress["total_chars"] = info.get("total_chars", 0)
                ctx.save_progress()
            except Exception as e:
                self.log(f"get_pdf_info failed (non-fatal): {e}", "warn")

        self.log(
            f"Extracted {len(blocks)} blocks "
            f"(translatable={translatable}, math={math_blocks})"
        )

        return AgentResult.ok(
            data={"block_count": len(blocks)},
            translatable=translatable,
            math_blocks=math_blocks,
            page_count=ctx.progress.get("page_count", 0),
        )
