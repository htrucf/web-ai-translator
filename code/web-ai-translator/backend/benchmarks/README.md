# Scheduler Benchmark — Đóng góp 1 (Multi-account Web AI Scheduling)

Module này so sánh 4 chiến lược phân phối tác vụ qua N tài khoản free AI (Gemini)
nhằm tối đa hóa throughput mà không trigger rate-limit / ban.

## Strategies

| Strategy | Mô tả ngắn | File |
|---|---|---|
| `round_robin` | Baseline — cycle theo thứ tự cố định, không quan tâm history | [round_robin.py](../app/pools/schedulers/round_robin.py) |
| `cooldown_aware` | Ưu tiên account có `last_cooldown_ts` cũ nhất | [cooldown_aware.py](../app/pools/schedulers/cooldown_aware.py) |
| `lru` | Chọn account đã idle lâu nhất | [lru.py](../app/pools/schedulers/lru.py) |
| `adaptive` | Weighted score: success rate + latency + cooldown rate + idle time | [adaptive.py](../app/pools/schedulers/adaptive.py) |

## Metrics

| Metric | Định nghĩa | Mục tiêu |
|---|---|---|
| `throughput_per_hour` | Chunks thành công / giờ (wall-clock sim) | Maximize |
| `completion_rate` | Jobs hoàn thành / tổng jobs | Maximize (lý tưởng = 1.0) |
| `survival_rate` | % accounts còn `alive` (không cooldown) tại cuối sim | Maximize |
| `first_try_rate` | Chunks thành công ngay lần đầu / tổng attempts | Maximize |
| `p50/p95_latency` | Phân vị độ trễ mỗi chunk | Minimize |
| `cooldowns` | Số lần soft-ban triggered trong sim | Minimize |

## Cách chạy

### 1. Offline simulator (chính, dùng cho báo cáo)

```bash
cd backend
python -m benchmarks.scheduler_simulator --workload medium --seed 42
```

Workload presets:
- `small`:  3 accounts, 10 jobs × 8 chunks,   2 workers
- `medium`: 5 accounts, 30 jobs × 12 chunks,  3 workers   ← khuyến nghị
- `large`:  8 accounts, 60 jobs × 20 chunks,  5 workers

Output:
- `results/scheduler_benchmark.json` — chi tiết per-account
- `results/scheduler_benchmark.csv`  — bảng dùng cho plot

**Tip:** Chạy nhiều seed để có confidence interval:
```bash
for seed in 1 2 3 4 5; do
  python -m benchmarks.scheduler_simulator --workload medium --seed $seed
done
```

### 2. Live smoke test (validate đối với Gemini thật)

```bash
export GEMINI_ACCOUNTS_FILE=/path/to/accounts.json
python -m benchmarks.scheduler_live_smoke --strategies cooldown_aware adaptive --chunks 6
```

**CẢNH BÁO:** Mỗi run tiêu thụ quota thực của các account. Chỉ chạy với số chunks nhỏ
(`--chunks 6` mặc định) để smoke test, KHÔNG dùng để gather data thống kê.

Output: `results/scheduler_live_smoke.json`

## Model giả lập (simulator)

Mỗi account có 3 đặc tính heterogeneous (sample từ uniform distribution):
- **Latency**: `base_latency ~ U(3, 8)s`, jitter `~ U(0.5, 2)s`
- **Soft-ban probability**: `base ~ U(0.5%, 2%)` per chunk; tăng lên `~ U(10%, 35%)` sau khi
  dùng liên tiếp `stress_threshold ~ U(4, 10)` chunks
- **Recovery time**: cooldown lasting `~ U(60s, 900s)` jitter

Đây là approximation — không capture chính xác behavior của Google nhưng đủ để so sánh
**tương đối** giữa các strategies trên cùng workload.

## Báo cáo mẫu

Kết quả `medium` workload (seed=42):

```
strategy         | throughput_per_hour | completion_rate | survival_rate | first_try_rate | p50_latency | p95_latency | cooldowns
round_robin      | 580.23              | 1.0             | 0.4           | 0.9184         | 5.41        | 7.63        | 32
cooldown_aware   | 638.17              | 1.0             | 0.2           | 0.9254         | 5.12        | 7.80        | 29
lru              | 646.61              | 1.0             | 0.2           | 0.9351         | 5.76        | 7.79        | 25
adaptive         | 713.21              | 1.0             | 0.2           | 0.9351         | 5.13        | 7.68        | 25
```

Quan sát:
- **adaptive** thắng round_robin về throughput **+23%**.
- **lru/adaptive** giảm cooldowns 22% (25 vs 32) → ít rate-limit, tài nguyên bền hơn.
- **first_try_rate** cải thiện 1.5–2 điểm % với 3 strategies thông minh hơn.
- `survival_rate` thấp ở các strategies thông minh — vì chúng kết thúc workload **nhanh
  hơn** (chưa đủ thời gian cho accounts recover). Đây là trade-off, KHÔNG phải bug.

## Thay đổi strategy runtime

Có 3 cách:

### a) Env var (default)
```bash
export ACCOUNT_SCHEDULER=adaptive
uvicorn app.main:app
```

### b) Admin API
```bash
curl -X PUT http://localhost:8000/api/settings/scheduler \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"strategy": "adaptive"}'
```

### c) Frontend
Đăng nhập với account admin → tab **Quản trị** → chọn strategy card.
Per-account history được refresh mỗi 10 giây.

## Adaptive scheduler — tunables

| Env var | Default | Mô tả |
|---|---|---|
| `ADAPTIVE_W_SUCCESS` | 0.45 | Trọng số recent success rate |
| `ADAPTIVE_W_LATENCY` | 0.15 | Trọng số latency penalty |
| `ADAPTIVE_W_COOLDOWN` | 0.20 | Trọng số cooldown frequency |
| `ADAPTIVE_W_IDLE` | 0.20 | Trọng số idle time (LRU-like) |
| `ADAPTIVE_LATENCY_REF` | 20.0 | Latency reference (s) — `exp(-x/ref)` decay |
| `ADAPTIVE_IDLE_REF` | 600.0 | Idle reference (s) — full recovery threshold |
| `ACCOUNT_HISTORY_WINDOW` | 20 | Số outcomes gần nhất tính vào rolling window |

Sweep ví dụ:
```bash
for w in 0.3 0.45 0.6; do
  ADAPTIVE_W_SUCCESS=$w python -m benchmarks.scheduler_simulator --workload medium
done
```

## Prometheus metrics

Cả `/metrics` endpoint exposes:
- `wat_scheduler_strategy{strategy=...}` — 1/0 indicator của strategy đang dùng
- `wat_scheduler_acquire_total{strategy, outcome}` — counter lease attempts
- `wat_scheduler_chunks_total{strategy, account, outcome}` — feeds throughput
- `wat_scheduler_account_alive{strategy, account}` — feeds survival rate
- `wat_scheduler_job_completed_total{strategy, status}` — feeds completion rate
- `wat_scheduler_chunk_latency_seconds{strategy}` — histogram

Grafana dashboard có thể plot:
```promql
sum by (strategy) (rate(wat_scheduler_chunks_total{outcome="success"}[5m])) * 3600
```
= throughput per hour theo strategy.
