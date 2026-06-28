"""CLI kiem thu cho prototype hybrid.

Chi dung thu vien chuan (urllib) -> chay bang python nao cung duoc.

Vi du:
    # 1 job gui toi worker ChatGPT
    python test_client.py --prompt "Dich sang tieng Viet: Hello world." --backend chatgpt

    # Benchmark song song: 6 job, mo nhieu tab/worker de thay chia tai
    python test_client.py --benchmark 6 --backend any
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_SERVER = "http://localhost:8765"
DEFAULT_PROMPT = (
    "Dich sang tieng Viet, chi tra ve ban dich, khong giai thich: "
    "'Machine learning is a subset of artificial intelligence.'"
)


def _req(method: str, url: str, data: dict | None = None, timeout: float = 300):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def submit(server: str, prompt: str, backend: str) -> str:
    res = _req("POST", f"{server}/jobs", {"prompt": prompt, "backend": backend})
    return res["job_id"]


def wait(server: str, job_id: str, timeout: float = 360, poll: float = 1.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _req("GET", f"{server}/jobs/{job_id}")
        if job["status"] in ("done", "error"):
            return job
        time.sleep(poll)
    return {"status": "timeout", "id": job_id, "result": None, "error": "client wait timeout"}


def run_single(server: str, prompt: str, backend: str) -> None:
    print(f"→ submit (backend={backend}) ...")
    t0 = time.time()
    job_id = submit(server, prompt, backend)
    print(f"  job_id = {job_id} — dang cho worker xu ly "
          f"(mo tab AI + Tampermonkey neu chua) ...")
    job = wait(server, job_id)
    rt = time.time() - t0
    print("─" * 60)
    print(f"status   : {job['status']}")
    print(f"worker   : {job.get('worker_id')}")
    print(f"round-trip: {rt:.1f}s")
    if job.get("timings"):
        print(f"timings  : {job['timings']}")
    print("─" * 60)
    if job["status"] == "done":
        print(job["result"])
    else:
        print(f"LOI: {job.get('error')}")


def run_benchmark(server: str, n: int, prompt: str, backend: str) -> None:
    print(f"→ benchmark: submit {n} job (backend={backend}) cung luc ...")
    print("  (mo nhieu tab/worker de thay chia tai song song)")
    wall0 = time.time()

    job_ids = [submit(server, f"[{i+1}/{n}] {prompt}", backend) for i in range(n)]
    print(f"  da submit: {', '.join(job_ids)}")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = {ex.submit(wait, server, jid): jid for jid in job_ids}
        for fut in as_completed(futs):
            results.append(fut.result())

    wall = time.time() - wall0
    done = [r for r in results if r["status"] == "done"]
    errors = [r for r in results if r["status"] != "done"]

    print("═" * 60)
    print(f"{'job_id':14}{'status':9}{'worker':22}{'gen_ms':>8}")
    for r in sorted(results, key=lambda x: x.get("worker_id") or ""):
        gen = (r.get("timings") or {}).get("generate_ms", "")
        print(f"{r.get('id',''):14}{r['status']:9}{str(r.get('worker_id')):22}{str(gen):>8}")
    print("═" * 60)
    workers = {r.get("worker_id") for r in done}
    print(f"tong: {len(results)}  ·  done: {len(done)}  ·  loi: {len(errors)}")
    print(f"worker tham gia: {len([w for w in workers if w])}")
    print(f"wall-time: {wall:.1f}s  ·  throughput: {len(done)/wall*60:.1f} job/phut"
          if wall > 0 else "")
    print("Goi y: chi tiet timeline o logs/events.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(description="Test client cho hybrid bridge.")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--backend", default="any",
                    choices=["chatgpt", "gemini", "aistudio", "deepseek",
                             "grok", "copilot", "any"])
    ap.add_argument("--benchmark", type=int, default=0,
                    help="So job gui song song (0 = chay 1 job don)")
    args = ap.parse_args()

    try:
        _req("GET", f"{args.server}/health", timeout=5)
    except urllib.error.URLError as e:
        print(f"Khong ket noi duoc bridge tai {args.server} ({e}).")
        print("Chay truoc: python bridge_server.py")
        return

    if args.benchmark and args.benchmark > 0:
        run_benchmark(args.server, args.benchmark, args.prompt, args.backend)
    else:
        run_single(args.server, args.prompt, args.backend)


if __name__ == "__main__":
    main()
