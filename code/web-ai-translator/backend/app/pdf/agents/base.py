"""Base contracts cho multi-agent system.

Định nghĩa:
  - AgentContext     — shared state mọi agent đều có thể đọc/ghi
  - AgentResult      — output chuẩn từ mỗi agent (success/data/errors/metrics)
  - BaseAgent        — interface mỗi agent phải implement
  - AgentError       — exception riêng cho lỗi agent
  - AgentStatus      — trạng thái pending/running/completed/failed/cancelled

Nguyên tắc:
  - Agent KHÔNG tự lưu progress.json → coordinator phụ trách persistence
  - Agent KHÔNG share global state → mọi giao tiếp qua AgentContext
  - Agent return AgentResult thay vì raise (trừ AgentError critical)
  - Cancel check: agent gọi ctx.is_cancelled() định kỳ
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


# ── Status enum ───────────────────────────────────────────────────────────────

class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


# ── Errors ────────────────────────────────────────────────────────────────────

class AgentError(Exception):
    """Lỗi nghiêm trọng từ agent — coordinator sẽ quyết định abort hay retry.

    Khác với raise Exception: AgentError nói rõ agent này fail và lý do,
    để coordinator có thể quyết định pipeline có tiếp tục được không.
    """

    def __init__(self, agent_name: str, message: str, recoverable: bool = True):
        self.agent_name = agent_name
        self.recoverable = recoverable
        super().__init__(f"[{agent_name}] {message}")


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    """Output chuẩn từ mỗi agent.

    success=True → coordinator tiếp tục với data
    success=False + recoverable=True → coordinator có thể retry hoặc skip
    success=False + recoverable=False → coordinator abort pipeline
    """
    success: bool
    data: Any = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    recoverable: bool = True
    duration_seconds: float = 0.0

    @classmethod
    def ok(cls, data: Any = None, **metrics) -> "AgentResult":
        return cls(success=True, data=data, metrics=metrics)

    @classmethod
    def fail(
        cls, error: str, recoverable: bool = True, **metrics
    ) -> "AgentResult":
        return cls(
            success=False,
            errors=[error],
            recoverable=recoverable,
            metrics=metrics,
        )


# ── Context ───────────────────────────────────────────────────────────────────

@dataclass
class AgentContext:
    """Shared state — moi agent đọc/ghi qua đây.

    Coordinator khởi tạo context, mọi agent nhận cùng một instance.
    Cancel check chuyển qua callable để không phụ thuộc thread/asyncio cụ thể.
    """
    # Job identifiers
    job_id: str
    job_dir: str
    pdf_path: str
    mode: str = "standard"   # "standard" | "book"

    # PDF data (filled by coordinator after extract)
    blocks: list = field(default_factory=list)         # list[TextBlock]
    chunks: list = field(default_factory=list)         # list[list[TextBlock]] - filled by Planner
    plan: Optional[Any] = None                          # TranslationPlan from PlannerAgent

    # Translation state
    glossary: dict[str, str] = field(default_factory=dict)
    glossary_enabled: bool = True
    locked_terms: list[str] = field(default_factory=list)   # lowercase EN keys; user-locked terms
    memory: Optional[Any] = None                        # ContextMemory

    # Web automation
    translator: Optional[Any] = None                    # WebAITranslator
    page: Optional[Any] = None                          # active Playwright page
    context: Optional[Any] = None                       # browser context

    # Persistence (coordinator manages, agents call to save)
    progress: dict = field(default_factory=dict)
    save_progress: Callable[[], None] = lambda: None

    # Control flow
    is_cancelled: Callable[[], bool] = lambda: False
    ensure_page: Optional[Callable] = None              # async () -> Page

    # Settings (mode-specific)
    settings: dict = field(default_factory=dict)


# ── Base Agent ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """Interface mọi agent phải implement.

    Subclass:
      - Set `name` (string) cho logging
      - Implement async run(ctx) -> AgentResult
      - Có thể override on_error() để cleanup riêng
    """
    name: str = "BaseAgent"

    def log(self, message: str, level: str = "info"):
        """Logging chuẩn — prefix bằng tên agent để dễ trace."""
        prefix = f"[{self.name}]"
        if level == "warn":
            print(f"{prefix} WARN: {message}")
        elif level == "error":
            print(f"{prefix} ERROR: {message}")
        else:
            print(f"{prefix} {message}")

    @abstractmethod
    async def run(self, ctx: AgentContext) -> AgentResult:
        """Thực thi agent với context. Trả về AgentResult."""
        raise NotImplementedError

    async def execute(self, ctx: AgentContext) -> AgentResult:
        """Wrapper an toàn cho run(): bắt exception, đo time, check cancel.

        Coordinator nên gọi execute() thay vì run() trực tiếp.
        """
        if ctx.is_cancelled():
            return AgentResult.fail(
                f"{self.name} cancelled before start", recoverable=True
            )

        start = time.time()
        try:
            result = await self.run(ctx)
            result.duration_seconds = time.time() - start
            return result
        except AgentError as e:
            self.log(f"AgentError: {e}", level="error")
            return AgentResult(
                success=False,
                errors=[str(e)],
                recoverable=e.recoverable,
                duration_seconds=time.time() - start,
            )
        except Exception as e:
            self.log(f"Unexpected exception: {type(e).__name__}: {e}", level="error")
            return AgentResult(
                success=False,
                errors=[f"{type(e).__name__}: {e}"],
                recoverable=True,
                duration_seconds=time.time() - start,
            )
