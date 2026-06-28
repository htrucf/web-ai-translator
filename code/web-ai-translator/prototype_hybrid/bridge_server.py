"""Bridge server cho prototype hybrid (userscript <-> backend).

Vai tro: hang doi job dich don gian + kenh giao tiep voi userscript Tampermonkey
chay trong tab ChatGPT/Copilot that. KHONG lai browser, KHONG dung Playwright/CDP
-> tranh hoan toan vector "automation-protocol fingerprinting" (Runtime.enable)
ma Cloudflare/Copilot dung de chan bot.

Luong:
    test_client (hoac pipeline sau nay)  --POST /jobs-->  hang doi
    userscript trong tab Aic that        --GET  /jobs/next--> nhan job
                                         --POST /jobs/{id}/result--> tra ket qua
    test_client                          --GET  /jobs/{id}--> doc ket qua

Day la prototype DOC LAP — chay o port rieng (8765), khong dung gi den ban
Playwright goc (backend/app). Chay:

    ./venv312/Scripts/python.exe web-ai-translator/prototype_hybrid/bridge_server.py
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ── Cau hinh ────────────────────────────────────────────────────────────────
APP_VERSION = "0.2.0"            # tang khi doi API — kiem tra qua GET /health
PORT = 8765
LONGPOLL_SECONDS = 25.0          # /jobs/next giu ket noi toi da bao lau
WORKER_ALIVE_SECONDS = 30.0      # worker poll trong khoang nay -> coi la "song"

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "events.jsonl"

Backend = Literal[
    "chatgpt", "gemini", "aistudio", "deepseek", "grok", "copilot", "any"
]


# ── Trang thai in-memory ────────────────────────────────────────────────────
# job_id -> dict(prompt, backend, status, result, error, timings, worker_id, ts_*)
_jobs: dict[str, dict[str, Any]] = {}
# worker_id -> dict(backend, last_seen, jobs_done)
_workers: dict[str, dict[str, Any]] = {}
# Bao hieu khi co job moi -> danh thuc cac /jobs/next dang cho
_cond = asyncio.Condition()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, **fields: Any) -> None:
    """Ghi 1 dong JSONL — du lieu tho de dung bi+eu do throughput/concurrency (DATN)."""
    rec = {"ts": _now_iso(), "event": event, **fields}
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # log khong duoc cung khong nen lam chet request
        print(f"[bridge] WARN: khong ghi duoc log: {e}")


def _job_matches_worker(job: dict[str, Any], worker_backend: str) -> bool:
    """Worker backend=chatgpt nhan job target chatgpt hoac any (va nguoc lai)."""
    target = job["backend"]
    return target == "any" or target == worker_backend


# ── API models ──────────────────────────────────────────────────────────────
class SubmitJob(BaseModel):
    prompt: str
    backend: Backend = "any"


class JobResult(BaseModel):
    text: str = ""
    error: Optional[str] = None
    timings: Optional[dict[str, Any]] = None


# ── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Hybrid Bridge (prototype)")

# GM_xmlhttpRequest cua userscript chay o context extension nen bo qua CORS;
# CORS o day chi can cho test_client / trang status mo tu localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": APP_VERSION,
            "jobs": len(_jobs), "workers": len(_workers)}


@app.post("/jobs")
async def create_job(req: SubmitJob) -> dict[str, str]:
    """Day 1 job dich vao hang doi. Tra ve job_id de poll ket qua."""
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {
        "id": job_id,
        "prompt": req.prompt,
        "backend": req.backend,
        "status": "pending",
        "result": None,
        "error": None,
        "timings": None,
        "worker_id": None,
        "ts_created": time.time(),
        "ts_claimed": None,
        "ts_done": None,
    }
    log_event("created", job_id=job_id, backend=req.backend,
              prompt_chars=len(req.prompt))
    async with _cond:
        _cond.notify_all()
    return {"job_id": job_id}


@app.get("/jobs/next")
async def next_job(worker_id: str, backend: str = "chatgpt") -> dict[str, Any]:
    """Userscript long-poll lay job. Tra {} neu het gio (client tu goi lai)."""
    _workers.setdefault(worker_id, {"backend": backend, "jobs_done": 0})
    _workers[worker_id]["backend"] = backend
    _workers[worker_id]["last_seen"] = time.time()

    deadline = time.monotonic() + LONGPOLL_SECONDS
    async with _cond:
        while True:
            for job in _jobs.values():
                if job["status"] == "pending" and _job_matches_worker(job, backend):
                    job["status"] = "claimed"
                    job["worker_id"] = worker_id
                    job["ts_claimed"] = time.time()
                    wait_ms = round((job["ts_claimed"] - job["ts_created"]) * 1000)
                    log_event("claimed", job_id=job["id"], backend=job["backend"],
                              worker_id=worker_id, queue_wait_ms=wait_ms)
                    return {"job_id": job["id"], "prompt": job["prompt"],
                            "backend": job["backend"]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            try:
                await asyncio.wait_for(_cond.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return {}


@app.post("/jobs/{job_id}/result")
async def submit_result(job_id: str, res: JobResult) -> dict[str, bool]:
    """Userscript tra ket qua (hoac loi) ve."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id khong ton tai")

    job["ts_done"] = time.time()
    job["timings"] = res.timings
    latency_ms = None
    if job["ts_claimed"]:
        latency_ms = round((job["ts_done"] - job["ts_claimed"]) * 1000)

    if res.error:
        job["status"] = "error"
        job["error"] = res.error
        log_event("error", job_id=job_id, backend=job["backend"],
                  worker_id=job["worker_id"], latency_ms=latency_ms,
                  error=res.error[:300])
    else:
        job["status"] = "done"
        job["result"] = res.text
        log_event("done", job_id=job_id, backend=job["backend"],
                  worker_id=job["worker_id"], latency_ms=latency_ms,
                  text_chars=len(res.text or ""))

    w = _workers.get(job["worker_id"] or "")
    if w:
        w["jobs_done"] = w.get("jobs_done", 0) + 1
    return {"ok": True}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id khong ton tai")
    return {
        "id": job["id"],
        "backend": job["backend"],
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
        "timings": job["timings"],
        "worker_id": job["worker_id"],
    }


