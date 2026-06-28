"""Tests cho eval_pipeline — vòng lặp dịch ∥ đánh giá đồng thời.

Dùng fake callable (không browser/Ollama) + asyncio.run với timeout để bắt
deadlock thay vì treo vô hạn.
"""

import asyncio
from collections import defaultdict

import pytest

from app.pdf.eval_pipeline import EvalConfig, EvalPipeline


def _run(coro, timeout=10.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def _src(i):
    return f"src {i}"


# ── 1. Feedback loop: heuristic retry + judge retry, tất cả hội tụ ────────────

def test_feedback_loops_converge():
    attempts = defaultdict(int)

    async def translate(idx):
        attempts[idx] += 1
        a = attempts[idx]
        if idx == 1 and a == 1:
            return "BADH c1", True          # heuristic tệ → retry
        if idx == 2 and a == 1:
            return "v1 c2", True            # heuristic ổn nhưng judge tệ
        return f"v{a} c{idx}", True

    def heuristic(idx, text):
        return 40.0 if text.startswith("BADH") else 80.0

    judged_batch_sizes = []

    async def judge(batch):
        judged_batch_sizes.append(len(batch))
        out = {}
        for idx, _src_, mt in batch:
            out[idx] = 50.0 if (idx == 2 and mt == "v1 c2") else 85.0
        return out

    cfg = EvalConfig(
        heuristic_threshold=60, judge_threshold=70,
        judge_batch_size=2, max_attempts=3, num_workers=2,
    )
    pipe = EvalPipeline(
        list(range(6)), translate, heuristic, _src, judge, cfg,
    )
    report = _run(pipe.run())

    # Mọi chunk pass, không cái nào bị flag
    assert report.passed == [0, 1, 2, 3, 4, 5]
    assert report.flagged == []

    # 4 chunk dịch 1 lần + chunk1 (2) + chunk2 (2) = 8 lần dịch
    assert report.total_translations == 8
    assert report.total_judge_calls >= 1

    # best-so-far giữ bản đã sửa
    finals = pipe.final_translations()
    assert finals[1] == "v2 c1"
    assert finals[2] == "v2 c2"

    # batch judge không bao giờ vượt cấu hình
    assert judged_batch_sizes and max(judged_batch_sizes) <= cfg.judge_batch_size

    # có quyết định retry ở cả tầng heuristic (chunk1) và judge (chunk2)
    retried = {(r["index"], r["stage"]) for r in report.rounds
               if r["decision"] == "retry"}
    assert (1, "heuristic") in retried
    assert (2, "judge") in retried


# ── 2. Cạn max_attempts → flagged, giữ best-so-far ────────────────────────────

def test_max_attempts_flags_and_keeps_best():
    async def translate(idx):
        return "BADH", True                 # luôn heuristic tệ

    def heuristic(idx, text):
        return 40.0

    async def judge(batch):
        return {i: 85.0 for i, _, _ in batch}

    cfg = EvalConfig(heuristic_threshold=60, max_attempts=2, num_workers=2)
    pipe = EvalPipeline(list(range(3)), translate, heuristic, _src, judge, cfg)
    report = _run(pipe.run())

    assert report.passed == []
    assert report.flagged == [0, 1, 2]
    # mỗi chunk dịch đúng max_attempts lần
    assert report.total_translations == 6
    # judge không bao giờ được gọi (heuristic chặn hết)
    assert report.total_judge_calls == 0
    # vẫn giữ bản tốt nhất (dù tệ) để dựng PDF
    assert all(t == "BADH" for t in pipe.final_translations().values())


# ── 3. Judge tắt → chỉ gate heuristic ─────────────────────────────────────────

def test_judge_disabled_runs_heuristic_only():
    async def translate(idx):
        return f"ok {idx}", True

    def heuristic(idx, text):
        return 90.0

    cfg = EvalConfig(judge_enabled=False, num_workers=3)
    pipe = EvalPipeline(list(range(5)), translate, heuristic, _src,
                        judge_fn=None, config=cfg)
    report = _run(pipe.run())

    assert report.passed == [0, 1, 2, 3, 4]
    assert report.total_judge_calls == 0
    assert report.total_translations == 5


# ── 4. Cancel sớm → không treo ────────────────────────────────────────────────

def test_cancellation_stops_cleanly():
    flag = {"cancel": False}

    async def translate(idx):
        await asyncio.sleep(0.01)
        flag["cancel"] = True               # cancel sau chunk đầu
        return f"v {idx}", True

    def heuristic(idx, text):
        return 80.0

    async def judge(batch):
        return {i: 85.0 for i, _, _ in batch}

    cfg = EvalConfig(num_workers=1, judge_batch_size=2)
    pipe = EvalPipeline(list(range(10)), translate, heuristic, _src, judge,
                        cfg, is_cancelled=lambda: flag["cancel"])
    report = _run(pipe.run())

    # Dừng sạch, không xử hết 10 chunk
    assert report.cancelled is True
    assert len(report.passed) < 10


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
