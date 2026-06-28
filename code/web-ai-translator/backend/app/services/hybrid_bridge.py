"""Client async tới bridge server cho TRANSLATOR_MODE=hybrid.

Đây là lớp "transport" thay thế Playwright: thay vì lái browser qua CDP/Playwright
(bị Cloudflare/Copilot chặn vì `Runtime.enable`), pipeline đẩy prompt vào hàng đợi
của bridge; một userscript Tampermonkey chạy trong tab AI thật (đã đăng nhập) kéo
job, điền prompt, đợi trả lời, scrape kết quả gửi về. Xem prototype_hybrid/README.md.

Bridge server chạy ĐỘC LẬP (mặc định http://localhost:8765):

    ./venv312/Scripts/python.exe web-ai-translator/prototype_hybrid/bridge_server.py

Module này chỉ là HTTP client mỏng (httpx async) — KHÔNG khởi động bridge. Các
endpoint khớp với bridge_server.py:
    POST /jobs              -> {"job_id": ...}
    GET  /jobs/{id}         -> {"status": pending|claimed|done|error, "result", ...}
    GET  /health            -> {"ok", "workers", "jobs", ...}
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.config import settings


class BridgeError(RuntimeError):
    """Job báo lỗi từ phía userscript, hoặc bridge trả response không hợp lệ."""


class BridgeUnavailable(BridgeError):
    """Không kết nối được bridge, hoặc không worker nào nhận job (tab AI chưa mở)."""


def base_url() -> str:
    """Địa chỉ gốc của bridge, bỏ dấu '/' cuối."""
    return (getattr(settings, "BRIDGE_URL", "http://localhost:8765") or "").rstrip("/")


async def health(timeout: float = 5.0) -> dict:
    """Ping bridge. Raise BridgeUnavailable nếu không kết nối được."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url()}/health")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # noqa: BLE001 — gói mọi lỗi mạng thành 1 loại
        raise BridgeUnavailable(
            f"Không kết nối được bridge tại {base_url()}: {e}"
        ) from e


async def submit(prompt: str, backend: str, timeout: float = 15.0) -> str:
    """Đẩy 1 job dịch vào hàng đợi. Trả job_id để poll kết quả.

    `backend` (chatgpt/gemini/...) định tuyến job tới đúng loại tab userscript.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url()}/jobs",
                json={"prompt": prompt, "backend": backend},
            )
            resp.raise_for_status()
            return resp.json()["job_id"]
    except BridgeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise BridgeUnavailable(
            f"Không gửi được job tới bridge {base_url()}: {e}"
        ) from e


async def wait(
    job_id: str,
    timeout: float = 420.0,
    poll: float = 1.5,
    pending_timeout: float = 60.0,
) -> dict:
    """Poll tới khi job xong/lỗi/hết giờ.

    - status == "done"  -> trả nguyên dict job (có "result", "timings").
    - status == "error" -> raise BridgeError (đếm là 1 lần dịch thất bại).
    - chưa worker nào nhận trong `pending_timeout`s -> raise BridgeUnavailable
      (fail nhanh khi tab AI chưa mở, thay vì chờ trọn `timeout`).
    - quá `timeout` mà đã claimed (worker xử lý chậm) -> raise asyncio.TimeoutError.
    """
    start = time.monotonic()
    claimed = False
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            try:
                resp = await client.get(f"{base_url()}/jobs/{job_id}")
                resp.raise_for_status()
                job = resp.json()
            except Exception as e:  # noqa: BLE001 — lỗi mạng tạm thời -> thử lại
                if time.monotonic() - start > timeout:
                    raise BridgeUnavailable(
                        f"Mất kết nối bridge khi chờ job {job_id}: {e}"
                    ) from e
                await asyncio.sleep(poll)
                continue

            status = job.get("status")
            if status == "done":
                return job
            if status == "error":
                raise BridgeError(job.get("error") or f"job {job_id} báo lỗi")
            if status == "claimed":
                claimed = True

            elapsed = time.monotonic() - start
            if not claimed and elapsed > pending_timeout:
                raise BridgeUnavailable(
                    f"Job {job_id} chưa worker nào nhận sau {pending_timeout:.0f}s — "
                    "kiểm tra tab AI + userscript đã mở và đăng nhập chưa."
                )
            if elapsed > timeout:
                raise asyncio.TimeoutError(
                    f"Job {job_id} không hoàn thành trong {timeout:.0f}s"
                )
            await asyncio.sleep(poll)
