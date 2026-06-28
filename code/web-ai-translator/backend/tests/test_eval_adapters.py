"""Tests cho eval_adapters — phần thuần (không browser).

ModelPassAgent attempt scheduler + judge gộp-prompt build/parse.
"""

from app.pdf.eval_adapters import (
    build_batch_judge_prompt,
    parse_batch_judge_response,
)
from app.pdf.agents.critic_agent import CriticAgent
from app.pdf.agents.model_pass_agent import ModelAttemptScheduler
from app.pdf.model_preference import expand_model_execution_order


# ── ModelPassAgent scheduler: cấp model + provenance từng attempt ─────────────

def test_attempt_scheduler_plans_ladder_and_ensemble():
    scheduler = ModelAttemptScheduler(["gemini", "chatgpt", "deepseek"])
    plans = [scheduler.next_attempt(0) for _ in range(4)]
    assert [p.strategy for p in plans] == [
        "initial", "refine", "escalate", "ensemble",
    ]
    assert [p.model for p in plans[:3]] == ["gemini", "gemini", "chatgpt"]
    assert plans[3].candidate_models == ("gemini", "chatgpt")
    assert plans[2].provenance()["model_order"] == [
        "gemini", "chatgpt", "deepseek",
    ]


def test_attempt_scheduler_user_order_reversed():
    scheduler = ModelAttemptScheduler(["DeepSeek", "Gemini"])
    assert scheduler.next_attempt(7).model == "deepseek"
    assert scheduler.next_attempt(7).model == "deepseek"  # refine cùng model đầu


def test_attempt_scheduler_independent_per_chunk():
    scheduler = ModelAttemptScheduler(["a", "b", "c"])
    assert scheduler.next_attempt(0).model == "a"
    assert scheduler.next_attempt(1).model == "a"
    assert scheduler.next_attempt(0).strategy == "refine"
    assert scheduler.next_attempt(0).model == "b"


def test_attempt_scheduler_empty_falls_back():
    assert ModelAttemptScheduler([]).models == ["gemini"]
    assert ModelAttemptScheduler(["", "  "]).models == ["gemini"]


def test_attempt_scheduler_skips_down_model_for_new_chunks():
    scheduler = ModelAttemptScheduler(["gemini", "chatgpt", "deepseek"])
    scheduler.mark_model_down("gemini", reason="rate_limit")

    plan = scheduler.next_attempt(3)

    assert plan.strategy == "initial"
    assert plan.model == "chatgpt"
    assert plan.provenance()["unavailable_models"] == ["gemini"]
    assert plan.provenance()["available_model_order"] == ["chatgpt", "deepseek"]


def test_attempt_scheduler_restarts_chunk_when_previous_model_goes_down():
    scheduler = ModelAttemptScheduler(["gemini", "chatgpt"])
    assert scheduler.next_attempt(0).model == "gemini"
    scheduler.mark_model_down("gemini", reason="captcha")

    plan = scheduler.next_attempt(0)

    assert plan.strategy == "initial"
    assert plan.model == "chatgpt"
    assert plan.reason == "model_failover_from_gemini"


def test_critic_agent_owns_repair_policy():
    critic = CriticAgent()
    scheduler = ModelAttemptScheduler(["gemini", "chatgpt"])
    decisions = [
        critic.decide_repair(scheduler.next_attempt(0))
        for _ in range(4)
    ]
    assert [d.action for d in decisions] == [
        "translate", "refine", "change_model", "ensemble",
    ]
    assert [d.model for d in decisions[:3]] == [
        "gemini", "gemini", "chatgpt",
    ]
    assert decisions[3].candidate_models == ("gemini", "chatgpt")


def test_critic_agent_can_stop_repair_after_budget():
    critic = CriticAgent(max_repair_attempts=1)
    scheduler = ModelAttemptScheduler(["gemini"])
    assert critic.decide_repair(scheduler.next_attempt(0)).action == "translate"
    decision = critic.decide_repair(scheduler.next_attempt(0))
    assert decision.action == "stop"
    assert decision.should_stop is True


