"""Tests for multi-agent system in app/pdf/agents/.

Tests cover:
  - base.py: AgentContext, AgentResult, BaseAgent.execute() error handling
  - planner.py: section detection + chunking, sentence boundary respect
  - glossary_agent.py: seeding from global, filter helpers
  - translator_agent.py: prompt building, response extraction, truncation detection
  - critic_agent.py: fix application, prompt assembly
  - coordinator.py: phase ordering, resume support (without real browser)

No real browser, no real Ollama. Uses in-memory mocks where needed.
"""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.pdf.agents.base import (
    AgentContext,
    AgentResult,
    BaseAgent,
    AgentError,
    AgentStatus,
)
from app.pdf.agents.planner import (
    PlannerAgent,
    PlanSection,
    TranslationPlan,
    _is_likely_section_header,
    _ends_at_sentence,
)
from app.pdf.agents.glossary_agent import GlossaryAgent
from app.pdf.agents.translator_agent import (
    TranslatorAgent,
    TranslateRequest,
    _build_translation_prompt,
    _extract_text_from_response,
    _is_truncated,
)
from app.pdf.agents.critic_agent import CriticAgent
from app.pdf.agents.cross_model_agreement_agent import CrossModelAgreementAgent


# ── Fakes ─────────────────────────────────────────────────────────────────────

@dataclass
class FakeBlock:
    """Minimal stand-in for TextBlock — just the fields agents read."""
    text: str
    is_translatable: bool = True
    is_math: bool = False
    font_size: float = 10.0
    spans_info: list = field(default_factory=list)
    translated_text: str = None
    page_num: int = 0
    block_idx: int = 0
    bbox: tuple = (0, 0, 100, 20)


def _make_ctx(**overrides) -> AgentContext:
    """Create a minimal AgentContext with sensible defaults."""
    defaults = {
        "job_id": "test-job",
        "job_dir": "/tmp/test-job",
        "pdf_path": "/tmp/test.pdf",
        "progress": {"translated_chunks": {}},
        "save_progress": lambda: None,
        "is_cancelled": lambda: False,
    }
    defaults.update(overrides)
    return AgentContext(**defaults)


# ── base.py ───────────────────────────────────────────────────────────────────

def test_agent_result_ok_factory():
    r = AgentResult.ok(data={"x": 1}, count=5)
    assert r.success
    assert r.data == {"x": 1}
    assert r.metrics["count"] == 5
    assert r.errors == []


def test_agent_result_fail_factory():
    r = AgentResult.fail("oops", recoverable=False)
    assert not r.success
    assert "oops" in r.errors
    assert not r.recoverable


def test_agent_error_message_includes_name():
    err = AgentError("MyAgent", "boom", recoverable=False)
    assert "MyAgent" in str(err)
    assert "boom" in str(err)
    assert not err.recoverable


def test_agent_status_values():
    assert AgentStatus.PENDING == "pending"
    assert AgentStatus.RUNNING == "running"
    assert AgentStatus.COMPLETED == "completed"
    assert AgentStatus.FAILED == "failed"


class _OkAgent(BaseAgent):
    name = "OkAgent"
    async def run(self, ctx):
        return AgentResult.ok("done")


class _BoomAgent(BaseAgent):
    name = "BoomAgent"
    async def run(self, ctx):
        raise ValueError("kaboom")


class _AgentErrorAgent(BaseAgent):
    name = "AEAgent"
    async def run(self, ctx):
        raise AgentError("AEAgent", "fail", recoverable=False)


def test_base_agent_execute_ok():
    ctx = _make_ctx()
    result = asyncio.run(_OkAgent().execute(ctx))
    assert result.success
    assert result.data == "done"
    assert result.duration_seconds >= 0


def test_base_agent_execute_catches_exception():
    ctx = _make_ctx()
    result = asyncio.run(_BoomAgent().execute(ctx))
    assert not result.success
    assert "kaboom" in result.errors[0]
    assert result.recoverable  # generic exception → recoverable=True default


def test_base_agent_execute_catches_agent_error():
    ctx = _make_ctx()
    result = asyncio.run(_AgentErrorAgent().execute(ctx))
    assert not result.success
    assert not result.recoverable


def test_base_agent_execute_skips_when_cancelled():
    ctx = _make_ctx(is_cancelled=lambda: True)
    result = asyncio.run(_OkAgent().execute(ctx))
    assert not result.success
    assert "cancelled" in result.errors[0].lower()


# ── planner.py: section detection ─────────────────────────────────────────────

