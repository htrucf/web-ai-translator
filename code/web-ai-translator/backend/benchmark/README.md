# Benchmark hiệu năng — Chương "Đánh giá kết quả"

Bộ script đo tự động cho các kịch bản **E1 (scaling theo số agent), E2 (thời gian theo độ dài), E3 (tài nguyên)** mô tả trong `thesis/latex/Chuong/5_Quality.tex`.

## Tiền đề
- Chạy **từ thư mục `web-ai-translator/backend`**.
- Đã đăng nhập web AI (Gemini/…) trong `browser_data` — giống pipeline thật.
- `psutil` (đo tài nguyên) đã có trong `requirements.txt`. `matplotlib` cần cho vẽ đồ thị: `pip install matplotlib`.
- Mỗi lần chạy là **một bản dịch đầy đủ** → tốn thời gian thật; nên bắt đầu với tài liệu ngắn + `--reps 2` để thử.

## Chạy
```bash
# E1 — scaling ít↔nhiều agent (single-model, tắt judge cho sạch)
./venv312/Scripts/python.exe benchmark/run_benchmark.py \
    --pdf workspace/samples/medium.pdf \
    --tabs 1,2,3,4,6 --reps 5 --models gemini --judge off \
    --out benchmark/results.csv

# E2 — thời gian theo độ dài (num_tabs tốt nhất từ E1)
./venv312/Scripts/python.exe benchmark/run_benchmark.py \
    --pdf short.pdf --pdf medium.pdf --pdf long.pdf \
    --tabs 3 --reps 5 --out benchmark/results.csv

# Tổng hợp + vẽ (mean±std, speedup, efficiency, throughput)
./venv312/Scripts/python.exe benchmark/analyze.py \
    --in benchmark/results.csv --out benchmark/aggregated.csv --plots benchmark/plots
```

## Đầu ra
- `results.csv` — 1 dòng / lần chạy (raw, append được nhiều phiên).
- `aggregated.csv` — mean±std, speedup, efficiency, throughput, request, RAM/CPU → **điền thẳng vào bảng LaTeX** (Bảng `tab:eval5_scaling`, `tab:eval5_time`, `tab:eval5_resource`).
- `plots/` — `speedup_vs_tabs.png`, `efficiency_vs_tabs.png`, `time_vs_tabs.png`, `ram_vs_tabs.png`.

## Nguồn số liệu
- `duration_seconds`, `total_chunks` ← `progress.json`.
- số request AI ← `progress.json["eval_loop"]` (`total_translations` + `total_judge_calls` + glossary + style).
- CPU/RAM/Chromium ← lấy mẫu `psutil` ở luồng nền trong lúc chạy.

## Lưu ý
- Để đo **chi phí (E6)**: chạy lại với `--judge web` và so request giữa `off` vs `web`.
- Để đo **tải đồng thời (E4/E5)**: chạy nhiều tiến trình `run_benchmark.py` song song (mỗi tiến trình 1 "người"), hoặc bắn API `/api/pdf-translate/upload` đồng thời.
- Web AI phi xác định → luôn `--reps ≥ 5`, ghi nhận giờ chạy.
