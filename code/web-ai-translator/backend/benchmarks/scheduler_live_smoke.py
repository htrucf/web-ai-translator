"""Live smoke test against real Gemini accounts.

DANGER: this script consumes real account quota. Use it sparingly — its
purpose is to validate that the scheduler's choices line up with what
actually happens against the live web UI, not to gather statistically
significant data.

What it does:
  - Picks a tiny workload (default 6 chunks total).
  - Cycles through each strategy, sending the same chunks through Gemini.
  - Records: per-chunk latency, success/fail, cooldown events.
  - Writes a JSON report next to the simulator's output for easy comparison.

Prereqs:
  - GEMINI_ACCOUNTS_FILE env var pointing at a JSON list of accounts
  - Browser profiles already logged into Gemini

Run:
    python -m benchmarks.scheduler_live_smoke --strategies cooldown_aware adaptive --chunks 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.pools import get_account_pool                    # noqa: E402
from app.pools.account_history import get_account_history  # noqa: E402
from app.pools.schedulers import list_strategies           # noqa: E402

logger = logging.getLogger("scheduler_live_smoke")

# Short, deterministic sample chunks so cross-strategy comparison is fair.
SAMPLE_CHUNKS = [
    "Translate to Vietnamese: The convolutional neural network learns hierarchical features.",
    "Translate to Vietnamese: Gradient descent minimises the loss function over training data.",
    "Translate to Vietnamese: Attention mechanisms allow models to focus on relevant tokens.",
    "Translate to Vietnamese: Backpropagation computes gradients via the chain rule.",
    "Translate to Vietnamese: Regularisation techniques prevent overfitting on training data.",
    "Translate to Vietnamese: Transfer learning reuses pretrained weights for downstream tasks.",
    "Translate to Vietnamese: Self-supervised pretraining produces strong representations.",
    "Translate to Vietnamese: Layer normalisation stabilises gradients in deep networks.",
]


async def run_chunk(translator, prompt: str) -> tuple[bool, float]:
    """Translate one chunk via the existing translator. Returns (success, latency)."""
    t0 = time.time()
    try:
        result = await translator.translate(prompt)
        latency = time.time() - t0
        ok = bool(result) and len(result.strip()) > 5
        return ok, latency
    except Exception as e:
        logger.warning("translate failed: %s", e)
        return False, time.time() - t0


async def smoke_one_strategy(strategy: str, n_chunks: int) -> dict:
    """Run n_chunks through the pool using the named strategy."""
    pool = get_account_pool()
    pool.set_scheduler(strategy)
    history = get_account_history()

    # Import lazily so the module can be imported without Playwright installed.
    from app.services.translator import GeminiTranslator   # noqa: E402

    per_chunk: list[dict] = []
    cooldowns = 0
    t_start = time.time()

    for i in range(n_chunks):
        prompt = SAMPLE_CHUNKS[i % len(SAMPLE_CHUNKS)]
        acc = pool.acquire(worker_id=f"smoke-{strategy}", timeout=60.0)
        if acc is None:
            per_chunk.append({"i": i, "account": None, "ok": False, "latency": None, "reason": "no_account"})
            continue

        translator = GeminiTranslator(profile_dir=acc.profile_dir)
        try:
            await translator.start()
            ok, latency = await run_chunk(translator, prompt)
            history.record_outcome(acc.email, success=ok, latency=latency)
            per_chunk.append({"i": i, "account": acc.email, "ok": ok, "latency": round(latency, 2)})
            if not ok:
                # Conservative: treat any failure as a soft signal but only
                # trigger cooldown if we suspect a rate-limit (very long
                # latency or specific error not propagated here).
                if latency > 30.0:
                    pool.cooldown(acc.email, reason="suspected_rate_limit")
                    cooldowns += 1
        finally:
            try:
                await translator.stop()
            except Exception:
                pass
            pool.release(acc.email, f"smoke-{strategy}")

    duration = time.time() - t_start
    succ = sum(1 for r in per_chunk if r["ok"])
    return {
        "strategy": strategy,
        "duration_s": round(duration, 2),
        "chunks": n_chunks,
        "successes": succ,
        "failures": n_chunks - succ,
        "cooldowns": cooldowns,
        "throughput_per_hour": round((succ / duration) * 3600, 2) if duration > 0 else 0.0,
        "per_chunk": per_chunk,
    }


async def main_async(args):
    results = []
    for strat in args.strategies:
        print(f"[*] Running live smoke with strategy={strat} ({args.chunks} chunks) ...")
        try:
            res = await smoke_one_strategy(strat, args.chunks)
            results.append(res)
            print(f"    {strat}: {res['successes']}/{res['chunks']} ok, "
                  f"{res['throughput_per_hour']} chunks/h, cooldowns={res['cooldowns']}")
        except Exception as e:
            logger.exception("smoke for %s failed", strat)
            results.append({"strategy": strat, "error": str(e)})

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "scheduler_live_smoke.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
        }, f, indent=2)
    print(f"[+] Wrote {out_path}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Live smoke test for schedulers — uses real accounts")
    ap.add_argument("--strategies", nargs="*", default=list_strategies())
    ap.add_argument("--chunks", type=int, default=6, help="Number of chunks per strategy")
    ap.add_argument("--out-dir", default=os.path.join(_HERE, "results"))
    args = ap.parse_args()

    if not os.getenv("GEMINI_ACCOUNTS_FILE"):
        print("WARNING: GEMINI_ACCOUNTS_FILE is not set — will fall back to single anonymous account.")
        print("         Live smoke is only meaningful with 2+ accounts.")

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
