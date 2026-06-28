"""Tests cho 3 agent mới của panel judge: Local / Glossary / Report.

Toàn bộ chạy LOCAL (không browser, không AI) — dùng TextBlock giả.
"""

import asyncio
from types import SimpleNamespace


def blk(text, translated, translatable=True, page=0, idx=0):
    return SimpleNamespace(
        text=text, translated_text=translated,
        is_translatable=translatable, page_num=page, block_idx=idx,
    )


# ── LocalJudgeAgent: lỗi cấu trúc per-chunk, KHÔNG dính terminology ───────────

def test_local_judge_flags_untranslated_structural():
    from app.pdf.agents.local_judge_agent import LocalJudgeAgent
    eng = ("This paragraph is clearly long enough and was not translated "
           "into Vietnamese at all here.")
    score, errors = LocalJudgeAgent().judge_chunk([blk(eng, eng)])
    assert score < 100
    assert errors                                   # bắt được lỗi cấu trúc
    assert all(e.category != "terminology" for e in errors)


def test_local_judge_clean_chunk():
    from app.pdf.agents.local_judge_agent import LocalJudgeAgent
    score, errors = LocalJudgeAgent().judge_chunk(
        [blk("This is a short sentence about cats.", "Đây là một câu ngắn về loài mèo.")]
    )
    assert score >= 90
    assert all(e.category != "terminology" for e in errors)


# ── GlossaryJudgeAgent: chỉ lỗi terminology + prompt batch ────────────────────

def test_glossary_judge_terminology_violation():
    from app.pdf.agents.glossary_judge_agent import GlossaryJudgeAgent
    chunk = [blk("The neural network is trained on data.",
                 "Mạng này được huấn luyện trên dữ liệu.")]
    errors = GlossaryJudgeAgent().judge_chunk_local(chunk, {"neural network": "mạng nơ-ron"})
    assert errors
    assert all(e.category == "terminology" for e in errors)


def test_glossary_judge_no_glossary_empty():
    from app.pdf.agents.glossary_judge_agent import GlossaryJudgeAgent
    assert GlossaryJudgeAgent().judge_chunk_local([blk("hello", "xin chào")], None) == []


def test_glossary_judge_batch_prompt_contains_segments():
    from app.pdf.agents.glossary_judge_agent import GlossaryJudgeAgent
    p = GlossaryJudgeAgent.build_batch_newterm_prompt(
        [(0, "Deep learning", "Học sâu"), (3, "gradient descent", "hạ gradient")]
    )
    assert "[đoạn 0]" in p and "[đoạn 3]" in p
    assert "Deep learning" in p and "Học sâu" in p


# ── JudgeAgent: backend dispatcher mới, không chứa Ollama ─────────────────────

def test_judge_agent_normalizes_cometkiwi_aliases():
    from app.pdf.agents.judge_agent import normalize_judge_backend
    assert normalize_judge_backend("wmt22-cometkiwi-da") == "cometkiwi"
    assert normalize_judge_backend("Unbabel/wmt22-cometkiwi-da") == "cometkiwi"


def test_judge_agent_rejects_ollama_backend():
    import pytest
    from app.pdf.agents.judge_agent import normalize_judge_backend
    with pytest.raises(ValueError):
        normalize_judge_backend("ollama")


# ── ReportAgent: chốt trạng thái cuối ────────────────────────────────────────

def test_report_agent_done_with_warnings():
    from app.pdf.agents.report_agent import ReportAgent
    ctx = SimpleNamespace(
        progress={
            "quality": {"score": 65, "issue_count": 3},
            "validation": {"status": "ok"},
            "eval_loop": {"total_translations": 10, "total_judge_calls": 3,
                          "passed": [0, 1, 2], "flagged": [3]},
            "glossary": {"terms": {"a": "b"}},
            "output_path": "x.pdf",
        },
        save_progress=lambda: None,
    )
    res = asyncio.run(ReportAgent().run(ctx))
    assert res.success
    r = ctx.progress["report"]
    assert r["final_status"] == "done_with_warnings"   # score 65 < 70
    assert r["flagged_chunks"] == 1 and r["passed_chunks"] == 3
    assert r["glossary_terms"] == 1


def test_report_agent_done_clean():
    from app.pdf.agents.report_agent import ReportAgent
    ctx = SimpleNamespace(
        progress={"quality": {"score": 95}, "validation": {"status": "ok"}},
        save_progress=lambda: None,
    )
    asyncio.run(ReportAgent().run(ctx))
    assert ctx.progress["report"]["final_status"] == "done"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
