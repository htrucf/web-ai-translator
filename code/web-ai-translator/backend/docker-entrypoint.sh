#!/bin/bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Single entrypoint, branches on ROLE so the same image serves api/worker/beat.
# ─────────────────────────────────────────────────────────────────────────────

ROLE="${ROLE:-${1:-api}}"

# Xvfb helper — chi dung khi can chay Chrome headed trong container.
# api/beat/flower khong dung Playwright nen khong can.
start_xvfb() {
    if [[ -z "${DISPLAY:-}" ]]; then
        export DISPLAY=:99
    fi
    if ! pgrep -x Xvfb >/dev/null 2>&1; then
        echo "[entrypoint] Starting Xvfb on $DISPLAY..."
        Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension GLX +render -noreset \
            >/tmp/xvfb.log 2>&1 &
        # Wait Xvfb san sang toi da ~3s
        for _ in $(seq 1 30); do
            if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
                echo "[entrypoint] Xvfb ready on $DISPLAY"
                return 0
            fi
            sleep 0.1
        done
        echo "[entrypoint] Warning: Xvfb may not be ready, continuing anyway"
    fi
}

# Wait for Postgres
if [[ -n "${DATABASE_URL:-}" ]]; then
    echo "[entrypoint] Waiting for Postgres…"
    python -c "
import os, sys, time
import psycopg
url = os.environ['DATABASE_URL'].replace('+psycopg', '')
for i in range(60):
    try:
        psycopg.connect(url, connect_timeout=2).close()
        print(f'[entrypoint] Postgres ready after {i}s')
        sys.exit(0)
    except Exception as e:
        time.sleep(1)
print('[entrypoint] Postgres never came up', file=sys.stderr)
sys.exit(1)
"
fi

case "$ROLE" in
    api)
        echo "[entrypoint] Running migrations…"
        alembic upgrade head || echo "[entrypoint] Alembic upgrade failed — continuing anyway"
        echo "[entrypoint] Starting FastAPI (uvicorn, gunicorn workers)…"
        exec gunicorn app.main:app \
            --workers "${GUNICORN_WORKERS:-2}" \
            --worker-class uvicorn.workers.UvicornWorker \
            --bind 0.0.0.0:8000 \
            --access-logfile - \
            --error-logfile - \
            --timeout 120
        ;;
    worker)
        # Worker chay Playwright headed → can Xvfb (no real display in container)
        start_xvfb
        echo "[entrypoint] Starting Celery worker (DISPLAY=$DISPLAY)..."
        exec celery -A app.celery_app:celery_app worker \
            --loglevel="${LOG_LEVEL:-info}" \
            --concurrency="${CELERY_CONCURRENCY:-1}" \
            -Q "translate,default"
        ;;
    beat)
        echo "[entrypoint] Starting Celery beat…"
        exec celery -A app.celery_app:celery_app beat --loglevel="${LOG_LEVEL:-info}"
        ;;
    flower)
        echo "[entrypoint] Starting Flower…"
        exec celery -A app.celery_app:celery_app flower --port=5555
        ;;
    shell)
        exec /bin/bash
        ;;
    *)
        # Pass through arbitrary command (e.g. `docker compose exec backend alembic upgrade head`)
        exec "$@"
        ;;
esac
