"""Agentic Web Navigation — VLM-based element detection.

Khi CSS selectors hardcoded thất bại (ví dụ web UI thay đổi giao diện),
module này chụp screenshot trang web và dùng Vision-Language Model (VLM)
qua Ollama (local, miễn phí) để xác định vị trí các phần tử UI.

Kiến trúc:
  1. Playwright chụp screenshot toàn trang
  2. Gửi screenshot + prompt mô tả phần tử cần tìm → Ollama VLM
  3. VLM trả về tọa độ (x, y) của phần tử
  4. Playwright click/type tại tọa độ đó

VLM Models hỗ trợ (qua Ollama):
  - llava:7b      — LLaVA 1.6, 4.7GB, nhanh
  - llava:13b     — LLaVA 1.6, 8GB, chính xác hơn
  - llava-phi3    — nhẹ hơn, 2.9GB
  - minicpm-v     — MiniCPM-V, 5.8GB, tốt cho UI grounding

Yêu cầu:
  - Ollama đang chạy tại localhost:11434
  - Model đã được pull: ollama pull llava:7b
"""

import asyncio
import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from playwright.async_api import Page

from app.audit import log_event


# ── Configuration ─────────────────────────────────────────────────────────────

from app.config import settings as _settings

OLLAMA_URL = _settings.OLLAMA_URL
DEFAULT_MODEL = _settings.VLM_MODEL

# Prompts cho VLM — mô tả phần tử UI cần tìm
_ELEMENT_PROMPTS = {
    "input_box": (
        "Look at this screenshot of a web chat interface. "
        "Find the text input area / message box where a user types their message. "
        "Return ONLY the center coordinates as JSON: {\"x\": number, \"y\": number}. "
        "No explanation."
    ),
    "send_button": (
        "Look at this screenshot of a web chat interface. "
        "Find the Send / Submit button used to send a message. "
        "It is usually an arrow icon or a button labeled 'Send'. "
        "Return ONLY the center coordinates as JSON: {\"x\": number, \"y\": number}. "
        "No explanation."
    ),
    "stop_button": (
        "Look at this screenshot of a web chat interface. "
        "Find the Stop / Cancel button that stops the AI from generating. "
        "It might be a square icon or labeled 'Stop'. "
        "If no stop button is visible, return {\"x\": -1, \"y\": -1}. "
        "Return ONLY JSON: {\"x\": number, \"y\": number}. No explanation."
    ),
    "last_response": (
        "Look at this screenshot of a web chat interface. "
        "Find the last AI response message area. "
        "Return the bounding box as JSON: {\"x\": number, \"y\": number, "
        "\"width\": number, \"height\": number} of the response area. "
        "No explanation."
    ),
    "new_chat_button": (
        "Look at this screenshot of a web chat interface. "
        "Find the 'New chat' or 'New conversation' button. "
        "If not visible, return {\"x\": -1, \"y\": -1}. "
        "Return ONLY JSON: {\"x\": number, \"y\": number}. No explanation."
    ),
}


@dataclass
class ElementLocation:
    """Vị trí phần tử trên màn hình."""
    x: int
    y: int
    width: int = 0
    height: int = 0
    confidence: float = 0.0  # 0-1, ước tính từ VLM
    found: bool = True


# ── Ollama VLM client ─────────────────────────────────────────────────────────

async def _check_ollama_available() -> bool:
    """Kiểm tra Ollama có đang chạy không."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


async def _check_model_available(model: str = DEFAULT_MODEL) -> bool:
    """Kiểm tra model VLM đã được pull chưa."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            if r.status_code != 200:
                return False
            models = r.json().get("models", [])
            return any(m.get("name", "").startswith(model.split(":")[0]) for m in models)
    except Exception:
        return False


async def _query_vlm(
    screenshot_b64: str,
    prompt: str,
    model: str = DEFAULT_MODEL,
) -> str:
    """Gửi screenshot + prompt tới Ollama VLM, trả về text response."""
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [screenshot_b64],
        "stream": False,
        "options": {
            "temperature": 0.1,   # Gần deterministic
            "num_predict": 100,   # Chỉ cần JSON ngắn
        },
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
        )
        r.raise_for_status()
        return r.json().get("response", "")