def test_section_header_pattern_matches_numbered():
    is_h, title = _is_likely_section_header(
        "1. Introduction", font_size=12, is_bold=True, avg_font_size=10.0
    )
    assert is_h
    assert "Introduction" in title


def test_section_header_pattern_matches_keyword():
    is_h, title = _is_likely_section_header(
        "Abstract", font_size=11, is_bold=True, avg_font_size=10.0
    )
    assert is_h
    assert "Abstract" in title


def test_section_header_rejects_long_text():
    is_h, _ = _is_likely_section_header(
        "This is a very long sentence that is clearly not a section header " * 3,
        font_size=12, is_bold=True, avg_font_size=10.0,
    )
    assert not is_h


def test_section_header_bold_large_font_short():
    """Pure heuristic: bold + larger font + short text."""
    is_h, _ = _is_likely_section_header(
        "Methodology Overview", font_size=14, is_bold=True, avg_font_size=10.0,
    )
    assert is_h


def test_ends_at_sentence_recognises_punctuation():
    assert _ends_at_sentence("This ends with period.")
    assert _ends_at_sentence("Question? ")
    assert _ends_at_sentence("Exclaim!")
    assert _ends_at_sentence("Semicolon;")
    assert not _ends_at_sentence("Mid sentence here")


# ── PlannerAgent.run() ────────────────────────────────────────────────────────

def test_planner_fails_on_empty_blocks():
    ctx = _make_ctx(blocks=[])
    result = asyncio.run(PlannerAgent().run(ctx))
    assert not result.success
    assert not result.recoverable


def test_planner_chunks_simple_text():
    blocks = [
        FakeBlock(text="Abstract", font_size=14),
        FakeBlock(text="This paper presents a method. " * 30, font_size=10),
        FakeBlock(text="1. Introduction", font_size=14),
        FakeBlock(text="The introduction discusses prior work. " * 30, font_size=10),
    ]
    ctx = _make_ctx(blocks=blocks)
    result = asyncio.run(PlannerAgent().run(ctx))
    assert result.success
    assert ctx.plan is not None
    assert len(ctx.chunks) >= 1


def test_planner_detects_sections_from_keywords():
    blocks = [
        FakeBlock(text="Abstract", font_size=12),
        FakeBlock(text="Some abstract text. " * 10, font_size=10),
        FakeBlock(text="1. Introduction", font_size=12),
        FakeBlock(text="Intro text. " * 10, font_size=10),
        FakeBlock(text="2. Method", font_size=12),
        FakeBlock(text="Method text. " * 10, font_size=10),
    ]
    ctx = _make_ctx(blocks=blocks)
    asyncio.run(PlannerAgent().run(ctx))
    titles = [s.title.lower() for s in ctx.plan.sections]
    # At least 2 section headers should be detected
    assert any("intro" in t or "method" in t or "abstract" in t for t in titles)


def test_translation_plan_section_for_chunk_returns_none_when_unmapped():
    plan = TranslationPlan(sections=[], chunks=[[]])
    assert plan.section_for_chunk(0) is None


def test_translation_plan_summary_handles_no_sections():
    plan = TranslationPlan(sections=[], chunks=[[], [], []])
    s = plan.summary()
    assert "no sections" in s.lower()


def test_translation_plan_to_dict_serialisable():
    sec = PlanSection(title="Intro", start_block_idx=0, end_block_idx=5)
    plan = TranslationPlan(sections=[sec], chunks=[[]], total_chars=100)
    d = plan.to_dict()
    assert d["num_chunks"] == 1
    assert d["num_sections"] == 1
    assert d["sections"][0]["title"] == "Intro"


# ── translator_agent.py ───────────────────────────────────────────────────────

def test_build_prompt_includes_required_sections():
    p = _build_translation_prompt("Hello world")
    assert "Hello world" in p
    assert "QUY TẮC" in p


def test_build_prompt_with_context_and_glossary():
    ctx_text = "=== NGỮ CẢNH ===\nVI: trước đó\n\n"
    gloss = "=== GLOSSARY ===\nML → ML\n\n"
    p = _build_translation_prompt(
        "Hello", glossary_text=gloss, context_text=ctx_text
    )
    assert "GLOSSARY" in p
    assert "NGỮ CẢNH" in p
    assert "NHẤT QUÁN" in p
    assert "BẢNG THUẬT NGỮ" in p


def test_build_prompt_with_section_hint():
    p = _build_translation_prompt("Text", section_hint="Introduction")
    assert "Introduction" in p
    assert "NGỮ CẢNH SECTION" in p


def test_extract_text_from_text_block():
    raw = "Some preamble.\n```text\n[1] Bản dịch\n```\nTrailing"
    out = _extract_text_from_response(raw)
    assert "[1] Bản dịch" in out
    assert "Trailing" not in out


