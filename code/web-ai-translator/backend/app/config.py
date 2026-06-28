"""Cấu hình ứng dụng."""

import json
import os
from dotenv import load_dotenv

from app import paths

load_dotenv()

_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "..", "runtime_settings.json")
SUPPORTED_AI_BACKENDS = (
    "gemini", "chatgpt", "aistudio", "deepseek", "grok", "copilot",
)
SUPPORTED_TARGET_BROWSERS = ("chrome", "chromium")


def _load_runtime() -> dict:
    try:
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_runtime(data: dict):
    try:
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class Settings:
    # Resolved at import time via app.paths — picks up WORKSPACE_DIR env var,
    # then OS user-data dir when packaged, then ./workspace beside backend/ in dev.
    WORKSPACE_DIR: str = paths.workspace_dir()
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # Comma-separated list of origins allowed to send credentialed requests.
    # Default covers Vite dev server only — production deployments must set this
    # explicitly. Wildcard "*" is intentionally NOT supported alongside
    # `allow_credentials=True` (browser CORS spec violation).
    CORS_ORIGINS: str = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )

    # Translator mode:
    #   "new_browser" (Playwright Chromium) | "cdp" (attach to user's Chrome) |
    #   "hybrid"      (không lái browser — đẩy job qua bridge cho userscript
    #                  Tampermonkey trong tab AI thật; xem prototype_hybrid/)
    TRANSLATOR_MODE: str = os.getenv("TRANSLATOR_MODE", "new_browser")
    CDP_URL: str = os.getenv("CDP_URL", "http://localhost:9222")
    # Hybrid mode: địa chỉ bridge server (prototype_hybrid/bridge_server.py).
    BRIDGE_URL: str = os.getenv("BRIDGE_URL", "http://localhost:8765")
    # Browser used by new_browser mode. chrome tries system Chrome first;
    # chromium uses Playwright's bundled Chromium directly.
    TARGET_BROWSER: str = os.getenv("TARGET_BROWSER", "chrome").lower()

    # AI backend: gemini | chatgpt | aistudio | deepseek | grok | copilot
    AI_BACKEND: str = os.getenv("AI_BACKEND", "gemini")

    # VLM navigation (Agentic Web Navigation fallback)
    VLM_MODEL: str = os.getenv("VLM_MODEL", "llava:7b")
    OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")

    def __init__(self):
        # Runtime overrides (survive backend restart via file)
        rt = _load_runtime()
        if "translator_mode" in rt:
            self.TRANSLATOR_MODE = rt["translator_mode"]
        if "ai_backend" in rt:
            self.AI_BACKEND = rt["ai_backend"]
        if "target_browser" in rt:
            self.TARGET_BROWSER = rt["target_browser"]
        if self.TARGET_BROWSER not in SUPPORTED_TARGET_BROWSERS:
            self.TARGET_BROWSER = "chrome"

    def set_translator_mode(self, mode: str):
        assert mode in ("new_browser", "cdp", "hybrid"), f"Invalid mode: {mode}"
        self.TRANSLATOR_MODE = mode
        rt = _load_runtime()
        rt["translator_mode"] = mode
        _save_runtime(rt)

    def set_ai_backend(self, backend: str):
        assert backend in SUPPORTED_AI_BACKENDS, f"Invalid backend: {backend}"
        self.AI_BACKEND = backend
        rt = _load_runtime()
        rt["ai_backend"] = backend
        _save_runtime(rt)

    def set_target_browser(self, browser: str):
        assert browser in SUPPORTED_TARGET_BROWSERS, f"Invalid browser: {browser}"
        self.TARGET_BROWSER = browser
        rt = _load_runtime()
        rt["target_browser"] = browser
        _save_runtime(rt)


settings = Settings()
