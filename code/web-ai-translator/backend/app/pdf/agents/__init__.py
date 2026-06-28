"""Multi-agent system cho PDF translation theo 3 pha.

Pha 1 — Chuẩn bị trước dịch:
  - ExtractorAgent    — quét + phân loại text/math blocks.
  - PlannerAgent      — phân tích cấu trúc, chia chunks theo section.
  - GlossaryAgent     — trích xuất + duy trì bảng thuật ngữ.
  - StyleAnchorAgent  — sinh neo văn phong cho các worker song song.

Pha 2 — Vòng dịch, đánh giá và sửa:
  - ModelPassAgent    — cấp model + provenance theo preference của user.
  - CrossModelAgreementAgent — tạo handoff anchor để giữ văn phong khi
                        chuyển model.
  - TranslatorAgent   — dịch từng chunk bằng web AI.
  - LocalJudgeAgent   — kiểm tra lỗi cấu trúc per-chunk, không gọi AI.
  - GlossaryJudgeAgent — kiểm tra nhất quán thuật ngữ trong batch.
  - JudgeAgent        — chấm MQM bằng web AI hoặc COMETKiwi, không dùng Ollama.
  - CriticAgent       — quyết định repair policy: refine, đổi model,
                        ensemble hoặc stop.

Pha 3 — Hoàn thiện:
  - RebuilderAgent    — chèn text dịch vào PDF.
  - ProofreaderAgent  — soát file PDF đầu ra: số trang, kích thước, mở được.
  - ReportAgent       — tổng hợp báo cáo + chốt trạng thái cuối.

Coordinator:
  - MultiAgentCoordinator — điều phối 3 pha, không tự dịch/chấm/sửa.
"""

from app.pdf.agents.base import (
    AgentContext,
    AgentError,
    AgentResult,
    AgentStatus,
    BaseAgent,
)
from app.pdf.agents.coordinator import MultiAgentCoordinator
from app.pdf.agents.cross_model_agreement_agent import CrossModelAgreementAgent
from app.pdf.agents.critic_agent import CriticAgent, RepairDecision
from app.pdf.agents.extractor_agent import ExtractorAgent
from app.pdf.agents.glossary_agent import GlossaryAgent
from app.pdf.agents.glossary_judge_agent import GlossaryJudgeAgent
from app.pdf.agents.judge_agent import JudgeAgent
from app.pdf.agents.local_judge_agent import LocalJudgeAgent
from app.pdf.agents.model_pass_agent import (
    ModelAttemptPlan,
    ModelAttemptScheduler,
    ModelPassAgent,
)
from app.pdf.agents.planner import PlannerAgent, PlanSection, TranslationPlan
from app.pdf.agents.proofreader_agent import ProofreaderAgent
from app.pdf.agents.rebuilder_agent import RebuilderAgent
from app.pdf.agents.report_agent import ReportAgent
from app.pdf.agents.style_anchor_agent import StyleAnchorAgent
from app.pdf.agents.translator_agent import TranslatorAgent

__all__ = [
    # Base contracts
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "AgentError",
    "AgentStatus",
    # Pre-translation
    "ExtractorAgent",
    "PlannerAgent",
    "TranslationPlan",
    "PlanSection",
    "GlossaryAgent",
    "StyleAnchorAgent",
    # Translation
    "TranslatorAgent",
    "ModelPassAgent",
    "ModelAttemptPlan",
    "ModelAttemptScheduler",
    "CrossModelAgreementAgent",
    "CriticAgent",
    "RepairDecision",
    "LocalJudgeAgent",
    "GlossaryJudgeAgent",
    "JudgeAgent",
    # Finalize
    "RebuilderAgent",
    "ProofreaderAgent",
    "ReportAgent",
    # Coordinator
    "MultiAgentCoordinator",
]