def test_single_model_preference_gets_emergency_fallbacks():
    order = expand_model_execution_order(["chatgpt"])
    assert order[0] == "chatgpt"
    assert "gemini" in order
    assert len(order) > 1


def test_multi_model_preference_does_not_add_extra_fallbacks():
    assert expand_model_execution_order(["chatgpt", "gemini"]) == [
        "chatgpt", "gemini",
    ]


# ── Batched judge prompt ──────────────────────────────────────────────────────

def test_batch_prompt_contains_every_segment():
    batch = [(0, "Hello world", "Xin chào thế giới"),
             (3, "Deep learning", "Học sâu")]
    p = build_batch_judge_prompt(batch)
    assert "[index 0]" in p and "[index 3]" in p
    assert "Hello world" in p and "Xin chào thế giới" in p
    assert "Deep learning" in p and "Học sâu" in p
    assert "MQM" in p and "JSON" in p


# ── Parser ────────────────────────────────────────────────────────────────────

def test_parse_computes_mqm_from_errors():
    raw = '''[
      {"index": 0, "errors": []},
      {"index": 1, "errors": [{"category": "accuracy", "severity": "major"}]}
    ]'''
    out = parse_batch_judge_response(raw, [0, 1])
    assert out[0] == 100.0                 # không lỗi → 100
    assert out[1] is not None and out[1] < 100.0   # có lỗi major → trừ điểm


def test_parse_handles_code_fence_and_missing_index():
    raw = "```json\n[{\"index\": 2, \"errors\": []}]\n```"
    out = parse_batch_judge_response(raw, [2, 5])
    assert out[2] == 100.0
    assert out[5] is None                  # index 5 không có trong phản hồi


def test_parse_falls_back_to_score_without_errors():
    raw = '[{"index": 0, "score": 82}]'
    out = parse_batch_judge_response(raw, [0])
    assert out[0] == 82.0


def test_parse_malformed_returns_all_none():
    assert parse_batch_judge_response("not json at all", [0, 1]) == {0: None, 1: None}
    assert parse_batch_judge_response("", [4]) == {4: None}


# ── refine_chunk: refine 1 chunk theo critique của chính nó (fake translator) ──

def test_refine_chunk_critiques_and_sends_refine_prompt():
    import asyncio
    from types import SimpleNamespace
    from app.pdf.agents.critic_agent import CriticAgent

    # 1 block "dịch" vẫn là tiếng Anh → HeuristicCritic phải bắt lỗi untranslated
    english = ("This is a sufficiently long English sentence that clearly "
               "was not translated into Vietnamese at all.")
    blocks = [SimpleNamespace(
        text=english, translated_text=english,
        is_translatable=True, page_num=0, block_idx=0,
    )]

    sent = {}

    class FakeTranslator:
        async def _send_prompt_and_get_response(self, page, prompt):
            sent["prompt"] = prompt
            return "```text\n[1] Đây là câu tiếng Việt đã được sửa.\n```"

    critic = CriticAgent()
    out = asyncio.run(critic.refine_chunk(
        blocks, page=None, translator=FakeTranslator(),
        glossary={}, locked_terms=[],
    ))

    assert out == "[1] Đây là câu tiếng Việt đã được sửa."
    # Có lỗi cụ thể → dùng refine prompt (chứa danh sách lỗi + bản dịch cũ)
    assert "LỖI CẦN SỬA" in sent["prompt"]
    assert "[1]" in sent["prompt"]


# ── build_produce_fn: thang sửa leo dần theo số lần gọi 1 chunk ───────────────

