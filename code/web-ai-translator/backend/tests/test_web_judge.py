# -*- coding: utf-8 -*-
"""Tests for the cross-model web judge (app/pdf/web_judge.py) + backend factory.

The cross-model judge MUST grade with a web AI different from the translator
(self-judging bias mitigation). These tests cover:

  pick_judge_backend()      — judge backend is always ≠ translator
  make_backend() / _BACKENDS — backend factory (gemini/chatgpt/deepseek)
  WebAITranslator(backend=) — per-instance backend override
  DeepSeekBackend           — interface completeness
  judge_segment()           — MQM recompute, self-score preserved, error paths
  judge_segments_batch()    — report shape, cross-model guarantee (mocked browser)

No real browser or network: WebAITranslator is faked for the async batch test.
"""

import pytest

from app.pdf.web_judge import (
    pick_judge_backend,
    judge_segment,
    judge_segments_batch,
    KNOWN_BACKENDS,
    _JUDGE_PRIORITY,
)
from app.services.translator import (
    make_backend,
    _BACKENDS,
    WebAITranslator,
    GeminiBackend,
    ChatGPTBackend,
    AIStudioBackend,
    DeepSeekBackend,
    GrokBackend,
    CopilotBackend,
)


# ── pick_judge_backend — core cross-model guarantee ──────────────────────

def test_preferred_valid_and_different_is_used():
    assert pick_judge_backend("gemini", preferred="chatgpt") == "chatgpt"
    assert pick_judge_backend("gemini", preferred="deepseek") == "deepseek"


def test_preferred_equal_to_translator_is_overridden():
    # preferred == translator → must auto-pick something else
    out = pick_judge_backend("chatgpt", preferred="chatgpt")
    assert out != "chatgpt"
    assert out in KNOWN_BACKENDS


def test_preferred_invalid_falls_back_to_autopick():
    out = pick_judge_backend("gemini", preferred="not-a-backend")
    assert out != "gemini"
    assert out in KNOWN_BACKENDS


def test_autopick_follows_priority_when_no_preferred():
    # translator=gemini → first in priority that differs is chatgpt
    assert pick_judge_backend("gemini") == "chatgpt"
    # translator=chatgpt → priority skips chatgpt, picks deepseek
    assert pick_judge_backend("chatgpt") == "deepseek"


def test_case_insensitive_translator_name():
    assert pick_judge_backend("GEMINI", preferred="ChatGPT") == "chatgpt"


@pytest.mark.parametrize("translator", list(KNOWN_BACKENDS))
def test_judge_is_never_equal_to_translator(translator):
    # No preferred, and every preferred option — judge must differ.
    assert pick_judge_backend(translator) != translator
    for pref in [None, *KNOWN_BACKENDS]:
        assert pick_judge_backend(translator, preferred=pref) != translator


def test_priority_order_constant():
    assert _JUDGE_PRIORITY == (
        "chatgpt", "deepseek", "aistudio", "grok", "copilot", "gemini",
    )
    assert set(KNOWN_BACKENDS) == {
        "gemini", "chatgpt", "aistudio", "deepseek", "grok", "copilot",
    }


# ── Backend factory ───────────────────────────────────────────────────────

def test_make_backend_returns_correct_classes():
    assert isinstance(make_backend("gemini"), GeminiBackend)
    assert isinstance(make_backend("chatgpt"), ChatGPTBackend)
    assert isinstance(make_backend("aistudio"), AIStudioBackend)
    assert isinstance(make_backend("deepseek"), DeepSeekBackend)
    assert isinstance(make_backend("grok"), GrokBackend)
    assert isinstance(make_backend("copilot"), CopilotBackend)


def test_make_backend_is_case_insensitive():
    assert isinstance(make_backend("DeepSeek"), DeepSeekBackend)


def test_make_backend_unknown_defaults_to_gemini():
    assert isinstance(make_backend("totally-unknown"), GeminiBackend)
    assert isinstance(make_backend(""), GeminiBackend)


def test_backends_registry_keys():
    assert set(_BACKENDS.keys()) == {
        "gemini", "chatgpt", "aistudio", "deepseek", "grok", "copilot",
    }


def test_deepseek_backend_url_and_interface():
    be = DeepSeekBackend()
    assert be.url == "https://chat.deepseek.com"
    for method in (
        "count_responses", "is_response_done",
        "get_last_response_text", "send_input", "start_new_chat",
    ):
        assert callable(getattr(be, method))


@pytest.mark.parametrize(
    ("name", "cls", "url"),
    [
        ("aistudio", AIStudioBackend, "https://aistudio.google.com/prompts/new_chat"),
        ("grok", GrokBackend, "https://grok.com"),
        ("copilot", CopilotBackend, "https://copilot.microsoft.com"),
    ],
)
def test_new_backend_url_and_interface(name, cls, url):
    be = cls()
    assert be.url == url
    assert isinstance(make_backend(name), cls)
    for method in (
        "count_responses", "is_response_done",
        "get_last_response_text", "send_input", "start_new_chat",
    ):
        assert callable(getattr(be, method))


