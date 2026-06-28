"""RebuilderAgent — Wrap PDF rebuild thành agent chuẩn.

Vai trò (con người tương ứng): "thợ dàn trang" — sau khi bản dịch sạch,
người này dàn lại bản dịch lên đúng bố cục gốc: giữ font, vị trí, cột,
hình. Khác biệt: không cần dịch nội dung, chỉ cần khớp layout.

Wrap `processor.rebuild_pdf_inplace` → tạo file `output/translated.pdf`.
"""

from __future__ import annotations

import os

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.processor import rebuild_pdf_inplace


class RebuilderAgent(BaseAgent):
    name = "RebuilderAgent"

    async def run(self, ctx: AgentContext) -> AgentResult:
        if ctx.is_cancelled():
            return AgentResult.fail("Cancelled", recoverable=True)

        if not ctx.blocks:
            return AgentResult.fail("No blocks in context", recoverable=False)

        output_dir = os.path.join(ctx.job_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "translated.pdf")

        self.log(f"Rebuilding PDF → {output_path}")
        try:
            rebuild_pdf_inplace(ctx.pdf_path, ctx.blocks, output_path)
        except Exception as e:
            return AgentResult.fail(
                f"PDF rebuild failed: {e}", recoverable=False
            )

        if not os.path.exists(output_path):
            return AgentResult.fail(
                "Rebuild ran but output file not found", recoverable=False
            )

        size = os.path.getsize(output_path)
        ctx.progress["output_path"] = output_path
        ctx.save_progress()
        self.log(f"Output: {output_path} ({size:,} bytes)")
        return AgentResult.ok(
            data={"output_path": output_path}, size_bytes=size
        )