def _parse_coordinates(response: str) -> dict:
    """Parse JSON coordinates từ VLM response."""
    # Tìm JSON object trong response
    match = re.search(r'\{[^}]+\}', response)
    if not match:
        raise ValueError(f"No JSON found in VLM response: {response[:200]}")
    try:
        data = json.loads(match.group())
        # Validate có x, y
        if "x" not in data or "y" not in data:
            raise ValueError(f"Missing x/y in: {data}")
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from VLM: {match.group()}: {e}")


# ── Screenshot helper ─────────────────────────────────────────────────────────

async def _take_screenshot_b64(page: Page) -> str:
    """Chụp screenshot toàn trang, trả về base64 string."""
    screenshot_bytes = await page.screenshot(type="png", full_page=False)
    return base64.b64encode(screenshot_bytes).decode("utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

class VisionNavigator:
    """VLM-based web element finder — fallback khi CSS selectors thất bại.

    Usage:
        nav = VisionNavigator()
        if await nav.is_available():
            loc = await nav.find_element(page, "send_button")
            if loc.found:
                await page.mouse.click(loc.x, loc.y)
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self._available: Optional[bool] = None
        self._cache: dict[str, ElementLocation] = {}

    async def is_available(self) -> bool:
        """Kiểm tra Ollama + VLM model sẵn sàng."""
        if self._available is not None:
            return self._available
        ollama_ok = await _check_ollama_available()
        if not ollama_ok:
            self._available = False
            print("[VisionNav] Ollama not available at", OLLAMA_URL)
            return False
        model_ok = await _check_model_available(self.model)
        if not model_ok:
            self._available = False
            print(f"[VisionNav] Model '{self.model}' not found. Run: ollama pull {self.model}")
            return False
        self._available = True
        print(f"[VisionNav] Ready — model: {self.model}")
        return True

    def clear_cache(self):
        """Xóa cache vị trí (gọi khi trang thay đổi)."""
        self._cache.clear()

    async def find_element(
        self,
        page: Page,
        element_type: str,
        custom_prompt: str | None = None,
        use_cache: bool = True,
    ) -> ElementLocation:
        """Tìm phần tử UI bằng VLM.

        Args:
            page: Playwright page
            element_type: Loại phần tử ("input_box", "send_button", "stop_button",
                          "last_response", "new_chat_button")
            custom_prompt: Prompt tùy chỉnh (thay cho prompt mặc định)
            use_cache: Dùng cache vị trí đã tìm trước đó

        Returns:
            ElementLocation — tọa độ phần tử, found=False nếu không tìm thấy
        """
        # Check cache
        if use_cache and element_type in self._cache:
            cached = self._cache[element_type]
            log_event("vlm.cache_hit",
                      element_type=element_type,
                      x=cached.x, y=cached.y)
            return cached

        prompt = custom_prompt or _ELEMENT_PROMPTS.get(element_type)
        if not prompt:
            raise ValueError(f"Unknown element type: {element_type}")

        # Backend label sourced from current settings — keeps the VLM module
        # backend-agnostic at the API level while still letting Prometheus
        # split rescues by Gemini vs ChatGPT.
        try:
            from app.config import settings
            backend_label = settings.AI_BACKEND or "unknown"
        except Exception:
            backend_label = "unknown"

        t0 = time.time()
        outcome = "error"
        log_event("vlm.fallback_triggered",
                  element_type=element_type,
                  backend=backend_label,
                  model=self.model)
        try:
            screenshot_b64 = await _take_screenshot_b64(page)
            response = await _query_vlm(screenshot_b64, prompt, self.model)
            coords = _parse_coordinates(response)

            x, y = int(coords["x"]), int(coords["y"])

            # VLM trả về (-1, -1) khi không tìm thấy
            if x < 0 or y < 0:
                loc = ElementLocation(x=0, y=0, found=False)
                outcome = "not_found"
            else:
                loc = ElementLocation(
                    x=x,
                    y=y,
                    width=int(coords.get("width", 0)),
                    height=int(coords.get("height", 0)),
                    confidence=0.8,
                    found=True,
                )
                outcome = "found"
                # Cache kết quả
                if use_cache:
                    self._cache[element_type] = loc

            print(f"[VisionNav] {element_type}: ({x}, {y}) found={loc.found}")
            log_event(
                "vlm.find_element_done",
                element_type=element_type,
                backend=backend_label,
                model=self.model,
                outcome=outcome,
                found=loc.found,
                x=loc.x, y=loc.y,
                width=loc.width, height=loc.height,
                confidence=loc.confidence,
                latency_ms=round((time.time() - t0) * 1000),
            )
            return loc

        except Exception as e:
            print(f"[VisionNav] Error finding {element_type}: {e}")
            log_event(
                "vlm.find_element_error",
                element_type=element_type,
                backend=backend_label,
                model=self.model,
                error=str(e)[:200],
                error_type=type(e).__name__,
                latency_ms=round((time.time() - t0) * 1000),
            )
            return ElementLocation(x=0, y=0, found=False)
        finally:
            try:
                from app.metrics import vlm_fallback_total, vlm_call_latency_seconds
                vlm_fallback_total.labels(
                    backend=backend_label,
                    element_type=element_type,
                    outcome=outcome,
                ).inc()
                vlm_call_latency_seconds.labels(
                    element_type=element_type,
                ).observe(time.time() - t0)
            except Exception:
                pass

    async def click_element(
        self,
        page: Page,
        element_type: str,
        custom_prompt: str | None = None,
    ) -> bool:
        """Tìm và click phần tử bằng VLM. Trả về True nếu thành công."""
        loc = await self.find_element(page, element_type, custom_prompt, use_cache=False)
        if not loc.found:
            return False
        await page.mouse.click(loc.x, loc.y)
        return True

    async def is_element_visible(
        self,
        page: Page,
        element_type: str,
        custom_prompt: str | None = None,
    ) -> bool:
        """Kiểm tra phần tử có visible không bằng VLM."""
        loc = await self.find_element(page, element_type, custom_prompt, use_cache=False)
        return loc.found

    async def type_in_element(
        self,
        page: Page,
        element_type: str,
        text: str,
    ) -> bool:
        """Tìm input element bằng VLM, click vào, paste text."""
        loc = await self.find_element(page, element_type, use_cache=False)
        if not loc.found:
            return False
        await page.mouse.click(loc.x, loc.y)
        await asyncio.sleep(0.3)
        # Clipboard paste (an toàn với text dài)
        await page.evaluate(
            "async (t) => { await navigator.clipboard.writeText(t); }", text
        )
        await page.keyboard.press("Control+KeyV")
        return True

    async def derive_selector_at(self, page: Page, x: int, y: int) -> str | None:
        """After a successful VLM click, walk up the DOM at (x, y) to find a
        stable CSS selector that uniquely identifies the element.

        Used by the self-healing selector memory: when learning from a VLM hit,
        we want a selector that:
          1. Resolves to exactly one element (so subsequent code clicks the
             right thing).
          2. Prefers attribute-based selectors (id, data-testid, aria-label,
             role, name) over class-based ones — modern frameworks hash class
             names per build, so `.css-1a2b3c` selectors break on every deploy.

        Returns None if no stable, unique selector can be derived (caller
        should fall back to keeping the VLM path active for that element).
        """
        js = """
        (args) => {
          const px = args.x, py = args.y;
          function uniqueOrNull(sel) {
            try {
              const els = document.querySelectorAll(sel);
              return els.length === 1 ? sel : null;
            } catch (_) { return null; }
          }
          // Heuristic: filter out CSS-Modules / hashed class names which
          // change per build. Keep classes that look "stable" (semantic).
          function isStableClass(c) {
            if (!c || c.length < 3) return false;
            // matches things like 'css-1a2b3c', 'btn__abc123', '_hash_x9z'
            if (/^_?[a-z]+[-_]?[a-zA-Z0-9]{4,}$/.test(c) && /\\d/.test(c)) return false;
            return true;
          }
          let el = document.elementFromPoint(px, py);
          if (!el) return null;
          // Walk up to nearest interactive ancestor (cap depth so we don't
          // pop all the way to <body>).
          const interactive = 'button,input,textarea,a,[role="button"],[role="textbox"],[contenteditable="true"]';
          let depth = 0;
          while (el && el.parentElement && !el.matches(interactive)) {
            if (el.id || el.getAttribute('aria-label') || el.getAttribute('data-testid')) break;
            el = el.parentElement;
            if (++depth > 6) break;
          }
          if (!el || el === document.body) return null;
          const tag = el.tagName.toLowerCase();
          // 1. ID
          if (el.id) {
            const sel = '#' + CSS.escape(el.id);
            const ok = uniqueOrNull(sel);
            if (ok) return ok;
          }
          // 2. Stable attributes
          const attrs = ['data-testid', 'aria-label', 'name', 'aria-labelledby', 'placeholder'];
          for (const attr of attrs) {
            const v = el.getAttribute(attr);
            if (!v) continue;
            const escaped = v.replace(/"/g, '\\\\"');
            const sel = tag + '[' + attr + '="' + escaped + '"]';
            const ok = uniqueOrNull(sel);
            if (ok) return ok;
          }
          // 3. role + tag
          const role = el.getAttribute('role');
          if (role) {
            const sel = tag + '[role="' + role + '"]';
            const ok = uniqueOrNull(sel);
            if (ok) return ok;
          }
          // 4. Tag + stable classes
          if (el.classList.length) {
            const stable = Array.from(el.classList).filter(isStableClass).slice(0, 3);
            if (stable.length) {
              const sel = tag + stable.map(c => '.' + CSS.escape(c)).join('');
              const ok = uniqueOrNull(sel);
              if (ok) return ok;
            }
          }
          return null;
        }
        """
        try:
            sel = await page.evaluate(js, {"x": x, "y": y})
            log_event(
                "vlm.selector_derived",
                x=x, y=y,
                selector=sel or "",
                success=bool(sel),
            )
            return sel
        except Exception as e:
            print(f"[VisionNav] derive_selector_at failed: {e}")
            log_event(
                "vlm.selector_derive_error",
                x=x, y=y,
                error=str(e)[:200],
            )
            return None

    async def detect_page_state(self, page: Page) -> str:
        """Phát hiện trạng thái trang: 'idle' | 'generating' | 'error' | 'unknown'.

        Dùng VLM để đọc trạng thái tổng quát thay vì kiểm tra từng selector.
        """
        prompt = (
            "Look at this screenshot of a web chat interface (like Gemini or ChatGPT). "
            "Determine the current state of the interface. "
            "Return ONLY one of these states as JSON: "
            "{\"state\": \"idle\"} — the AI is ready for input, "
            "{\"state\": \"generating\"} — the AI is currently generating a response, "
            "{\"state\": \"error\"} — there is an error message visible, "
            "{\"state\": \"unknown\"} — cannot determine. "
            "No explanation."
        )
        t0 = time.time()
        try:
            screenshot_b64 = await _take_screenshot_b64(page)
            response = await _query_vlm(screenshot_b64, prompt, self.model)
            data = _parse_coordinates(response)  # reuse JSON parser
            state = data.get("state", "unknown")
            if state not in ("idle", "generating", "error", "unknown"):
                state = "unknown"
            print(f"[VisionNav] Page state: {state}")
            log_event(
                "vlm.detect_page_state",
                state=state,
                model=self.model,
                latency_ms=round((time.time() - t0) * 1000),
            )
            return state
        except Exception as e:
            print(f"[VisionNav] Error detecting page state: {e}")
            log_event(
                "vlm.detect_page_state_error",
                error=str(e)[:200],
                model=self.model,
                latency_ms=round((time.time() - t0) * 1000),
            )
            return "unknown"


# ── Convenience: singleton instance ───────────────────────────────────────────
_navigator: VisionNavigator | None = None


def get_navigator(model: str = DEFAULT_MODEL) -> VisionNavigator:
    """Lấy singleton VisionNavigator instance."""
    global _navigator
    if _navigator is None or _navigator.model != model:
        _navigator = VisionNavigator(model=model)
    return _navigator
