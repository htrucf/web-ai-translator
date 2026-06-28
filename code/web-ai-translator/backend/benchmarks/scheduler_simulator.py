"""Offline simulator that compares the 4 schedulers on the same workload.

Why a simulator?
  Hitting Gemini live is slow (each chunk = real browser request), unreliable
  (Google's UI shifts under us), and not reproducible (different accounts
  rate-limit at different moments). For a comparative study we need the
  *same* workload replayed across all strategies — that's only achievable
  in-process.

Model:
  - N simulated accounts, each with three failure dimensions:
      * baseline latency (Gaussian)
      * soft-ban probability per request, raised after a "stress" threshold
      * recovery time after cooldown (uniform jitter)
  - A workload of M jobs, each with C chunks. Workers pull jobs FIFO and
    process one chunk at a time. Chunk processing time = latency + tax for
    cooldown waits.

Outputs:
  - JSON file with per-strategy metrics
  - Pretty terminal table
  - Optional CSV for offline plotting

Run:
    python -m benchmarks.scheduler_simulator --workload medium --seed 42
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# Add backend root to sys.path so `app.*` imports work when run as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.pools.account_history import AccountHistory   # noqa: E402
from app.pools.schedulers import (                      # noqa: E402
    SchedulerContext,
    build_scheduler,
    list_strategies,
)


# ── Workload presets ────────────────────────────────────────────────────────

PRESETS = {
    "small":  {"accounts": 3, "jobs": 10, "chunks_per_job": 8,  "workers": 2},
    "medium": {"accounts": 5, "jobs": 30, "chunks_per_job": 12, "workers": 3},
    "large":  {"accounts": 8, "jobs": 60, "chunks_per_job": 20, "workers": 5},
}


# ── Simulated account model ─────────────────────────────────────────────────

@dataclass
class SimAccount:
    email: str
    base_latency: float           # seconds per chunk, mean
    latency_jitter: float         # std dev
    ban_prob_base: float          # per-chunk soft-ban probability when fresh
    ban_prob_stressed: float      # when used >stress_threshold chunks in a row
    stress_threshold: int         # consecutive uses before "stressed"
    recovery_min: float           # min cooldown duration (sim seconds)
    recovery_max: float           # max cooldown duration

    # Mutable runtime state
    consecutive_uses: int = 0
    cooldown_until: float = 0.0
    in_use: bool = False

    def is_free(self, now: float) -> bool:
        return (not self.in_use) and (now >= self.cooldown_until)

    def is_alive(self, now: float) -> bool:
        return now >= self.cooldown_until

    def ban_prob(self) -> float:
        if self.consecutive_uses >= self.stress_threshold:
            return self.ban_prob_stressed
        return self.ban_prob_base


def make_accounts(n: int, rng: random.Random) -> list[SimAccount]:
    """Heterogeneous accounts — some fragile, some robust, like real life."""
    accounts = []
    for i in range(n):
        # Inject diversity: each account has slightly different parameters.
        accounts.append(SimAccount(
            email=f"acct{i+1}@sim",
            base_latency=rng.uniform(3.0, 8.0),
            latency_jitter=rng.uniform(0.5, 2.0),
            ban_prob_base=rng.uniform(0.005, 0.02),
            ban_prob_stressed=rng.uniform(0.10, 0.35),
            stress_threshold=rng.randint(4, 10),
            recovery_min=rng.uniform(60.0, 180.0),
            recovery_max=rng.uniform(300.0, 900.0),
        ))
    return accounts


# ── Simulator core ──────────────────────────────────────────────────────────

@dataclass
class SimResult:
    strategy: str
    total_chunks: int
    successful_chunks: int
    failed_chunks: int
    jobs_total: int
    jobs_completed: int
    sim_duration: float
    throughput_per_hour: float
    account_survival: float        # fraction of accounts still usable at end
    per_account_uses: dict[str, int] = field(default_factory=dict)
    per_account_fails: dict[str, int] = field(default_factory=dict)
    chunk_latencies: list[float] = field(default_factory=list)
    cooldown_events: int = 0

    def latency_p50(self) -> float:
        return statistics.median(self.chunk_latencies) if self.chunk_latencies else 0.0

    def latency_p95(self) -> float:
        if not self.chunk_latencies:
            return 0.0
        s = sorted(self.chunk_latencies)
        return s[int(len(s) * 0.95) - 1] if len(s) > 1 else s[0]

    def to_row(self) -> dict:
        attempts = self.successful_chunks + self.failed_chunks
        first_try_rate = self.successful_chunks / max(1, attempts)
        return {
            "strategy": self.strategy,
            "throughput_per_hour": round(self.throughput_per_hour, 2),
            "completion_rate": round(self.jobs_completed / max(1, self.jobs_total), 4),
            "survival_rate": round(self.account_survival, 4),
            "first_try_rate": round(first_try_rate, 4),
            "p50_latency": round(self.latency_p50(), 2),
            "p95_latency": round(self.latency_p95(), 2),
            "cooldowns": self.cooldown_events,
            "sim_duration_h": round(self.sim_duration / 3600, 2),
        }


def simulate(
    strategy_name: str,
    accounts: list[SimAccount],
    jobs: int,
    chunks_per_job: int,
    workers: int,
    seed: int,
) -> SimResult:
    """Discrete-event style sim — workers race to grab chunks; sim clock is
    the max of any worker's local clock. Approximation good enough for
    relative comparison between strategies."""

    rng = random.Random(seed)
    scheduler = build_scheduler(strategy_name)
    history = AccountHistory(redis_client=None)

    # Reset account state — different strategies must start fresh.
    accs = {a.email: SimAccount(**{**a.__dict__}) for a in accounts}
    for a in accs.values():
        a.consecutive_uses = 0
        a.cooldown_until = 0.0
        a.in_use = False

    # Job queue: each job has a list of chunk indices. Worker pulls 1 chunk
    # at a time so multiple workers can interleave (mimics async pipeline).
    chunk_queue: list[tuple[int, int]] = []  # (job_idx, chunk_idx)
    for j in range(jobs):
        for c in range(chunks_per_job):
            chunk_queue.append((j, c))

    jobs_completed_chunks = {j: 0 for j in range(jobs)}

    # Worker simulation: each worker has its own clock. They share the
    # account pool. The "global" sim time = max(worker.clock).
    worker_clocks = [0.0] * workers
    total_chunks = len(chunk_queue)
    successful = 0
    failed = 0
    cooldowns = 0
    latencies: list[float] = []
    per_uses: dict[str, int] = {a: 0 for a in accs}
    per_fails: dict[str, int] = {a: 0 for a in accs}

    # Round-robin worker stepping ensures fair scheduling between workers.
    while chunk_queue:
        progressed_this_pass = False
        for w in range(workers):
            if not chunk_queue:
                break
            now = worker_clocks[w]
            free = [e for e, a in accs.items() if a.is_free(now)]
            if not free:
                # Fast-forward this worker to the next account recovery.
                next_recovery = min(a.cooldown_until for a in accs.values() if a.cooldown_until > now)
                worker_clocks[w] = next_recovery
                progressed_this_pass = True
                continue
            stats_snap = history.get_all(free)
            ctx = SchedulerContext(free=free, stats=stats_snap, now=now)
            pick = scheduler.pick(ctx)
            if pick is None:
                worker_clocks[w] += 0.1
                continue

            acc = accs[pick]
            acc.in_use = True
            history.touch(pick)

            # Simulate the chunk processing
            latency = max(0.5, rng.gauss(acc.base_latency, acc.latency_jitter))
            ban = rng.random() < acc.ban_prob()

            worker_clocks[w] += latency
            latencies.append(latency)
            per_uses[pick] += 1

            if ban:
                # Cooldown: account becomes unusable for a while.
                recovery = rng.uniform(acc.recovery_min, acc.recovery_max)
                acc.cooldown_until = worker_clocks[w] + recovery
                acc.consecutive_uses = 0
                acc.in_use = False
                history.record_cooldown(pick)
                cooldowns += 1
                per_fails[pick] += 1
                failed += 1
                # Banned chunks get re-queued — the workload still needs to
                # finish, just on a different account.
                # (Don't pop; we'll re-pop below in non-fail branch.)
            else:
                # Success — chunk completes.
                acc.consecutive_uses += 1
                acc.in_use = False
                history.record_outcome(pick, success=True, latency=latency)
                successful += 1
                job_idx, _ = chunk_queue.pop(0)
                jobs_completed_chunks[job_idx] += 1
                progressed_this_pass = True
                continue

            # If we got here, the chunk failed — leave it at the head of the
            # queue so it retries on the next worker turn.
            progressed_this_pass = True

        if not progressed_this_pass:
            # All workers stuck — advance the slowest one to next recovery.
            stuck_idx = min(range(workers), key=lambda i: worker_clocks[i])
            next_recovery = min(
                (a.cooldown_until for a in accs.values() if a.cooldown_until > worker_clocks[stuck_idx]),
                default=worker_clocks[stuck_idx] + 1.0,
            )
            worker_clocks[stuck_idx] = next_recovery

    sim_duration = max(worker_clocks)
    jobs_completed = sum(1 for j, c in jobs_completed_chunks.items() if c >= chunks_per_job)
    survival = sum(1 for a in accs.values() if a.is_alive(sim_duration)) / len(accs)
    throughput_per_hour = (successful / sim_duration) * 3600 if sim_duration > 0 else 0.0

    return SimResult(
        strategy=strategy_name,
        total_chunks=total_chunks,
        successful_chunks=successful,
        failed_chunks=failed,
        jobs_total=jobs,
        jobs_completed=jobs_completed,
        sim_duration=sim_duration,
        throughput_per_hour=throughput_per_hour,
        account_survival=survival,
        per_account_uses=per_uses,
        per_account_fails=per_fails,
        chunk_latencies=latencies,
        cooldown_events=cooldowns,
    )


# ── Reporting ───────────────────────────────────────────────────────────────

def print_table(results: list[SimResult]) -> None:
    cols = [
        ("strategy", 16),
        ("throughput_per_hour", 12),
        ("completion_rate", 10),
        ("survival_rate", 10),
        ("first_try_rate", 10),
        ("p50_latency", 10),
        ("p95_latency", 10),
        ("cooldowns", 10),
        ("sim_duration_h", 10),
    ]
    header = " | ".join(name.ljust(w) for name, w in cols)
    print("\n" + header)
    print("-" * len(header))
    rows = [r.to_row() for r in results]
    for row in rows:
        line = " | ".join(str(row[name]).ljust(w) for name, w in cols)
        print(line)
    print()


def save_outputs(results: list[SimResult], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": [r.to_row() for r in results],
        "per_account_uses": {r.strategy: r.per_account_uses for r in results},
        "per_account_fails": {r.strategy: r.per_account_fails for r in results},
    }
    json_path = os.path.join(out_dir, "scheduler_benchmark.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    csv_path = os.path.join(out_dir, "scheduler_benchmark.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        rows = [r.to_row() for r in results]
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"[+] Wrote {json_path}")
    print(f"[+] Wrote {csv_path}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Compare scheduling strategies on a simulated workload")
    ap.add_argument("--workload", choices=list(PRESETS), default="medium")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--strategies", nargs="*", default=list_strategies(),
                    help="Subset of strategies to benchmark")
    ap.add_argument("--out-dir", default=os.path.join(_HERE, "results"))
    args = ap.parse_args()

    cfg = PRESETS[args.workload]
    print(f"[*] Workload: {args.workload} -> {cfg}")
    print(f"[*] Seed: {args.seed}")
    print(f"[*] Strategies: {args.strategies}")

    rng = random.Random(args.seed)
    accounts = make_accounts(cfg["accounts"], rng)

    results: list[SimResult] = []
    for s in args.strategies:
        print(f"[*] Simulating {s} ...")
        # Use a per-strategy derived seed so each run sees the same
        # randomness AT each step — fairness across strategies.
        res = simulate(
            strategy_name=s,
            accounts=accounts,
            jobs=cfg["jobs"],
            chunks_per_job=cfg["chunks_per_job"],
            workers=cfg["workers"],
            seed=args.seed,
        )
        results.append(res)

    print_table(results)
    save_outputs(results, args.out_dir)


if __name__ == "__main__":
    main()
