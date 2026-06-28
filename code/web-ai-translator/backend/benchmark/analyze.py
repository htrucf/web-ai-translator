#!/usr/bin/env python3
"""Tổng hợp results.csv -> mean±std, speedup/efficiency, throughput + vẽ đồ thị.

  Speedup  S(k) = T(1) / T(k)        (baseline là num_tabs=1, theo từng PDF)
  Efficiency E(k) = S(k) / k
  Throughput      = số chunk / (thời gian phút)

Xuất aggregated.csv (điền thẳng vào bảng LaTeX) + 4 đồ thị PNG.

Ví dụ:
  ./venv312/Scripts/python.exe benchmark/analyze.py \
      --in benchmark/results.csv --out benchmark/aggregated.csv --plots benchmark/plots
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

# Console Windows hay dùng cp1252 -> ép UTF-8 để in được tiếng Việt.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _mean_opt(values):
    xs = [v for v in values if v is not None]
    return statistics.mean(xs) if xs else None


def _round(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else ""


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def aggregate(rows: list[dict], time_field: str = "duration_seconds") -> dict:
    """Gom theo (pdf, num_tabs) -> thống kê. Ưu tiên duration_seconds, fallback wall."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        t = _fnum(r.get(time_field)) or _fnum(r.get("wall_seconds"))
        if t is None:
            continue
        groups[(r["pdf"], int(r["num_tabs"]))].append({
            "t": t,
            "req": _fnum(r.get("n_requests")),
            "ram": _fnum(r.get("ram_peak_mb")),
            "cpu": _fnum(r.get("cpu_avg")),
            "chunks": _fnum(r.get("total_chunks")),
            "q": _fnum(r.get("quality_score")),
        })

    agg: dict[tuple, dict] = {}
    for key, items in groups.items():
        ts = [i["t"] for i in items]
        agg[key] = {
            "n": len(ts),
            "t_mean": statistics.mean(ts),
            "t_std": statistics.pstdev(ts) if len(ts) > 1 else 0.0,
            "req_mean": _mean_opt(i["req"] for i in items),
            "ram_mean": _mean_opt(i["ram"] for i in items),
            "cpu_mean": _mean_opt(i["cpu"] for i in items),
            "chunks": _mean_opt(i["chunks"] for i in items),
            "q_mean": _mean_opt(i["q"] for i in items),
        }
    return agg


def with_derived(agg: dict) -> dict:
    """Thêm speedup/efficiency (baseline k=1 theo từng pdf) + throughput."""
    base = {pdf: v["t_mean"] for (pdf, k), v in agg.items() if k == 1}
    out = {}
    for (pdf, k), v in agg.items():
        b = base.get(pdf)
        speedup = (b / v["t_mean"]) if (b and v["t_mean"]) else None
        throughput = (v["chunks"] / (v["t_mean"] / 60)) if v["chunks"] else None
        out[(pdf, k)] = {
            **v,
            "speedup": speedup,
            "efficiency": (speedup / k) if speedup else None,
            "throughput": throughput,
        }
    return out


def write_csv(agg: dict, out: str):
    fields = ["pdf", "num_tabs", "n", "t_mean_s", "t_std_s", "speedup", "efficiency",
              "throughput_chunks_min", "req_mean", "ram_mean_mb", "cpu_mean_pct", "quality"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for (pdf, k) in sorted(agg):
            v = agg[(pdf, k)]
            w.writerow([
                pdf, k, v["n"], _round(v["t_mean"], 1), _round(v["t_std"], 1),
                _round(v["speedup"]), _round(v["efficiency"]), _round(v["throughput"], 1),
                _round(v["req_mean"], 1), _round(v["ram_mean"], 1),
                _round(v["cpu_mean"], 1), _round(v["q_mean"], 1),
            ])
    print(f"[analyze] đã ghi {out}")


def make_plots(agg: dict, outdir: str):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[analyze] thiếu matplotlib -> bỏ qua vẽ (pip install matplotlib)")
        return

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    pdfs = sorted({pdf for (pdf, _k) in agg})

    def series(pdf, key):
        ks = sorted(k for (p, k) in agg if p == pdf)
        return ks, [agg[(pdf, k)].get(key) for k in ks]

    def _save(name, ylabel, title, key, extra=None):
        plt.figure()
        for pdf in pdfs:
            ks, ys = series(pdf, key)
            plt.plot(ks, ys, marker="o", label=pdf)
        if extra:
            extra(plt)
        plt.xlabel("num_tabs (số agent song song)")
        plt.ylabel(ylabel)
        plt.title(title)
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.savefig(out / name, dpi=150, bbox_inches="tight")
        plt.close()

    ks_all = sorted({k for (_p, k) in agg})
    _save("speedup_vs_tabs.png", "Speedup S(k)", "Speedup theo số agent song song",
          "speedup", extra=lambda plt: plt.plot(ks_all, ks_all, "k--", alpha=0.4,
                                                 label="lý tưởng (tuyến tính)"))
    _save("efficiency_vs_tabs.png", "Efficiency E(k)=S(k)/k", "Hiệu suất song song",
          "efficiency", extra=lambda plt: plt.axhline(1.0, color="k", ls="--", alpha=0.4))
    _save("time_vs_tabs.png", "Thời gian (s)", "Thời gian theo số agent", "t_mean")
    _save("ram_vs_tabs.png", "RAM đỉnh (MB)", "Tài nguyên RAM theo số agent", "ram_mean")
    print(f"[analyze] đã lưu đồ thị vào {out}/")


def main():
    ap = argparse.ArgumentParser(description="Tổng hợp + vẽ kết quả benchmark")
    ap.add_argument("--in", dest="inp", default="benchmark/results.csv")
    ap.add_argument("--out", default="benchmark/aggregated.csv")
    ap.add_argument("--plots", default="benchmark/plots")
    ap.add_argument("--time-field", default="duration_seconds")
    args = ap.parse_args()

    rows = load(args.inp)
    if not rows:
        print(f"[analyze] không có dữ liệu trong {args.inp}")
        return
    agg = with_derived(aggregate(rows, args.time_field))
    write_csv(agg, args.out)
    make_plots(agg, args.plots)


if __name__ == "__main__":
    main()