def _ladder_fixture(monkeypatch, refine_return="R"):
    """Cài fake translator/refine; trả (eval_adapters, produce_fn, log)."""
    import asyncio
    from types import SimpleNamespace
    import app.pdf.eval_adapters as eval_adapters
    from app.pdf.agents.critic_agent import CriticAgent

    log = []

    def fake_make_translator(ctx, translators):
        async def translate_one(model, idx):
            log.append(f"translate:{model}")
            return "G" if model == "gemini" else "CCCC"   # khác độ dài cho ensemble
        return translate_one

    async def fake_refine(self, chunk, *, page, translator, glossary=None,
                          locked_terms=None, errors=None, codec=None):
        log.append("refine")
        return refine_return

    monkeypatch.setattr(eval_adapters, "_make_chunk_translator", fake_make_translator)
    monkeypatch.setattr(CriticAgent, "refine_chunk", fake_refine)

    ctx = SimpleNamespace(
        chunks=[[], []],
        glossary={},
        glossary_enabled=True,
        locked_terms=[],
        progress={},
        save_progress=lambda: None,
    )
    translators = {"gemini": ("tr_g", "pg_g"), "chatgpt": ("tr_c", "pg_c")}
    locks = {"gemini": asyncio.Lock(), "chatgpt": asyncio.Lock()}
    heuristic = lambda idx, text: len(text)   # ensemble chọn bản dài hơn

    produce = eval_adapters.build_produce_fn(
        ctx, ["gemini", "chatgpt"], translators, locks, heuristic,
    )
    return produce, log, ctx


def test_produce_fn_climbs_translate_refine_escalate_ensemble(monkeypatch):
    import asyncio
    produce, log, _ctx = _ladder_fixture(monkeypatch)

    async def run():
        return [await produce(0) for _ in range(4)]
    results = asyncio.run(run())

    # n=0 dịch m0 / n=1 refine / n=2 đổi sang m1 / n=3 ensemble (2 model, chọn dài hơn)
    assert log == ["translate:gemini", "refine", "translate:chatgpt",
                   "translate:gemini", "translate:chatgpt"]
    assert results[0] == ("G", True)
    assert results[1] == ("R", True)
    assert results[2] == ("CCCC", True)
    assert results[3] == ("CCCC", True)     # heuristic cao hơn thắng


def test_produce_fn_refine_empty_falls_back_to_translate(monkeypatch):
    import asyncio
    produce, log, _ctx = _ladder_fixture(monkeypatch, refine_return="")

    async def run():
        return [await produce(0) for _ in range(2)]
    results = asyncio.run(run())

    # refine trả "" → vẫn thử dịch lại m0 trong cùng attempt
    assert log == ["translate:gemini", "refine", "translate:gemini"]
    assert results[1] == ("G", True)


def test_produce_fn_records_attempt_provenance(monkeypatch):
    import asyncio
    produce, _log, ctx = _ladder_fixture(monkeypatch)

    async def run():
        return await produce(0)
    result = asyncio.run(run())

    assert result == ("G", True)
    attempts = ctx.progress["translation_attempts"]["0"]
    assert attempts[0]["strategy"] == "initial"
    assert attempts[0]["repair_action"] == "translate"
    assert attempts[0]["selected_model"] == "gemini"
    assert ctx.progress["translation_provenance"]["0"]["text_chars"] == 1


def test_produce_fn_circuit_breaker_falls_new_chunks_to_next_model(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import app.pdf.eval_adapters as eval_adapters

    log = []

    def fake_make_translator(ctx, translators):
        async def translate_one(model, idx):
            log.append(f"translate:{model}:{idx}")
            return "" if model == "gemini" else "C"
        return translate_one

    monkeypatch.setattr(eval_adapters, "_make_chunk_translator", fake_make_translator)

    ctx = SimpleNamespace(
        chunks=[[], [], [], []],
        glossary={},
        glossary_enabled=True,
        locked_terms=[],
        progress={},
        save_progress=lambda: None,
    )
    translators = {"gemini": ("tr_g", "pg_g"), "chatgpt": ("tr_c", "pg_c")}
    locks = {"gemini": asyncio.Lock(), "chatgpt": asyncio.Lock()}
    produce = eval_adapters.build_produce_fn(
        ctx, ["gemini", "chatgpt"], translators, locks, lambda idx, text: 100,
    )

    async def run():
        return [await produce(i) for i in range(4)]
    results = asyncio.run(run())

    assert log == [
        "translate:gemini:0",
        "translate:gemini:1",
        "translate:gemini:2",
        "translate:chatgpt:3",
    ]
    assert results[:3] == [("", False), ("", False), ("", False)]
    assert results[3] == ("C", True)
    assert ctx.progress["model_health"]["gemini"]["down"] is True
    assert ctx.progress["model_failover"]["available_models"] == ["chatgpt"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