@app.get("/", response_class=HTMLResponse)
async def status_page() -> str:
    """Trang status don gian, tu refresh — de quan sat job va worker."""
    now = time.time()

    worker_rows = ""
    for wid, w in sorted(_workers.items()):
        alive = (now - w.get("last_seen", 0)) < WORKER_ALIVE_SECONDS
        dot = "🟢" if alive else "⚪"
        worker_rows += (
            f"<tr><td>{dot} {wid}</td><td>{w.get('backend','')}</td>"
            f"<td>{w.get('jobs_done', 0)}</td></tr>"
        )
    worker_rows = worker_rows or '<tr><td colspan="3"><i>chua co worker</i></td></tr>'

    job_rows = ""
    for job in sorted(_jobs.values(), key=lambda j: j["ts_created"], reverse=True)[:50]:
        preview = (job["result"] or job["error"] or "")[:80].replace("<", "&lt;")
        job_rows += (
            f"<tr><td>{job['id']}</td><td>{job['backend']}</td>"
            f"<td><b>{job['status']}</b></td><td>{job['worker_id'] or ''}</td>"
            f"<td>{preview}</td></tr>"
        )
    job_rows = job_rows or '<tr><td colspan="5"><i>chua co job</i></td></tr>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3">
<title>Hybrid Bridge</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial;margin:24px;color:#222}}
 h2{{margin:18px 0 6px}} table{{border-collapse:collapse;width:100%}}
 td,th{{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:14px}}
 th{{background:#f5f5f5}} code{{background:#f0f0f0;padding:2px 5px;border-radius:4px}}
</style></head><body>
<h1>Hybrid Bridge — prototype</h1>
<p>Cong <code>{PORT}</code> · jobs <b>{len(_jobs)}</b> · workers <b>{len(_workers)}</b>
   · log: <code>logs/events.jsonl</code></p>
<h2>Workers</h2>
<table><tr><th>worker_id</th><th>backend</th><th>jobs_done</th></tr>{worker_rows}</table>
<h2>Jobs (50 gan nhat)</h2>
<table><tr><th>id</th><th>backend</th><th>status</th><th>worker</th><th>preview</th></tr>
{job_rows}</table>
</body></html>"""


if __name__ == "__main__":
    import uvicorn

    print(f"[bridge] v{APP_VERSION} — lang nghe tai http://localhost:{PORT}  (Ctrl+C de dung)")
    print(f"[bridge] Log su kien: {LOG_FILE}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