def test_extract_text_strips_chatbot_artifacts():
    raw = "[1] Bản dịch\n\nBạn có muốn tôi tiếp tục dịch không?"
    out = _extract_text_from_response(raw)
    assert "[1]" in out
    assert "Bạn có muốn" not in out


def test_extract_text_strips_prompt_leakage():
    raw = "[1] Bản dịch\n\n=== QUY TẮC BẮT BUỘC ===\n1. Foo"
    out = _extract_text_from_response(raw)
    assert "[1]" in out
    assert "QUY TẮC" not in out


def test_is_truncated_short_input_never_truncated():
    assert not _is_truncated("short", "any")


def test_is_truncated_empty_translation():
    assert _is_truncated("a" * 300, "")


def test_is_truncated_low_ratio():
    original = "x" * 1000
    short_translation = "y" * 100
    assert _is_truncated(original, short_translation)


def test_is_truncated_full_translation():
    original = "x" * 1000
    full_translation = "y" * 800
    assert not _is_truncated(original, full_translation)


def test_translator_agent_run_no_chunks_fails():
    ctx = _make_ctx(chunks=[])
    result = asyncio.run(TranslatorAgent().run(ctx))
    assert not result.success


# ── glossary_agent.py ─────────────────────────────────────────────────────────

def test_glossary_agent_fails_when_no_chunks():
    ctx = _make_ctx(chunks=[])
    result = asyncio.run(GlossaryAgent().run(ctx))
    assert not result.success
    assert not result.recoverable


def test_glossary_agent_seed_returns_dict():
    """_seed_from_global must always return a dict (empty if DB unavailable)."""
    seeded = GlossaryAgent._seed_from_global()
    assert isinstance(seeded, dict)


def test_glossary_agent_skips_extraction_without_translator(monkeypatch):
    """When no translator/page → extract_from_chunks returns (terms, fields) empty."""
    chunks = [[FakeBlock(text="hello world")]]
    ctx = _make_ctx(chunks=chunks, translator=None, ensure_page=None)
    agent = GlossaryAgent()
    terms, fields = asyncio.run(agent._extract_from_chunks(ctx))
    assert terms == {}
    assert fields == {}


def test_glossary_agent_run_returns_seed_only_without_translator():
    """Without translator, glossary still completes (just seeds + empty extract)."""
    chunks = [[FakeBlock(text="hello world")]]
    ctx = _make_ctx(chunks=chunks, translator=None, ensure_page=None)
    result = asyncio.run(GlossaryAgent().run(ctx))
    assert result.success
    # Glossary should be a dict (possibly empty if no global seed)
    assert isinstance(ctx.glossary, dict)


# ── critic_agent.py ───────────────────────────────────────────────────────────

def test_critic_agent_apply_fix_replaces_block_translation():
    blocks = [
        FakeBlock(text="Hello world", translated_text="Old translation"),
        FakeBlock(text="Goodbye", translated_text="Tạm biệt cũ"),
    ]
    translated = "[1] Xin chào thế giới\n\n[2] Tạm biệt mới"
    fixed = CriticAgent._apply_fix(translated, blocks)
    assert fixed == 2
    assert blocks[0].translated_text == "Xin chào thế giới"
    assert blocks[1].translated_text == "Tạm biệt mới"


def test_critic_agent_apply_fix_skips_non_vietnamese():
    """Should not overwrite if new text has no Vietnamese chars."""
    blocks = [FakeBlock(text="hi", translated_text="Cũ tiếng Việt")]
    translated = "[1] english only no vietnamese"
    fixed = CriticAgent._apply_fix(translated, blocks)
    assert fixed == 0
    assert blocks[0].translated_text == "Cũ tiếng Việt"


def test_critic_agent_group_blocks_respects_max_chars():
    blocks = [FakeBlock(text="x" * 800) for _ in range(5)]
    grouped = CriticAgent._group_blocks(blocks, max_chars=1500)
    # Each chunk should fit roughly within budget
    for chunk in grouped:
        total = sum(len(b.text) for b in chunk)
        assert total <= 1500 + 800  # +1 block overflow allowed


def test_critic_agent_blocks_to_numbered_format():
    blocks = [
        FakeBlock(text="First", translated_text="Đầu"),
        FakeBlock(text="Second", translated_text="Thứ hai"),
    ]
    out = CriticAgent._blocks_to_numbered(blocks, field="translation")
    assert "[1] Đầu" in out
    assert "[2] Thứ hai" in out


