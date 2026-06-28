"""ReportAgent — Tổng hợp báo cáo cuối cùng về quá trình + chất lượng dịch.

Vai trò (con người tương ứng): quản lý dự án viết báo cáo bàn giao — gom điểm
chất lượng, kết quả soát layout, thống kê vòng dịch (số lần dịch/chấm, chunk
đạt/treo), số thuật ngữ, rồi CHỐT trạng thái cuối (done / done_with_warnings).

Trước đây phần chốt trạng thái nằm rải trong ``coordinator.run()``; gom về 1 agent
để khớp sơ đồ "ReportAgent" và để có một bản tóm tắt ``progress["report"]`` duy nhất.
"""

from __future__ import annotations

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent


class ReportAgent(BaseAgent):
    name = "ReportAgent"

    async def run(self, ctx: AgentContext) -> AgentResult:
        prog = ctx.progress
        quality = prog.get("quality") or {}
        validation = prog.get("validation") or {}
        eval_loop = prog.get("eval_loop") or {}
        glossary = (prog.get("glossary") or {}).get("terms") or {}

        score = quality.get("score", 100)
        vstatus = validation.get("status", "ok")
        final_status = (
            "done_with_warnings" if (vstatus == "warning" or score < 70) else "done"
        )

        report = {
            "final_status": final_status,
            "quality_score": score,
            "quality_issues": quality.get("issue_count", 0),
            "validation_status": vstatus,
            "validation_warnings": validation.get("warnings", []),
            "total_translations": eval_loop.get("total_translations"),
            "total_judge_calls": eval_loop.get("total_judge_calls"),
            "passed_chunks": len(eval_loop.get("passed") or []),
            "flagged_chunks": len(eval_loop.get("flagged") or []),
            "glossary_terms": len(glossary),
            "output_path": prog.get("output_path", ""),
        }
        prog["report"] = report
        ctx.save_progress()

        self.log(
            f"Report: status={final_status}, quality={score}/100, "
            f"flagged={report['flagged_chunks']}, terms={report['glossary_terms']}"
        )
        return AgentResult.ok(data=report, final_status=final_status)
