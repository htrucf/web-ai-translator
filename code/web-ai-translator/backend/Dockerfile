# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage build: install deps in a builder, copy only what runtime needs.
# Final image bundles Playwright Chromium + xelatex.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System deps — fonts + xelatex for LaTeX compile, libs for Playwright Chromium
# Xvfb cho phep chay Chrome headed (--headless=False) — tranh bot detection
# tu Gemini/ChatGPT (chung dung User-Agent + naviator.webdriver de phat hien).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        # LaTeX (xelatex supports UTF-8 / Vietnamese)
        texlive-xetex texlive-fonts-recommended texlive-lang-other texlive-latex-extra \
        # Fonts (Times New Roman ~ Liberation Serif, Noto for Vietnamese)
        fonts-liberation fonts-noto-core fonts-noto-cjk \
        # Playwright Chromium runtime libs
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libwayland-client0 libdrm2 \
        # Xvfb — virtual display so Chrome can run headed inside container
        xvfb xauth \
        # Utilities
        tini procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─── Python deps ──
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# ─── Playwright browsers ──
RUN python -m playwright install chromium --with-deps

# ─── App source ──
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

# Workspace + browser_data se duoc mount nhu volumes trong compose
RUN mkdir -p /data/workspace /data/browser_data /tmp/prom_multiproc

# Mac dinh cho container: chrome chay as root -> can --no-sandbox.
# Lay tu env trong translator.py (_STEALTH_ARGS append khi PLAYWRIGHT_NO_SANDBOX=1).
ENV PLAYWRIGHT_NO_SANDBOX=1 \
    BROWSER_DATA_DIR=/data/browser_data \
    DISPLAY=:99

EXPOSE 8000

# Entrypoint script switches behavior based on ROLE (api / worker / beat)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["api"]