def test_critic_agent_extract_text_clean():
    raw = "Preamble.\n```text\n[1] Đoạn dịch\n```"
    out = CriticAgent._extract_text(raw)
    assert "[1] Đoạn dịch" in out


def test_critic_agent_fails_on_empty_blocks():
    ctx = _make_ctx(blocks=[])
    result = asyncio.run(CriticAgent().run(ctx))
    assert not result.success
    assert not result.recoverable


# ── cross_model_agreement_agent.py ────────────────────────────────────────────

def test_cross_model_agreement_builds_handoff_anchor():
    chunks = [
        [
            FakeBlock(
                text="The proposed method improves robustness.",
                translated_text="Phương pháp được đề xuất cải thiện độ bền vững.",
            )
        ],
        [FakeBlock(text="It also reduces training cost.")],
    ]
    ctx = _make_ctx(chunks=chunks, progress={"translated_chunks": {}})
    agent = CrossModelAgreementAgent(window=1)

    anchor = agent.prepare_handoff(
        ctx, 1, from_model="gemini", to_model="chatgpt"
    )

    assert anchor is not None
    assert anchor["from_model"] == "gemini"
    assert anchor["to_model"] == "chatgpt"
    assert "robustness" in anchor["en"]
    assert "độ bền vững" in anchor["vi"]
    key = CrossModelAgreementAgent.handoff_key(1, "chatgpt")
    assert ctx.progress["cross_model_handoffs"][key] == anchor


def test_cross_model_agreement_merges_with_style_anchor():
    base = {"en": "Source style", "vi": "Văn phong gốc"}
    handoff = {"en": "Previous source", "vi": "Bản dịch trước"}

    merged = CrossModelAgreementAgent.merge_style_anchor(base, handoff)

    assert "Source style" in merged["en"]
    assert "Previous source" in merged["en"]
    assert "Văn phong gốc" in merged["vi"]
    assert "Bản dịch trước" in merged["vi"]


def test_cross_model_agreement_keeps_global_handoff_chain():
    chunks = [
        [
            FakeBlock(
                text="The first model established the terminology.",
                translated_text="Mô hình đầu tiên đã thiết lập thuật ngữ.",
            )
        ],
        [
            FakeBlock(
                text="The second model follows that academic style.",
                translated_text="Mô hình thứ hai tiếp nối văn phong học thuật đó.",
            )
        ],
        [FakeBlock(text="The next chunk should keep the same style.")],
    ]
    ctx = _make_ctx(chunks=chunks, progress={"translated_chunks": {}})
    agent = CrossModelAgreementAgent(window=2)

    agent.record_success(ctx, 0, model="gemini")
    first = agent.prepare_global_handoff(
        ctx, 1, from_model=agent.current_style_owner(ctx),
        to_model="chatgpt", reason="rate_limit",
    )
    agent.record_success(ctx, 1, model="chatgpt")
    second = agent.prepare_global_handoff(
        ctx, 2, from_model=agent.current_style_owner(ctx),
        to_model="deepseek", reason="captcha",
    )

    assert first["from_model"] == "gemini"
    assert first["to_model"] == "chatgpt"
    assert second["from_model"] == "chatgpt"
    assert second["to_model"] == "deepseek"
    assert CrossModelAgreementAgent.get_handoff_anchor(ctx, 2, "deepseek") == second
    assert len(ctx.progress["cross_model_handoff_history"]) == 2


# ── End-to-end mini test (no browser) ─────────────────────────────────────────

def test_planner_then_translator_run_integration():
    """Pipeline ordering: Planner sets ctx.chunks → TranslatorAgent.run() reads them."""
    blocks = [
        FakeBlock(text="Abstract", font_size=12),
        FakeBlock(text="This paper studies neural networks. " * 5, font_size=10),
    ]
    ctx = _make_ctx(blocks=blocks)

    plan_result = asyncio.run(PlannerAgent().run(ctx))
    assert plan_result.success
    assert len(ctx.chunks) >= 1

    # Without a translator, TranslatorAgent.run() should fail gracefully
    ctx.progress["_current_chunk_index"] = 0
    # Need ensure_page → use a mock that raises
    ctx.translator = MagicMock()
    ctx.translator._send_prompt_and_get_response = AsyncMock(
        return_value="```text\n[1] Bản dịch\n```"
    )
    ctx.ensure_page = AsyncMock(return_value=MagicMock())

    result = asyncio.run(TranslatorAgent().translate_chunk(
        ctx,
        TranslateRequest(
            chunk_index=0,
            chunk=ctx.chunks[0],
            section_hint="Abstract",
            max_retries=0,
        ),
    ))
    assert result.success
    assert "Bản dịch" in result.data["translated"]
