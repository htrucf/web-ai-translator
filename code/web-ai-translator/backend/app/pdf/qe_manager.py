"""qe_manager.py — Quản lý model QE (COMETKiwi) cho JudgeAgent.

Dùng bởi endpoints /api/quality/qe-* để: (1) kiểm tra model đã tải về máy chưa,
(2) tải nền với tiến độ 0–100% (cho UI hỏi "tải xuống?" rồi hiện thanh tiến độ).

Tải = `comet.download_model` → snapshot weights vào HF cache. % được ước lượng
bằng cách so kích thước thư mục cache đang phình / tổng kích thước repo (lấy qua
HfApi, hoặc hằng số dự phòng). Tải chạy trong thread nền; trạng thái in-memory.
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time

from app.pdf.agents.judge_agent import (
    COMETKIWI_XL_MODEL,
    DEFAULT_COMETKIWI_MODEL,
    comet_model_for_backend,
)

# Trạng thái tải theo model id → {state, percent, message, model}
#   state ∈ idle | downloading | done | error
_PROGRESS: dict[str, dict] = {}
_LOCK = threading.Lock()

# Dự phòng khi không gọi được HfApi (offline / gated) — để tính % thô.
_KNOWN_SIZE_BYTES = {
    COMETKIWI_XL_MODEL: 13_980_000_000,        # ≈13.98 GB (checkpoint 13.9GB), GATED
    DEFAULT_COMETKIWI_MODEL: 2_260_000_000,    # ≈2.26 GB
}


def resolve_model(backend: str | None) -> str:
    return comet_model_for_backend(backend)


def comet_installed() -> bool:
    return importlib.util.find_spec("comet") is not None


def weights_present(model: str) -> bool:
    """True CHỈ khi checkpoint THẬT (model.ckpt ~GB) đã có trong cache.

    KHÔNG dùng snapshot_download(local_files_only=True) vì nó báo "có" ngay cả
    khi mới tải mỗi README/LICENSE (vỏ metadata vài KB) — dương tính giả. Ta quét
    snapshots/ tìm file checkpoint > 100MB (snapshots là symlink → blob thật)."""
    snap = os.path.join(_cache_dir_for(model), "snapshots")
    if not os.path.isdir(snap):
        return False
    for root, _dirs, files in os.walk(snap):
        for fn in files:
            if fn.endswith((".ckpt", ".bin", ".safetensors")):
                try:
                    if os.path.getsize(os.path.realpath(os.path.join(root, fn))) > 100_000_000:
                        return True
                except OSError:
                    pass
    return False


def _known_size(model: str) -> int:
    return _KNOWN_SIZE_BYTES.get(model, 3_000_000_000)


def _total_bytes(model: str) -> int:
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(model, files_metadata=True)
        total = sum((s.size or 0) for s in (info.siblings or []))
        if total > 0:
            return total
    except Exception:
        pass
    return _known_size(model)


def _cache_dir_for(model: str) -> str:
    from huggingface_hub import constants
    folder = "models--" + model.replace("/", "--")
    return os.path.join(constants.HF_HUB_CACHE, folder)


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _set(model: str, **kw) -> None:
    with _LOCK:
        _PROGRESS.setdefault(model, {"model": model}).update(kw)


def _download_worker(model: str) -> None:
    total = _total_bytes(model)
    stop = threading.Event()

    def monitor():
        cache = _cache_dir_for(model)
        while not stop.is_set():
            done = _dir_size(cache)
            pct = min(99, int(done / total * 100)) if total else 0
            _set(model, percent=pct,
                 message=f"Đang tải… {done // (1024 * 1024)}MB / ~{total // (1024 * 1024)}MB")
            time.sleep(1.0)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        from comet import download_model
        download_model(model)            # phần nặng (~14GB) — không nạp vào RAM ở đây
        stop.set()
        _set(model, state="done", percent=100, message="Hoàn tất tải model")
    except Exception as e:
        stop.set()
        msg = str(e)
        if any(k in msg.lower() for k in ("gated", "401", "403", "authoriz", "access")):
            msg = ("Model bị GATED trên HuggingFace — cần đăng nhập + chấp nhận license. "
                   "Chạy `huggingface-cli login` rồi vào trang model bấm 'Agree'. Chi tiết: " + msg)
        _set(model, state="error", percent=0, message=f"Lỗi tải: {msg}")


def start_download(backend: str) -> dict:
    """Khởi động tải nền (idempotent). Trả trạng thái hiện tại."""
    model = resolve_model(backend)
    if not comet_installed():
        return {"state": "error", "percent": 0, "model": model,
                "message": "Thiếu gói unbabel-comet (pip install unbabel-comet)"}
    if weights_present(model):
        _set(model, state="done", percent=100, message="Đã có sẵn")
        return dict(_PROGRESS[model])
    with _LOCK:
        st = _PROGRESS.get(model)
        if st and st.get("state") == "downloading":
            return dict(st)
        _PROGRESS[model] = {"state": "downloading", "percent": 0,
                            "message": "Bắt đầu tải…", "model": model}
    threading.Thread(target=_download_worker, args=(model,), daemon=True).start()
    return dict(_PROGRESS[model])


def get_download_status(backend: str) -> dict:
    model = resolve_model(backend)
    with _LOCK:
        prog = dict(_PROGRESS.get(model, {}))
    if prog:
        return prog
    if weights_present(model):
        return {"state": "done", "percent": 100, "message": "Đã có sẵn", "model": model}
    return {"state": "idle", "percent": 0, "message": "", "model": model}


def get_status(backend: str) -> dict:
    """Tổng hợp cho UI: gói + weights + tiến độ tải (nếu đang tải)."""
    model = resolve_model(backend)
    pkg = comet_installed()
    present = weights_present(model)
    with _LOCK:
        prog = dict(_PROGRESS.get(model, {}))
    return {
        "backend": backend,
        "model": model,
        "package_installed": pkg,
        "weights_present": present,
        "ready": pkg and present,
        "download_state": prog.get("state") or ("done" if present else "idle"),
        "percent": 100 if present else prog.get("percent", 0),
        "message": prog.get("message", ""),
        "approx_size_gb": round(_known_size(model) / 1e9, 1),
    }