def test_translator_backend_override():
    t = WebAITranslator(backend="deepseek")
    assert t._backend_name == "deepseek"
    assert isinstance(t._backend, DeepSeekBackend)


def test_translator_backend_override_case_insensitive():
    t = WebAITranslator(backend="ChatGPT")
    assert t._backend_name == "chatgpt"
    assert isinstance(t._backend, ChatGPTBackend)


# ── Fakes for async judge tests ───────────────────────────────────────────

_VALID_JSON = """```json
{"score": 85, "verdict": "good",
 "errors": [{"category": "accuracy", "severity": "major", "description": "mistranslated term"}],
 "strengths": "fluent", "suggestion": "use correct term"}
```"""


class _FakeTranslator:
    """Stand-in for WebAITranslator — returns a canned judge response.

    `raw` lets a test inject malformed output; `raise_on_send` exercises the
    exception path in judge_segment.
    """

    def __init__(self, backend=None, raw=_VALID_JSON, raise_on_send=False):
        self.backend = backend
        self._raw = raw
        self._raise = raise_on_send
        self.send_calls = 0
        self.new_chat_calls = 0
        self.cleaned = False

    async def launch_browser(self):
        return ("ctx", "page")

    async def start_new_chat(self, page):
        self.new_chat_calls += 1

    async def cleanup(self):
        self.cleaned = True

    async def _send_prompt_and_get_response(self, page, prompt):
        self.send_calls += 1
        if self._raise:
            raise RuntimeError("scrape failed")
        return self._raw


# ── judge_segment ──────────────────────────────────────────────────────────

async def test_judge_segment_recomputes_mqm_and_keeps_self_score():
    t = _FakeTranslator()
    out = await judge_segment(t, "page", "source", "translation", "chatgpt-web")
    assert out is not None
    # accuracy/major penalty = 5 → 100 - 5 = 95
    assert out["score"] == 95.0
    assert out["mqm_score"] == 95.0
    assert out["llm_self_score"] == 85        # model's own score preserved
    assert out["model"] == "chatgpt-web"
    assert t.send_calls == 1


async def test_judge_segment_unparseable_returns_none():
    t = _FakeTranslator(raw="this is not json at all")
    out = await judge_segment(t, "page", "src", "mt", "deepseek-web")
    assert out is None


async def test_judge_segment_send_failure_returns_none():
    t = _FakeTranslator(raise_on_send=True)
    out = await judge_segment(t, "page", "src", "mt", "deepseek-web")
    assert out is None


# ── judge_segments_batch ────────────────────────────────────────────────────

def _patch_translator(monkeypatch, **kwargs):
    """Patch web_judge.WebAITranslator with a fake; return the instance used."""
    created = {}

    def _factory(backend=None):
        inst = _FakeTranslator(backend=backend, **kwargs)
        created["inst"] = inst
        return inst

    import app.pdf.web_judge as wj
    monkeypatch.setattr(wj, "WebAITranslator", _factory)
    return created


async def test_batch_report_shape_and_cross_model(monkeypatch):
    created = _patch_translator(monkeypatch)
    pairs = [
        {"index": 0, "src": "Hello world", "mt": "Xin chào", "score_pct": 50},
        {"index": 1, "src": "Foo bar", "mt": "Phở bò", "score_pct": 40},
    ]
    report = await judge_segments_batch(
        pairs, judge_backend="chatgpt", translator_backend="gemini",
    )

    assert report["translator_backend"] == "gemini"
    assert report["judge_backend"] == "chatgpt"
    assert report["judge_backend"] != report["translator_backend"]
    assert report["model"] == "chatgpt-web"
    assert report["num_judged"] == 2
    assert report["avg_score"] == 95           # both segments score 95
    assert report["error_counts"] == {"accuracy": 2}
    assert len(report["results"]) == 2
    assert created["inst"].cleaned is True


async def test_batch_overrides_judge_equal_to_translator(monkeypatch):
    created = _patch_translator(monkeypatch)
    pairs = [{"index": 0, "src": "a", "mt": "b", "score_pct": 10}]
    # Ask for chatgpt as judge but translator is ALSO chatgpt → must override.
    report = await judge_segments_batch(
        pairs, judge_backend="chatgpt", translator_backend="chatgpt",
    )
    assert report["judge_backend"] != "chatgpt"
    assert report["judge_backend"] in KNOWN_BACKENDS
    assert created["inst"].backend == report["judge_backend"]


async def test_batch_empty_pairs_returns_zero_report(monkeypatch):
    _patch_translator(monkeypatch)
    report = await judge_segments_batch(
        [], judge_backend="deepseek", translator_backend="gemini",
    )
    assert report["num_judged"] == 0
    assert report["avg_score"] is None
    assert report["results"] == []


async def test_batch_respects_max_segments(monkeypatch):
    _patch_translator(monkeypatch)
    pairs = [
        {"index": i, "src": f"s{i}", "mt": f"t{i}", "score_pct": 10 + i}
        for i in range(8)
    ]
    report = await judge_segments_batch(
        pairs, judge_backend="deepseek", translator_backend="gemini",
        max_segments=3,
    )
    assert report["num_judged"] == 3
    assert len(report["results"]) == 3
