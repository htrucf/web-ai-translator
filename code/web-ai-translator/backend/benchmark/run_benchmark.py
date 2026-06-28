#!/usr/bin/env python3
"""Benchmark harness E1-E3 - đo thời gian / tài nguyên / scaling theo num_tabs.

Chạy MultiAgentCoordinator TRỰC TIẾP (không qua HTTP để tránh dedup), lặp qua
num_tabs x repetitions. Với mỗi lần chạy:
  - lấy mẫu tài nguyên (CPU%/RAM/số tiến trình Chromium) bằng psutil ở luồng nền,
  - đọc duration_seconds + số request AI từ progress.json (eval_loop),
  - ghi 1 dòng vào CSV.

Dùng analyze.py để tổng hợp mean±std + speedup/efficiency + vẽ đồ thị.

Tiền đề: chạy TỪ thư mục backend; đã đăng nhập web AI sẵn trong browser_data
(giống pipeline thật). Mỗi lần chạy là một bản dịch đầy đủ nên tốn thời gian thật.

Ví dụ (đo scaling E1, một tài liệu cỡ vừa, single-model, tắt judge cho sạch):
  ./venv312/Scripts/python.exe benchmark/run_benchmark.py \
      --pdf workspace/samples/medium.pdf \
      --tabs 1,2,3,4,6 --reps 5 --models gemini --judge off \
      --out benchmark/results.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import threading
import time
from pathlib import Path

# Console Windows hay dùng cp1252 -> ép UTF-8 để in được tiếng Việt.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Cho phép `import app...` khi chạy script từ thư mục backend.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import psutil
except ImportError:  # psutil nằm trong requirements; cảnh báo nếu thiếu
    psutil = None


# ── Lấy mẫu tài nguyên ────────────────────────────────────────────────────────

class ResourceSampler(threading.Thread):
    """Luồng nền lấy mẫu CPU%/RAM/số tiến trình Chromium trong lúc job chạy."""

    PROC_HINTS = ("chrome", "chromium", "python")

    def __init__(self, interval: float = 1.5):
        super().__init__(daemon=True)
        self.interval = interval
        self._stop = threading.Event()
        self.cpu_samples: list[float] = []
        self.ram_peak_mb = 0.0
        self.chrome_peak = 0

    def run(self):
        if psutil is None:
            return
        psutil.cpu_percent(interval=None)  # prime, đọc lần đầu trả 0
        while not self._stop.is_set():
            try:
                self.cpu_samples.append(psutil.cpu_percent(interval=None))
                rss = 0
                n_chrome = 0
                for p in psutil.process_iter(["name", "memory_info"]):
                    name = (p.info.get("name") or "").lower()
                    if not any(h in name for h in self.PROC_HINTS):
                        continue
                    mi = p.info.get("memory_info")
                    if mi:
                        rss += mi.rss
                    if "chrome" in name or "chromium" in name:
                        n_chrome += 1
                self.ram_peak_mb = max(self.ram_peak_mb, rss / 1e6)
                self.chrome_peak = max(self.chrome_peak, n_chrome)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    @property
    def cpu_avg(self) -> float:
        return sum(self.cpu_samples) / len(self.cpu_samples) if self.cpu_samples else 0.0


# ── Đọc kết quả từ progress.json ──────────────────────────────────────────────

def read_progress(work_dir: str, job_id: str) -> dict:
    p = Path(work_dir) / "jobs" / job_id / "progress.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def metrics_from_progress(prog: dict) -> dict:
    """Rút các chỉ số quan tâm; số request = dịch + judge + glossary + style."""
    el = prog.get("eval_loop") or {}
    tx = el.get("total_translations")
    jc = el.get("total_judge_calls")
    n_req = None
    passed = flagged = None
    if el:
        n_req = (tx or 0) + (jc or 0) + 2  # +1 glossary, +1 style anchor
        passed = len(el.get("passed") or [])
        flagged = len(el.get("flagged") or [])
    return {
        "duration_seconds": prog.get("duration_seconds"),
        "total_chunks": prog.get("total_chunks"),
        "status": prog.get("status"),
        "total_translations": tx,
        "total_judge_calls": jc,
        "n_requests": n_req,
        "passed": passed,
        "flagged": flagged,
        "quality_score": (prog.get("quality") or {}).get("score"),
    }


# ── Chạy 1 job ────────────────────────────────────────────────────────────────

async def run_one(pdf_path, work_dir, mode, models, num_tabs, judge) -> tuple[str, float]:
    from app.pdf.agents import MultiAgentCoordinator

    jb = None if str(judge).lower() in ("off", "none", "") else judge
    coord = MultiAgentCoordinator(
        work_dir=work_dir,
        mode=mode,
        models=models,
        num_tabs=num_tabs,
        judge_backend=jb,
    )
    job_id = f"bench_{Path(pdf_path).stem}_k{num_tabs}_{int(time.time() * 1000)}"
    t0 = time.time()
    await coord.run(pdf_path, job_id)
    return job_id, time.time() - t0


FIELDS = [
    "pdf", "num_tabs", "rep", "wall_seconds", "duration_seconds", "total_chunks",
    "n_requests", "total_translations", "total_judge_calls", "passed", "flagged",
    "quality_score", "cpu_avg", "ram_peak_mb", "chrome_peak", "status", "job_id",
]


def main():
    ap = argparse.ArgumentParser(description="Benchmark scaling/time/resource theo num_tabs")
    ap.add_argument("--pdf", action="append", required=True,
                    help="đường dẫn PDF (lặp cờ này cho nhiều tài liệu)")
    ap.add_argument("--tabs", default="1,2,3,4,6", help="danh sách num_tabs, vd 1,2,3,4,6")
    ap.add_argument("--reps", type=int, default=5, help="số lần lặp mỗi cấu hình")
    ap.add_argument("--mode", default="standard", choices=["standard", "book"])
    ap.add_argument("--models", default="gemini", help="vd gemini hoặc gemini,chatgpt")
    ap.add_argument("--judge", default="off", help="off | web | ollama")
    ap.add_argument("--workdir", default="workspace")
    ap.add_argument("--out", default="benchmark/results.csv")
    ap.add_argument("--sample-interval", type=float, default=1.5)
    args = ap.parse_args()

    if psutil is None:
        print("[bench] CẢNH BÁO: thiếu psutil -> không đo được tài nguyên (pip install psutil)")

    tabs = [int(x) for x in args.tabs.split(",") if x.strip()]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    is_new = not out.exists()

    with out.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        for pdf in args.pdf:
            for k in tabs:
                for rep in range(args.reps):
                    print(f"[bench] {Path(pdf).name} | num_tabs={k} | rep {rep + 1}/{args.reps}")
                    sampler = ResourceSampler(args.sample_interval)
                    sampler.start()
                    try:
                        job_id, wall = asyncio.run(
                            run_one(pdf, args.workdir, args.mode, models, k, args.judge)
                        )
                    except Exception as e:
                        print(f"[bench]   LỖI: {e}")
                        sampler.stop()
                        sampler.join(timeout=3)
                        continue
                    sampler.stop()
                    sampler.join(timeout=3)

                    m = metrics_from_progress(read_progress(args.workdir, job_id))
                    row = {
                        "pdf": Path(pdf).name, "num_tabs": k, "rep": rep,
                        "wall_seconds": round(wall, 1),
                        "cpu_avg": round(sampler.cpu_avg, 1),
                        "ram_peak_mb": round(sampler.ram_peak_mb, 1),
                        "chrome_peak": sampler.chrome_peak,
                        "job_id": job_id, **m,
                    }
                    writer.writerow(row)
                    f.flush()
                    print(f"[bench]   -> dur={row.get('duration_seconds')}s "
                          f"req={row.get('n_requests')} RAM={row.get('ram_peak_mb')}MB "
                          f"chunks={row.get('total_chunks')}")
    print(f"[bench] Hoàn tất. Kết quả: {out}  ->  chạy analyze.py để tổng hợp + vẽ.")


if __name__ == "__main__":
    main()
