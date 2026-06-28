"""ProofreaderAgent — Soát bản in cuối cùng.

Vai trò (con người tương ứng): "thợ soát bản in cuối" — đối chiếu PDF
gốc và PDF đã dịch về số trang, kích thước file, ratio font size, đếm
trang có nội dung. Bắt sai sót dàn trang trước khi giao cho khách.

ProofreaderAgent chỉ soát LAYOUT/STRUCTURE của file PDF đầu ra. Chất lượng
nội dung đã được xử lý trong eval-loop bằng LocalJudge/GlossaryJudge/JudgeAgent
và CriticAgent.

Wrap + mở rộng logic _validate cũ ở coordinator.py.
"""

from __future__ import annotations

import os

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.processor import get_pdf_info


# Tỉ lệ tối thiểu file dịch so với gốc — nhỏ hơn → có thể mất nội dung
MIN_SIZE_RATIO = 0.30
# Tỉ lệ tối đa — lớn hơn → có thể bị duplicate hoặc hallucinate
MAX_SIZE_RATIO = 5.0


class ProofreaderAgent(BaseAgent):
    name = "ProofreaderAgent"

    async def run(self, ctx: AgentContext) -> AgentResult:
        if ctx.is_cancelled():
            return AgentResult.fail("Cancelled", recoverable=True)

        original_pdf = ctx.pdf_path
        translated_pdf = ctx.progress.get("output_path") or os.path.join(
            ctx.job_dir, "output", "translated.pdf"
        )

        if not os.path.exists(translated_pdf):
            return AgentResult.fail(
                f"Translated PDF not found at {translated_pdf}",
                recoverable=False,
            )

        warnings: list[str] = []
        status = "ok"

        try:
            orig_info = get_pdf_info(original_pdf)
            trans_info = get_pdf_info(translated_pdf)
        except Exception as e:
            return AgentResult.fail(
                f"PDF info read failed: {e}", recoverable=True
            )

        orig_pages = orig_info.get("page_count", 0)
        trans_pages = trans_info.get("page_count", 0)
        orig_size = os.path.getsize(original_pdf)
        trans_size = os.path.getsize(translated_pdf)

        if trans_pages != orig_pages:
            status = "warning"
            warnings.append(
                f"Page count mismatch: original {orig_pages} → translated "
                f"{trans_pages}"
            )

        size_ratio = trans_size / orig_size if orig_size > 0 else 0
        if size_ratio < MIN_SIZE_RATIO:
            status = "warning"
            warnings.append(
                f"Translated PDF much smaller ({trans_size:,} vs "
                f"{orig_size:,} bytes, ratio={size_ratio:.2f})"
            )
        if size_ratio > MAX_SIZE_RATIO:
            status = "warning"
            warnings.append(
                f"Translated PDF much larger ({trans_size:,} vs "
                f"{orig_size:,} bytes, ratio={size_ratio:.2f}) — possible duplication"
            )

        # Soát các chunk fail từ phase translate
        failed_chunks = ctx.progress.get("failed_chunks", [])
        if failed_chunks:
            status = "warning"
            warnings.append(
                f"{len(failed_chunks)} chunk(s) failed during translation: "
                f"{failed_chunks[:10]}{'...' if len(failed_chunks) > 10 else ''}"
            )

        validation = {
            "status": status,
            "original_pages": orig_pages,
            "translated_pages": trans_pages,
            "original_size_bytes": orig_size,
            "translated_size_bytes": trans_size,
            "size_ratio": round(size_ratio, 3),
            "warnings": warnings,
        }
        ctx.progress["validation"] = validation
        ctx.save_progress()

        self.log(
            f"Proofread: status={status}, pages {orig_pages}→{trans_pages}, "
            f"size_ratio={size_ratio:.2f}, warnings={len(warnings)}"
        )
        return AgentResult.ok(
            data=validation,
            status=status,
            warnings=len(warnings),
        )
