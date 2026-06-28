"""Service dich thuat noi dung LaTeX sang tieng Viet thong qua Gemini/ChatGPT web mien phi.

Kiến trúc điều hướng 2 tầng (Agentic Web Navigation):
  Tầng 1 — CSS selectors: nhanh, chính xác khi UI không đổi
  Tầng 2 — VLM fallback:  chụp screenshot → Vision-Language Model (Ollama)
           tự xác định vị trí phần tử UI → click/type tại tọa độ
           Kích hoạt tự động khi CSS selectors thất bại.
"""

import asyncio
import logging
import os
import subprocess
import time
from playwright.async_api import async_playwright, Page, BrowserContext

from app.config import settings
from app import paths
from app.services.selector_memory import SelectorMemory
from app.audit import log_event

logger = logging.getLogger(__name__)

# ── Opt-in scheduler integration ────────────────────────────────────────────
# When ENABLE_SCHEDULER=1, the translator acquires a Gemini account from the
# AccountPool at job start, reports per-chunk outcomes to the scheduler's
# history (drives adaptive weighting), and releases at cleanup. Default OFF
# so existing single-account setups keep working unchanged.
SCHEDULER_ENABLED = os.getenv("ENABLE_SCHEDULER", "0") == "1"


# ─── AI Backend Strategy ──────────────────────────────────────────────────────

class AIBackend:
    """Base class — dinh nghia interface chung cho cac AI web backend.

    Mỗi method hỗ trợ VLM fallback: thử CSS selector trước,
    nếu thất bại thì dùng VisionNavigator chụp screenshot + VLM.
    """

    url: str = ""  # Trang can navigate toi de bat dau session moi

    def __init__(self):
        self._vlm = None          # lazy-init VisionNavigator
        self._vlm_checked = False  # đã kiểm tra Ollama chưa
        self._vlm_available = False

    async def _get_vlm(self):
        """Lazy-init VisionNavigator, kiểm tra Ollama 1 lần duy nhất."""
        if self._vlm_checked:
            return self._vlm if self._vlm_available else None
        self._vlm_checked = True
        try:
            from app.services.vision_nav import get_navigator
            nav = get_navigator()
            if await nav.is_available():
                self._vlm = nav
                self._vlm_available = True
                print("[AIBackend] VLM navigation: ENABLED (Ollama + VLM ready)")
                return nav
            else:
                print("[AIBackend] VLM navigation: DISABLED (Ollama/model not available)")
                return None
        except Exception as e:
            print(f"[AIBackend] VLM navigation: DISABLED ({e})")
            return None

    async def count_responses(self, page: Page) -> int:
        raise NotImplementedError

    async def is_response_done(self, page: Page) -> bool:
        raise NotImplementedError

    async def get_last_response_text(self, page: Page) -> str:
        raise NotImplementedError

    async def send_input(self, page: Page, prompt: str):
        """Nhap prompt vao o chat va gui di."""
        raise NotImplementedError

    async def start_new_chat(self, page: Page):
        """Mo cuoc tro chuyen moi (reload trang hoac click New chat)."""
        await page.goto(self.url, timeout=120000, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(2)
        # Clear VLM cache sau khi trang thay đổi
        if self._vlm:
            self._vlm.clear_cache()


class GeminiBackend(AIBackend):
    """Selector set cho gemini.google.com — có VLM fallback."""

    url = "https://gemini.google.com/app"

    async def count_responses(self, page: Page) -> int:
        for sel in (
            'message-content.model-response',
            '.model-response-text',
            'model-response',
        ):
            els = await page.query_selector_all(sel)
            if els:
                return len(els)
        return 0

    async def is_response_done(self, page: Page) -> bool:
        # ── Tầng 1: CSS selectors ────────────────────────────────────────
        # Nut Stop hien thi → dang generate
        for sel in (
            'button[aria-label="Stop response"]',
            'button[aria-label="Stop"]',
            'button.stop-button',
            'button[mattooltip="Stop response"]',
        ):
            btn = await page.query_selector(sel)
            if btn:
                try:
                    if await btn.is_visible():
                        return False
                except Exception:
                    pass
        # Loading indicator
        loading = await page.query_selector('mat-progress-bar, .loading-indicator, .streaming')
        if loading:
            try:
                if await loading.is_visible():
                    return False
            except Exception:
                pass
        # Nut Send xuat hien lai → xong
        for sel in ('button.send-button', 'button[aria-label="Send message"]'):
            btn = await page.query_selector(sel)
            if btn:
                try:
                    if await btn.is_visible():
                        return True
                except Exception:
                    pass
        # O input co the nhap lai
        inp = await page.query_selector(
            'div.ql-editor[role="textbox"], div[contenteditable="true"][role="textbox"]'
        )
        if inp:
            try:
                if await inp.is_visible():
                    return True
            except Exception:
                pass

        # ── Tầng 2: VLM fallback ────────────────────────────────────────
        vlm = await self._get_vlm()
        if vlm:
            print("  [Gemini] CSS selectors failed → VLM fallback: detect_page_state")
            state = await vlm.detect_page_state(page)
            if state == "generating":
                return False
            if state == "idle":
                return True

        return False

    async def get_last_response_text(self, page: Page) -> str:
        for sel in (
            'message-content.model-response',
            '.model-response-text',
            'model-response',
            '.response-container',
        ):
            els = await page.query_selector_all(sel)
            if els:
                return (await els[-1].inner_text()).strip()
        return ""

    async def send_input(self, page: Page, prompt: str):
        memory = SelectorMemory.instance()
        input_found = False

        # ── Tầng 1a: Learned selectors (đã tự học từ VLM trước đó) ───────
        for sel in memory.get("gemini", "input_box"):
            try:
                await page.wait_for_selector(sel, timeout=1500)
                await page.click(sel)
                memory.record_success("gemini", "input_box", sel)
                print(f"  [Gemini] Used learned input selector: {sel}")
                input_found = True
                break
            except Exception:
                memory.record_failure("gemini", "input_box", sel)

        # ── Tầng 1b: Hardcoded CSS selectors ─────────────────────────────
        if not input_found:
            input_sel = 'div.ql-editor[role="textbox"], div[contenteditable="true"][role="textbox"]'
            try:
                await page.wait_for_selector(input_sel, timeout=10000)
                await page.click(input_sel)
                input_found = True
            except Exception:
                pass

        # ── Tầng 2: VLM fallback — tìm input box + học selector ──────────
        if not input_found:
            vlm = await self._get_vlm()
            if vlm:
                print("  [Gemini] Input selector failed → VLM fallback: find input_box")
                loc = await vlm.find_element(page, "input_box", use_cache=False)
                if loc.found:
                    await page.mouse.click(loc.x, loc.y)
                    derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                    if derived:
                        memory.record_success("gemini", "input_box", derived)
                        print(f"  [Gemini] Learned new input_box selector: {derived}")
                    await asyncio.sleep(0.3)
                    input_found = True
            if not input_found:
                raise Exception("Cannot find input box (CSS + VLM both failed)")

        # Input found — paste prompt
        await page.evaluate("async (t) => { await navigator.clipboard.writeText(t); }", prompt)
        await page.keyboard.press("Control+KeyV")
        await asyncio.sleep(1)

        # ── Send button: learned → hardcoded → VLM ───────────────────────
        send_clicked = False

        for sel in memory.get("gemini", "send_button"):
            try:
                btn = await page.wait_for_selector(sel, timeout=1500)
                if btn:
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await btn.click()
                    memory.record_success("gemini", "send_button", sel)
                    print(f"  [Gemini] Used learned send selector: {sel}")
                    send_clicked = True
                    break
            except Exception:
                memory.record_failure("gemini", "send_button", sel)

        if not send_clicked:
            send_sel = 'button.send-button, button[aria-label="Send message"]'
            try:
                btn = await page.wait_for_selector(send_sel, timeout=5000)
                if btn:
                    await btn.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await btn.click()
                    send_clicked = True
            except Exception:
                pass

        if not send_clicked:
            vlm = await self._get_vlm()
            if vlm:
                print("  [Gemini] Send button selector failed → VLM fallback")
                loc = await vlm.find_element(page, "send_button", use_cache=False)
                if loc.found:
                    await page.mouse.click(loc.x, loc.y)
                    derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                    if derived:
                        memory.record_success("gemini", "send_button", derived)
                        print(f"  [Gemini] Learned new send_button selector: {derived}")
                    send_clicked = True

        if not send_clicked:
            await page.keyboard.press("Control+Enter")


class ChatGPTBackend(AIBackend):
    """Selector set cho chatgpt.com — có VLM fallback."""

    url = "https://chatgpt.com"

    async def count_responses(self, page: Page) -> int:
        for sel in (
            'article[data-testid^="conversation-turn-"][data-testid$="-assistant"]',
            'div[data-message-author-role="assistant"]',
            '.agent-turn',
        ):
            els = await page.query_selector_all(sel)
            if els:
                return len(els)
        return 0

    async def is_response_done(self, page: Page) -> bool:
        # ── Tầng 1: CSS selectors ────────────────────────────────────────
        # Nut Stop streaming hien thi → dang generate
        for sel in (
            'button[aria-label="Stop streaming"]',
            'button[data-testid="stop-button"]',
        ):
            btn = await page.query_selector(sel)
            if btn:
                try:
                    if await btn.is_visible():
                        return False
                except Exception:
                    pass
        # Nut Send / Submit xuat hien → xong
        for sel in (
            'button[data-testid="send-button"]',
            'button[aria-label="Send prompt"]',
            'button[aria-label="Send message"]',
        ):
            btn = await page.query_selector(sel)
            if btn:
                try:
                    if await btn.is_visible():
                        return True
                except Exception:
                    pass

        # ── Tầng 2: VLM fallback ────────────────────────────────────────
        vlm = await self._get_vlm()
        if vlm:
            print("  [ChatGPT] CSS selectors failed → VLM fallback: detect_page_state")
            state = await vlm.detect_page_state(page)
            if state == "generating":
                return False
            if state == "idle":
                return True

        return False

    async def get_last_response_text(self, page: Page) -> str:
        for sel in (
            'article[data-testid^="conversation-turn-"][data-testid$="-assistant"]',
            'div[data-message-author-role="assistant"]',
            '.agent-turn',
        ):
            els = await page.query_selector_all(sel)
            if els:
                return (await els[-1].inner_text()).strip()
        return ""

    async def send_input(self, page: Page, prompt: str):
        memory = SelectorMemory.instance()
        input_found = False

        # ── Tầng 1a: Learned selectors ───────────────────────────────────
        for sel in memory.get("chatgpt", "input_box"):
            try:
                await page.wait_for_selector(sel, timeout=1500)
                await page.click(sel)
                memory.record_success("chatgpt", "input_box", sel)
                print(f"  [ChatGPT] Used learned input selector: {sel}")
                input_found = True
                break
            except Exception:
                memory.record_failure("chatgpt", "input_box", sel)

        # ── Tầng 1b: Hardcoded CSS selectors ─────────────────────────────
        if not input_found:
            input_sel = (
                '#prompt-textarea, '
                'div[contenteditable="true"][data-id="root"], '
                'textarea[placeholder]'
            )
            try:
                await page.wait_for_selector(input_sel, timeout=10000)
                await page.click(input_sel)
                input_found = True
            except Exception:
                pass

        # ── Tầng 2: VLM fallback — tìm input box + học selector ──────────
        if not input_found:
            vlm = await self._get_vlm()
            if vlm:
                print("  [ChatGPT] Input selector failed → VLM fallback: find input_box")
                loc = await vlm.find_element(page, "input_box", use_cache=False)
                if loc.found:
                    await page.mouse.click(loc.x, loc.y)
                    derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                    if derived:
                        memory.record_success("chatgpt", "input_box", derived)
                        print(f"  [ChatGPT] Learned new input_box selector: {derived}")
                    await asyncio.sleep(0.3)
                    input_found = True
            if not input_found:
                raise Exception("Cannot find input box (CSS + VLM both failed)")

        # Input found — paste prompt
        await page.evaluate("async (t) => { await navigator.clipboard.writeText(t); }", prompt)
        await page.keyboard.press("Control+KeyV")
        await asyncio.sleep(1)

        # ── Send button: learned → hardcoded → VLM ───────────────────────
        send_clicked = False

        for sel in memory.get("chatgpt", "send_button"):
            try:
                btn = await page.wait_for_selector(sel, timeout=1500)
                if btn and await btn.is_visible():
                    await btn.click()
                    memory.record_success("chatgpt", "send_button", sel)
                    print(f"  [ChatGPT] Used learned send selector: {sel}")
                    send_clicked = True
                    break
            except Exception:
                memory.record_failure("chatgpt", "send_button", sel)

        if not send_clicked:
            send_sel = (
                'button[data-testid="send-button"], '
                'button[aria-label="Send prompt"], '
                'button[aria-label="Send message"]'
            )
            try:
                btn = await page.wait_for_selector(send_sel, timeout=5000)
                if btn and await btn.is_visible():
                    await btn.click()
                    send_clicked = True
            except Exception:
                pass

        if not send_clicked:
            vlm = await self._get_vlm()
            if vlm:
                print("  [ChatGPT] Send button selector failed → VLM fallback")
                loc = await vlm.find_element(page, "send_button", use_cache=False)
                if loc.found:
                    await page.mouse.click(loc.x, loc.y)
                    derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                    if derived:
                        memory.record_success("chatgpt", "send_button", derived)
                        print(f"  [ChatGPT] Learned new send_button selector: {derived}")
                    send_clicked = True

        if not send_clicked:
            await page.keyboard.press("Enter")

    async def start_new_chat(self, page: Page):
        """ChatGPT: click nut New chat thay vi reload trang."""
        memory = SelectorMemory.instance()

        # ── Tầng 1a: Learned selectors ───────────────────────────────────
        for sel in memory.get("chatgpt", "new_chat_button"):
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    memory.record_success("chatgpt", "new_chat_button", sel)
                    print(f"  [ChatGPT] Used learned new_chat selector: {sel}")
                    await asyncio.sleep(2)
                    if self._vlm:
                        self._vlm.clear_cache()
                    return
            except Exception:
                memory.record_failure("chatgpt", "new_chat_button", sel)

        # ── Tầng 1b: Hardcoded CSS selectors ─────────────────────────────
        new_chat_sel = (
            'a[aria-label="New chat"], '
            'button[aria-label="New chat"], '
            'a[href="/"]'
        )
        try:
            btn = await page.query_selector(new_chat_sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(2)
                if self._vlm:
                    self._vlm.clear_cache()
                return
        except Exception:
            pass

        # ── Tầng 2: VLM fallback — tìm New Chat button + học selector ───
        vlm = await self._get_vlm()
        if vlm:
            print("  [ChatGPT] New chat selector failed → VLM fallback")
            loc = await vlm.find_element(page, "new_chat_button", use_cache=False)
            if loc.found:
                await page.mouse.click(loc.x, loc.y)
                derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                if derived:
                    memory.record_success("chatgpt", "new_chat_button", derived)
                    print(f"  [ChatGPT] Learned new_chat_button selector: {derived}")
                await asyncio.sleep(2)
                vlm.clear_cache()
                return

        # Fallback cuối: navigate trực tiếp
        await page.goto(self.url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        if self._vlm:
            self._vlm.clear_cache()


class DeepSeekBackend(AIBackend):
    """Selector set cho chat.deepseek.com — có VLM fallback.

    DeepSeek web đổi class hash thường xuyên nên CSS selectors để lỏng và
    ưu tiên gửi bằng phím Enter; done-detection dựa thêm VLM fallback
    (Contribution 2) khi CSS thất bại.
    """

    url = "https://chat.deepseek.com"

    async def count_responses(self, page: Page) -> int:
        for sel in (
            '.ds-markdown',
            'div[class*="ds-markdown"]',
            'div[class*="_assistant"]',
        ):
            els = await page.query_selector_all(sel)
            if els:
                return len(els)
        return 0

    async def is_response_done(self, page: Page) -> bool:
        # ── Tầng 1: CSS selectors ────────────────────────────────────────
        # Nút Stop hiển thị → đang generate
        for sel in (
            'div[role="button"][aria-label="Stop"]',
            'div[aria-label="Stop generating"]',
            'button[aria-label*="Stop"]',
        ):
            btn = await page.query_selector(sel)
            if btn:
                try:
                    if await btn.is_visible():
                        return False
                except Exception:
                    pass
        # Toolbar copy/regenerate dưới response → xong
        for sel in (
            'div[class*="ds-icon-button"]',
            'div[class*="_toolbar"]',
        ):
            el = await page.query_selector(sel)
            if el:
                try:
                    if await el.is_visible():
                        return True
                except Exception:
                    pass

        # ── Tầng 2: VLM fallback ────────────────────────────────────────
        vlm = await self._get_vlm()
        if vlm:
            print("  [DeepSeek] CSS selectors failed → VLM fallback: detect_page_state")
            state = await vlm.detect_page_state(page)
            if state == "generating":
                return False
            if state == "idle":
                return True

        return False

    async def get_last_response_text(self, page: Page) -> str:
        for sel in (
            '.ds-markdown',
            'div[class*="ds-markdown"]',
            'div[class*="_assistant"]',
        ):
            els = await page.query_selector_all(sel)
            if els:
                return (await els[-1].inner_text()).strip()
        return ""

    async def send_input(self, page: Page, prompt: str):
        memory = SelectorMemory.instance()
        input_found = False

        # ── Tầng 1a: Learned selectors ───────────────────────────────────
        for sel in memory.get("deepseek", "input_box"):
            try:
                await page.wait_for_selector(sel, timeout=1500)
                await page.click(sel)
                memory.record_success("deepseek", "input_box", sel)
                print(f"  [DeepSeek] Used learned input selector: {sel}")
                input_found = True
                break
            except Exception:
                memory.record_failure("deepseek", "input_box", sel)

        # ── Tầng 1b: Hardcoded CSS selectors ─────────────────────────────
        if not input_found:
            input_sel = (
                'textarea#chat-input, '
                'textarea[placeholder], '
                'div[contenteditable="true"]'
            )
            try:
                await page.wait_for_selector(input_sel, timeout=10000)
                await page.click(input_sel)
                input_found = True
            except Exception:
                pass

        # ── Tầng 2: VLM fallback — tìm input box + học selector ──────────
        if not input_found:
            vlm = await self._get_vlm()
            if vlm:
                print("  [DeepSeek] Input selector failed → VLM fallback: find input_box")
                loc = await vlm.find_element(page, "input_box", use_cache=False)
                if loc.found:
                    await page.mouse.click(loc.x, loc.y)
                    derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                    if derived:
                        memory.record_success("deepseek", "input_box", derived)
                        print(f"  [DeepSeek] Learned new input_box selector: {derived}")
                    await asyncio.sleep(0.3)
                    input_found = True
            if not input_found:
                raise Exception("Cannot find input box (CSS + VLM both failed)")

        # Input found — paste prompt
        await page.evaluate("async (t) => { await navigator.clipboard.writeText(t); }", prompt)
        await page.keyboard.press("Control+KeyV")
        await asyncio.sleep(1)

        # ── Send: learned → hardcoded → Enter ───────────────────────────
        # DeepSeek gửi ổn định nhất bằng Enter; thử nút Send trước nếu có.
        send_clicked = False

        for sel in memory.get("deepseek", "send_button"):
            try:
                btn = await page.wait_for_selector(sel, timeout=1500)
                if btn and await btn.is_visible():
                    await btn.click()
                    memory.record_success("deepseek", "send_button", sel)
                    print(f"  [DeepSeek] Used learned send selector: {sel}")
                    send_clicked = True
                    break
            except Exception:
                memory.record_failure("deepseek", "send_button", sel)

        if not send_clicked:
            send_sel = (
                'div[role="button"][aria-label*="end"], '
                'button[type="submit"]'
            )
            try:
                btn = await page.wait_for_selector(send_sel, timeout=3000)
                if btn and await btn.is_visible():
                    await btn.click()
                    send_clicked = True
            except Exception:
                pass

        if not send_clicked:
            await page.keyboard.press("Enter")

    async def start_new_chat(self, page: Page):
        """DeepSeek: click 'New chat' nếu có, fallback navigate trực tiếp."""
        memory = SelectorMemory.instance()

        for sel in memory.get("deepseek", "new_chat_button"):
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    memory.record_success("deepseek", "new_chat_button", sel)
                    await asyncio.sleep(2)
                    if self._vlm:
                        self._vlm.clear_cache()
                    return
            except Exception:
                memory.record_failure("deepseek", "new_chat_button", sel)

        new_chat_sel = (
            'div[class*="_new"][role="button"], '
            'div[class*="new-chat"], '
            'a[href="/"]'
        )
        try:
            btn = await page.query_selector(new_chat_sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(2)
                if self._vlm:
                    self._vlm.clear_cache()
                return
        except Exception:
            pass

        # Fallback cuối: navigate trực tiếp
        await page.goto(self.url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        if self._vlm:
            self._vlm.clear_cache()


class GenericWebChatBackend(AIBackend):
    """Backend nền cho các web chat UI chưa có selector chuyên sâu.

    Dùng selector mềm + SelectorMemory + VLM fallback. Các subclass chỉ khai
    báo URL và vài selector đặc thù của từng trang.
    """

    backend_key = "generic"
    display_name = "Generic"
    response_selectors: tuple[str, ...] = (
        'div[data-message-author-role="assistant"]',
        'article',
        '[class*="markdown"]',
        '[class*="response"]',
        '[class*="answer"]',
    )
    input_selectors: tuple[str, ...] = (
        'textarea[placeholder]',
        'textarea',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
    )
    send_selectors: tuple[str, ...] = (
        'button[type="submit"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="Submit"]',
        'button[data-testid*="send"]',
    )
    stop_selectors: tuple[str, ...] = (
        'button[aria-label*="Stop"]',
        'button[data-testid*="stop"]',
        '[aria-label*="Stop generating"]',
    )
    done_selectors: tuple[str, ...] = (
        'button[aria-label*="Send"]',
        'button[type="submit"]',
        'button[data-testid*="send"]',
    )
    new_chat_selectors: tuple[str, ...] = (
        'a[aria-label*="New"]',
        'button[aria-label*="New"]',
        'a[href="/"]',
    )
    submit_key = "Enter"

    async def count_responses(self, page: Page) -> int:
        for sel in self.response_selectors:
            els = await page.query_selector_all(sel)
            if els:
                return len(els)
        return 0

    async def is_response_done(self, page: Page) -> bool:
        for sel in self.stop_selectors:
            btn = await page.query_selector(sel)
            if btn:
                try:
                    if await btn.is_visible():
                        return False
                except Exception:
                    pass
        for sel in self.done_selectors:
            btn = await page.query_selector(sel)
            if btn:
                try:
                    if await btn.is_visible():
                        return True
                except Exception:
                    pass
        vlm = await self._get_vlm()
        if vlm:
            print(
                f"  [{self.display_name}] CSS selectors failed "
                "→ VLM fallback: detect_page_state"
            )
            state = await vlm.detect_page_state(page)
            if state == "generating":
                return False
            if state == "idle":
                return True
        return False

    async def get_last_response_text(self, page: Page) -> str:
        for sel in self.response_selectors:
            els = await page.query_selector_all(sel)
            if els:
                text = (await els[-1].inner_text()).strip()
                if text:
                    return text
        return ""

    async def send_input(self, page: Page, prompt: str):
        memory = SelectorMemory.instance()
        input_found = False

        for sel in memory.get(self.backend_key, "input_box"):
            try:
                await page.wait_for_selector(sel, timeout=1500)
                await page.click(sel)
                memory.record_success(self.backend_key, "input_box", sel)
                print(f"  [{self.display_name}] Used learned input selector: {sel}")
                input_found = True
                break
            except Exception:
                memory.record_failure(self.backend_key, "input_box", sel)

        if not input_found:
            input_sel = ", ".join(self.input_selectors)
            try:
                await page.wait_for_selector(input_sel, timeout=10000)
                await page.click(input_sel)
                input_found = True
            except Exception:
                pass

        if not input_found:
            vlm = await self._get_vlm()
            if vlm:
                print(
                    f"  [{self.display_name}] Input selector failed "
                    "→ VLM fallback: find input_box"
                )
                loc = await vlm.find_element(page, "input_box", use_cache=False)
                if loc.found:
                    await page.mouse.click(loc.x, loc.y)
                    derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                    if derived:
                        memory.record_success(
                            self.backend_key, "input_box", derived
                        )
                        print(
                            f"  [{self.display_name}] Learned input selector: "
                            f"{derived}"
                        )
                    await asyncio.sleep(0.3)
                    input_found = True
            if not input_found:
                raise Exception("Cannot find input box (CSS + VLM both failed)")

        await page.evaluate(
            "async (t) => { await navigator.clipboard.writeText(t); }",
            prompt,
        )
        await page.keyboard.press("Control+KeyV")
        await asyncio.sleep(1)

        send_clicked = False
        for sel in memory.get(self.backend_key, "send_button"):
            try:
                btn = await page.wait_for_selector(sel, timeout=1500)
                if btn and await btn.is_visible():
                    await btn.click()
                    memory.record_success(self.backend_key, "send_button", sel)
                    print(f"  [{self.display_name}] Used learned send selector: {sel}")
                    send_clicked = True
                    break
            except Exception:
                memory.record_failure(self.backend_key, "send_button", sel)

        if not send_clicked:
            send_sel = ", ".join(self.send_selectors)
            try:
                btn = await page.wait_for_selector(send_sel, timeout=5000)
                if btn and await btn.is_visible():
                    await btn.click()
                    send_clicked = True
            except Exception:
                pass

        if not send_clicked:
            vlm = await self._get_vlm()
            if vlm:
                print(f"  [{self.display_name}] Send selector failed → VLM fallback")
                loc = await vlm.find_element(page, "send_button", use_cache=False)
                if loc.found:
                    await page.mouse.click(loc.x, loc.y)
                    derived = await vlm.derive_selector_at(page, loc.x, loc.y)
                    if derived:
                        memory.record_success(
                            self.backend_key, "send_button", derived
                        )
                        print(
                            f"  [{self.display_name}] Learned send selector: "
                            f"{derived}"
                        )
                    send_clicked = True

        if not send_clicked:
            await page.keyboard.press(self.submit_key)

    async def start_new_chat(self, page: Page):
        memory = SelectorMemory.instance()

        for sel in memory.get(self.backend_key, "new_chat_button"):
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    memory.record_success(self.backend_key, "new_chat_button", sel)
                    await asyncio.sleep(2)
                    if self._vlm:
                        self._vlm.clear_cache()
                    return
            except Exception:
                memory.record_failure(self.backend_key, "new_chat_button", sel)

        new_chat_sel = ", ".join(self.new_chat_selectors)
        try:
            btn = await page.query_selector(new_chat_sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(2)
                if self._vlm:
                    self._vlm.clear_cache()
                return
        except Exception:
            pass

        await page.goto(self.url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        if self._vlm:
            self._vlm.clear_cache()


class AIStudioBackend(GenericWebChatBackend):
    """Selector set cho aistudio.google.com."""

    backend_key = "aistudio"
    display_name = "AIStudio"
    url = "https://aistudio.google.com/prompts/new_chat"
    response_selectors = (
        'ms-chat-turn',
        'div[class*="model-response"]',
        'div[class*="response"]',
        '[class*="markdown"]',
        'article',
    )
    input_selectors = (
        'textarea[placeholder]',
        'textarea',
        'div[contenteditable="true"]',
    )
    send_selectors = (
        'button[aria-label*="Run"]',
        'button[aria-label*="Send"]',
        'button[type="submit"]',
    )
    stop_selectors = (
        'button[aria-label*="Stop"]',
        'button[aria-label*="Cancel"]',
    )
    done_selectors = (
        'button[aria-label*="Run"]',
        'button[aria-label*="Send"]',
    )
    new_chat_selectors = (
        'a[href*="new_chat"]',
        'button[aria-label*="New"]',
    )
    submit_key = "Control+Enter"


class GrokBackend(GenericWebChatBackend):
    """Selector set cho grok.com."""

    backend_key = "grok"
    display_name = "Grok"
    url = "https://grok.com"
    response_selectors = (
        '[data-testid*="message"]',
        '[class*="message"] [class*="markdown"]',
        '[class*="response"]',
        'article',
    )
    input_selectors = (
        'textarea[placeholder]',
        'textarea',
        'div[contenteditable="true"]',
    )
    send_selectors = (
        'button[aria-label*="Send"]',
        'button[type="submit"]',
        'button[data-testid*="send"]',
    )
    new_chat_selectors = (
        'a[href="/"]',
        'button[aria-label*="New"]',
        'a[aria-label*="New"]',
    )


class CopilotBackend(GenericWebChatBackend):
    """Selector set cho copilot.microsoft.com."""

    backend_key = "copilot"
    display_name = "Copilot"
    url = "https://copilot.microsoft.com"
    response_selectors = (
        'cib-message',
        '[data-content="ai-message"]',
        '[class*="ac-container"]',
        '[class*="markdown"]',
        'article',
    )
    input_selectors = (
        'textarea[placeholder]',
        'textarea',
        'div[contenteditable="true"]',
        'cib-text-input textarea',
    )
    send_selectors = (
        'button[aria-label*="Submit"]',
        'button[aria-label*="Send"]',
        'button[type="submit"]',
    )
    stop_selectors = (
        'button[aria-label*="Stop"]',
        'button[aria-label*="Cancel"]',
    )
    new_chat_selectors = (
        'button[aria-label*="New"]',
        'a[aria-label*="New"]',
        'a[href="/"]',
    )


# ─── Hybrid backend (TRANSLATOR_MODE=hybrid) ──────────────────────────────────

class HybridBackend(AIBackend):
    """Backend rỗng cho chế độ hybrid — browser do userscript Tampermonkey điều
    khiển (xem prototype_hybrid/), Python chỉ đẩy job qua bridge.

    4 method I/O (send_input/count_responses/is_response_done/get_last_response_text)
    KHÔNG bao giờ được gọi vì `WebAITranslator._send_prompt_and_get_response` đã rẽ
    nhánh sang bridge. `start_new_chat` là no-op: việc mở chat mới do userscript tự
    xoay vòng (cứ X prompt một lần) để tránh context phình.
    """

    url = ""

    async def start_new_chat(self, page):  # noqa: D401 — no-op trong hybrid
        return


class _HybridPage:
    """Page giả để code điều phối (ModelPassAgent/pipeline) chạy mà không cần sửa.

    Trong hybrid không có Playwright Page thật; mọi tương tác DOM đã chuyển sang
    userscript. Object này chỉ cần đáp ứng vài lời gọi vô hại mà tầng điều phối
    thực hiện trên `page` (vd. `evaluate("1")` để kiểm tra page còn sống).
    """

    async def evaluate(self, *args, **kwargs):
        return 1

    async def goto(self, *args, **kwargs):
        return None

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def close(self):
        return None


class _HybridContext:
    """BrowserContext giả — `new_page()` trả _HybridPage để vòng K-tab của
    ModelPassAgent (`context.new_page()`) chạy nguyên vẹn. Song song thật nằm ở
    phía trình duyệt (N tab userscript cùng poll bridge)."""

    @property
    def pages(self):
        return []

    async def new_page(self):
        return _HybridPage()

    async def close(self):
        return None


# Tên backend → class. Dùng cho cross-model judge (chọn model khác model dịch).
_BACKENDS: dict[str, type[AIBackend]] = {
    "gemini": GeminiBackend,
    "chatgpt": ChatGPTBackend,
    "aistudio": AIStudioBackend,
    "deepseek": DeepSeekBackend,
    "grok": GrokBackend,
    "copilot": CopilotBackend,
}


def _chrome_translator_hint() -> str:
    project_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    launcher = os.path.join(project_dir, "launcher.pyw")
    profile = os.path.join(project_dir, "backend", "browser_data", "chrome_cdp_profile")
    return (
        f"Launcher: {launcher}. "
        "Double-click launcher.pyw rồi bấm Bật, hoặc mở shortcut "
        "'Google Chrome (Translator)' trên Desktop. "
        f"Chrome Translator phải chạy ở {settings.CDP_URL} với profile riêng: {profile}."
    )


def make_backend(name: str) -> AIBackend:
    """Tạo backend theo tên web AI. Mặc định Gemini."""
    cls = _BACKENDS.get((name or "").lower(), GeminiBackend)
    return cls()


def _make_backend() -> AIBackend:
    """Tao backend tuong ung voi settings.AI_BACKEND.

    VLM fallback được lazy-init tự động — nếu Ollama + VLM model
    sẵn sàng thì kích hoạt, nếu không thì chỉ dùng CSS selectors.
    """
    return make_backend(settings.AI_BACKEND)


class WebAITranslator:
    """Dung Playwright tuong tac voi web AI de dich LaTeX sang tieng Viet."""

    def __init__(self, user_data_dir: str | None = None, backend: str | None = None):
        # None → resolve to OS-appropriate location via app.paths so the
        # packaged app writes into %APPDATA%/Library/XDG instead of CWD.
        self.user_data_dir = user_data_dir or paths.browser_data_dir()
        self.glossary: dict[str, str] = {}
        self.locked_terms: list[str] = []   # lowercase EN keys that are user-locked
        self._playwright = None
        # CDP mode: track the remote browser + page we opened
        self._cdp_browser = None
        self._cdp_page = None
        # AI backend — override qua param `backend` (dùng cho cross-model judge:
        # chọn model KHÁC model đã dịch), nếu None thì theo global settings.AI_BACKEND.
        self._backend_name = (backend or settings.AI_BACKEND or "gemini").lower()
        self._backend: AIBackend = make_backend(self._backend_name)
        # Hybrid mode: giữ _backend_name (để bridge route job theo model) nhưng
        # thay _backend bằng HybridBackend rỗng — không lái Playwright nữa.
        self._hybrid = (settings.TRANSLATOR_MODE or "").lower() == "hybrid"
        if self._hybrid:
            self._backend = HybridBackend()
        # Scheduler-leased account (Contribution 1). Only populated when
        # ENABLE_SCHEDULER=1; otherwise stays None and pipeline behaves exactly
        # like a single-account setup.
        self._account = None
        self._worker_id = f"worker-{os.getpid()}"
        # Audit logger được pipeline gắn vào sau khi khởi tạo (optional —
        # nếu None thì log_event() qua contextvar vẫn route được).
        self.audit = None
        print(f"[Translator] AI backend: {self._backend_name}, mode: {settings.TRANSLATOR_MODE}")

    @property
    def backend_name(self) -> str:
        """Tên backend đang dùng (gemini/chatgpt/deepseek) — public read-only."""
        return self._backend_name

    def get_account_info(self) -> dict[str, str]:
        """Trả về dict mô tả tài khoản đang dùng cho việc stamp metadata.

        Khi scheduler bật (ENABLE_SCHEDULER=1) → trả email thật của account đã
        lease. Khi không bật → trả rỗng → caller hiểu là "profile mặc định".
        Luôn an toàn để gọi (kể cả khi browser chưa launch).
        """
        return {
            "backend": self._backend_name,
            "account_email": self._account.email if self._account else "",
            "worker_id": self._worker_id,
        }

    def _kill_orphaned_chromium(self):
        """Kill cac process Chrome/Chromium bi orphan tu session cu.

        Kill hai loại:
          1. Playwright Chromium (command line chứa 'ms-playwright')
          2. Chrome thật dùng profile của chúng ta (chứa 'browser_data')
        KHÔNG kill Chrome của user (window khác, không có browser_data trong cmdline).

        Dung psutil de cross-platform (Windows + Linux container). Khong dung
        PowerShell vi Linux container khong co PowerShell.
        """
        try:
            import psutil
        except ImportError:
            print("[Translator] psutil not installed — skip orphan cleanup")
            return

        # Tên thư mục profile — đủ để phân biệt session của chúng ta vs Chrome thường
        profile_marker = os.path.basename(os.path.abspath(self.user_data_dir))  # "browser_data"
        target_names = {"chrome.exe", "chrome", "chromium", "chromium.exe",
                        "chromium-browser", "google-chrome", "headless_shell"}

        killed = 0
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pname = (proc.info.get("name") or "").lower()
                if pname not in target_names:
                    continue
                cmdline = proc.info.get("cmdline") or []
                cmd_str = " ".join(cmdline)
                if "ms-playwright" in cmd_str or profile_marker in cmd_str:
                    proc.kill()
                    killed += 1
                    print(f"[Translator] Killed orphaned browser pid={proc.info['pid']}")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        if killed == 0:
            # not an error — just silence the noisy stdout for the common case
            pass

    def _clean_lock_files(self, data_dir: str):
        """Xoa lock files tu session cu."""
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = os.path.join(data_dir, name)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def launch_browser(self) -> tuple[BrowserContext, Page]:
        """Mo browser. Che do phu thuoc settings.TRANSLATOR_MODE:
        - 'cdp': ket noi vao Chrome dang chay cua user (co Gemini Pro)
        - 'new_browser': mo Chromium moi qua Playwright (mac dinh)
        - 'hybrid': khong lai browser — day job qua bridge cho userscript
        """
        if self._hybrid:
            return await self._launch_hybrid()

        # Opt-in: lease a Gemini account from the AccountPool. If no account
        # becomes free within 60s we fall back to the legacy single-profile
        # path so demos never hard-fail because of scheduler misconfig.
        if SCHEDULER_ENABLED and self._account is None:
            t_acq = time.time()
            log_event("scheduler.acquire_started",
                      worker_id=self._worker_id, timeout_seconds=60.0)
            try:
                from app.pools.account_pool import get_account_pool
                pool = get_account_pool()
                acct = await asyncio.to_thread(pool.acquire, self._worker_id, 60.0)
                if acct is not None:
                    self._account = acct
                    # Override the profile dir so each account uses its own
                    # Playwright user-data-dir (separate cookies/session).
                    self.user_data_dir = acct.profile_dir
                    logger.info(
                        "[Translator] scheduler acquired account=%s profile=%s",
                        acct.email, acct.profile_dir,
                    )
                    log_event(
                        "scheduler.acquired",
                        account_email=acct.email,
                        profile_dir=acct.profile_dir,
                        wait_seconds=round(time.time() - t_acq, 3),
                    )
                else:
                    logger.warning(
                        "[Translator] scheduler acquire timed out — "
                        "falling back to default profile",
                    )
                    log_event("scheduler.acquire_timeout",
                              wait_seconds=round(time.time() - t_acq, 3))
            except Exception as e:
                logger.warning("[Translator] scheduler acquire failed: %s", e)
                log_event("scheduler.acquire_failed",
                          error=str(e)[:200],
                          wait_seconds=round(time.time() - t_acq, 3))

        log_event("browser.launch_started",
                  mode=settings.TRANSLATOR_MODE,
                  backend=self._backend_name,
                  account_email=self._account.email if self._account else "")
        if settings.TRANSLATOR_MODE == "cdp":
            try:
                return await self._connect_cdp()
            except Exception as e:
                logger.warning(
                    "[Translator] CDP unavailable (%s) — refusing managed-browser fallback",
                    e,
                )
                log_event(
                    "browser.cdp_unavailable",
                    cdp_url=settings.CDP_URL,
                    backend=self._backend_name,
                    error=str(e)[:200],
                )
                raise RuntimeError(
                    "Khong ket noi duoc Chrome CDP nen khong mo profile moi. "
                    + _chrome_translator_hint()
                ) from e
                raise RuntimeError(
                    "Không kết nối được Chrome CDP nên không mở profile mới. "
                    "Hãy bật launcher hoặc mở Chrome Translator trên port 9222 "
                    "rồi chạy lại job."
                ) from e
        return await self._launch_new_browser()

    async def _launch_hybrid(self):
        """Hybrid: không lái browser — chỉ kiểm tra bridge sẵn sàng rồi trả về
        context/page giả để tầng điều phối chạy nguyên vẹn.

        Browser do người dùng tự mở (tab AI đã đăng nhập + userscript Tampermonkey);
        song song thật nằm ở số tab userscript đang poll bridge.
        """
        from app.services import hybrid_bridge

        log_event("browser.launch_started", mode="hybrid",
                  backend=self._backend_name)
        try:
            info = await hybrid_bridge.health()
        except Exception as e:
            log_event("browser.hybrid_bridge_unavailable",
                      bridge_url=hybrid_bridge.base_url(), error=str(e)[:200])
            raise RuntimeError(
                f"Khong ket noi duoc bridge server tai {hybrid_bridge.base_url()}. "
                "Chay: python web-ai-translator/prototype_hybrid/bridge_server.py, "
                "roi mo + dang nhap tab AI co cai userscript Tampermonkey."
            ) from e

        workers = info.get("workers", 0)
        if not workers:
            logger.warning(
                "[Translator] hybrid: bridge OK nhung CHUA co worker nao — "
                "mo tab AI + userscript cho backend=%s", self._backend_name,
            )
        log_event("browser.hybrid_ready",
                  bridge_url=hybrid_bridge.base_url(),
                  backend=self._backend_name, workers=workers)
        print(f"[Translator] HYBRID: bridge {hybrid_bridge.base_url()} OK, "
              f"workers={workers}, backend={self._backend_name}")
        return _HybridContext(), _HybridPage()

    async def _connect_cdp(self) -> tuple[BrowserContext, Page]:
        """Ket noi vao Chrome dang chay cua user qua CDP (port 9222).

        Chrome phai duoc mo voi flag:
            chrome.exe --remote-debugging-port=9222 --remote-allow-origins=*
        """
        print(f"[Translator] CDP mode: connecting to {settings.CDP_URL} ...")
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        t_cdp = time.time()
        try:
            browser = await self._playwright.chromium.connect_over_cdp(settings.CDP_URL)
        except Exception as e:
            log_event("browser.cdp_connect_failed",
                      cdp_url=settings.CDP_URL,
                      duration_seconds=round(time.time() - t_cdp, 3),
                      error=str(e)[:200])
            raise ConnectionError(
                f"Khong the ket noi vao Chrome tai {settings.CDP_URL}. "
                + _chrome_translator_hint()
                + "\n"
                f"Chi tiet: {e}"
            )
            raise ConnectionError(
                f"Khong the ket noi vao Chrome tai {settings.CDP_URL}. "
                "Hay mo Chrome voi flag: --remote-debugging-port=9222 --remote-allow-origins=*\n"
                f"Chi tiet: {e}"
            )
        log_event("browser.cdp_connected",
                  cdp_url=settings.CDP_URL,
                  duration_seconds=round(time.time() - t_cdp, 3))

        self._cdp_browser = browser

        # Dung context dau tien (chua session/cookie cua user)
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context()

        # Mo tab moi de khong anh huong tab dang xem cua user
        page = await context.new_page()
        self._cdp_page = page

        print(f"[Translator] CDP: navigating to {self._backend.url} ...")
        await page.goto(
            self._backend.url,
            timeout=120000,
            wait_until="domcontentloaded",
        )
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        print("[Translator] CDP: connected and ready.")
        return context, page

    async def _launch_new_browser(self) -> tuple[BrowserContext, Page]:
        """Mo browser Playwright voi persistent profile. Lan dau can dang nhap Gemini thu cong."""
        abs_data_dir = os.path.abspath(self.user_data_dir)

        # Kill orphaned Chromium processes truoc
        self._kill_orphaned_chromium()
        time.sleep(1)

        # Xoa lock files
        self._clean_lock_files(abs_data_dir)

        os.makedirs(abs_data_dir, exist_ok=True)

        # Args chống phát hiện bot — KHÔNG có --no-sandbox / --disable-dev-shm-usage
        # vì các flag đó bị ChatGPT và Gemini dùng để nhận ra Playwright Chromium.
        _STEALTH_ARGS = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-notifications",
        ]

        # Linux container args — chi them khi PLAYWRIGHT_NO_SANDBOX=1 (set boi
        # Dockerfile khi chay as root). Tren host Linux thuong, chrome chay duoc
        # qua user namespace sandbox khong can flag nay.
        import sys as _sys
        if _sys.platform != "win32":
            # /dev/shm trong container mac dinh chi 64MB — chromium se crash.
            # Flag nay an toan, khong trigger bot detection.
            _STEALTH_ARGS.append("--disable-dev-shm-usage")
            if os.environ.get("PLAYWRIGHT_NO_SANDBOX") == "1":
                _STEALTH_ARGS.append("--no-sandbox")

        # Script inject vào mọi page để xóa dấu hiệu automation
        _STEALTH_SCRIPT = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """

        # Retry launch toi da 3 lan
        last_error = None
        for attempt in range(3):
            t_launch = time.time()
            try:
                self._playwright = await async_playwright().start()

                # TARGET_BROWSER điều khiển từ UI:
                # - chrome: ưu tiên Chrome thật (ít bị detect hơn), fallback Chromium
                # - chromium: dùng thẳng Playwright Chromium
                launch_kwargs = dict(
                    headless=False,
                    viewport={"width": 1280, "height": 800},
                    args=_STEALTH_ARGS,
                    timeout=30000,
                )
                preferred_browser = getattr(settings, "TARGET_BROWSER", "chrome")
                channel_used = preferred_browser
                if preferred_browser == "chromium":
                    channel_used = "chromium"
                    context = await self._playwright.chromium.launch_persistent_context(
                        abs_data_dir,
                        **launch_kwargs,
                    )
                    print("[Translator] Launched with Playwright Chromium")
                else:
                    try:
                        context = await self._playwright.chromium.launch_persistent_context(
                            abs_data_dir,
                            channel="chrome",
                            **launch_kwargs,
                        )
                        print("[Translator] Launched with system Chrome (channel=chrome)")
                    except Exception as chrome_err:
                        print(f"[Translator] Chrome not found ({chrome_err}), falling back to Chromium")
                        channel_used = "chromium"
                        context = await self._playwright.chromium.launch_persistent_context(
                            abs_data_dir,
                            **launch_kwargs,
                        )

                # Xóa dấu hiệu automation trên mọi page mới
                await context.add_init_script(_STEALTH_SCRIPT)

                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(
                    self._backend.url,
                    timeout=120000,
                    wait_until="domcontentloaded",
                )
                # Wait for network to quiet down (best-effort, don't fail if slow)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                log_event(
                    "browser.launched",
                    attempt=attempt + 1,
                    channel=channel_used,
                    profile_dir=abs_data_dir,
                    backend_url=self._backend.url,
                    duration_seconds=round(time.time() - t_launch, 3),
                )
                return context, page
            except Exception as e:
                last_error = e
                print(f"[Translator] Launch attempt {attempt+1} failed: {e}")
                log_event(
                    "browser.launch_failed",
                    attempt=attempt + 1,
                    duration_seconds=round(time.time() - t_launch, 3),
                    error=str(e)[:200],
                    error_type=type(e).__name__,
                )
                # Cleanup Playwright
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

                # Kill orphaned processes truoc khi retry
                self._kill_orphaned_chromium()
                time.sleep(2)
                self._clean_lock_files(abs_data_dir)

                # Lan thu 2: xoa profile bi corrupt va tao lai
                if attempt == 1:
                    import shutil
                    if os.path.exists(abs_data_dir):
                        shutil.rmtree(abs_data_dir, ignore_errors=True)
                    os.makedirs(abs_data_dir, exist_ok=True)
                    print("[Translator] Cleared browser_data, retrying...")
                    log_event("browser.profile_reset", profile_dir=abs_data_dir)

                await asyncio.sleep(2)

        log_event("browser.launch_exhausted",
                  attempts=3,
                  last_error=str(last_error)[:200] if last_error else "")
        raise last_error

    async def cleanup(self):
        """Dong Playwright instance va cleanup processes."""
        if self._hybrid:
            # Khong co browser/Playwright de dong; bridge song doc lap.
            log_event("browser.cleanup_done", mode="hybrid")
            return
        # Release the leased account back to the pool first — even if browser
        # teardown later raises, the lease shouldn't leak.
        if SCHEDULER_ENABLED and self._account is not None:
            released_email = self._account.email
            try:
                from app.pools.account_pool import get_account_pool
                get_account_pool().release(self._account.email, self._worker_id)
                log_event("scheduler.released",
                          account_email=released_email,
                          worker_id=self._worker_id)
            except Exception as e:
                logger.debug("release failed: %s", e)
                log_event("scheduler.release_failed",
                          account_email=released_email,
                          error=str(e)[:200])
            finally:
                self._account = None

        log_event("browser.cleanup_started", mode=settings.TRANSLATOR_MODE)
        if settings.TRANSLATOR_MODE == "cdp":
            # CDP mode: chi dong tab da mo, KHONG kill Chrome cua user
            if self._cdp_page:
                try:
                    await self._cdp_page.close()
                except Exception:
                    pass
                self._cdp_page = None
            if self._cdp_browser:
                try:
                    await self._cdp_browser.close()
                except Exception:
                    pass
                self._cdp_browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
        else:
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            # Kill any leftover Chromium processes (only in new_browser mode)
            self._kill_orphaned_chromium()
        log_event("browser.cleanup_done", mode=settings.TRANSLATOR_MODE)

    async def _send_prompt_and_get_response(self, page: Page, prompt: str) -> str:
        """Gui prompt vao AI web (Gemini hoac ChatGPT) va scrape ket qua."""
        if self._hybrid:
            return await self._send_via_bridge(prompt)

        backend = self._backend

        existing_count = await backend.count_responses(page)
        print(f"  [Translator] [{self._backend_name}] Response hien tai: {existing_count}")

        t_send = time.time()
        log_event("web.prompt_send_start",
                  backend=self._backend_name,
                  prompt_chars=len(prompt),
                  existing_response_count=existing_count)
        await backend.send_input(page, prompt)
        send_input_ms = round((time.time() - t_send) * 1000)
        print("  [Translator] Da gui prompt, dang cho response...")
        log_event("web.prompt_sent",
                  backend=self._backend_name,
                  send_input_ms=send_input_ms)

        # Giai doan 1: Cho response XUAT HIEN (toi da 90s)
        APPEAR_TIMEOUT_S = 90
        appeared_at = None
        t_appear = time.time()
        for i in range(APPEAR_TIMEOUT_S // 2):
            await asyncio.sleep(2)
            current_count = await backend.count_responses(page)
            if current_count > existing_count:
                appeared_at = i * 2
                print(f"  [Translator] Response bat dau xuat hien ({appeared_at}s)")
                log_event("web.response_appeared",
                          backend=self._backend_name,
                          appeared_after_seconds=appeared_at,
                          time_to_appear_ms=round((time.time() - t_appear) * 1000))
                await asyncio.sleep(3)
                break
            if i > 0 and i % 15 == 0:
                print(f"  [Translator] Van cho response xuat hien... ({i*2}s)")

        if appeared_at is None:
            log_event("web.response_appear_timeout",
                      backend=self._backend_name,
                      timeout_seconds=APPEAR_TIMEOUT_S)
            raise TimeoutError(
                f"{self._backend_name} khong phan hoi sau {APPEAR_TIMEOUT_S}s — "
                "co the bi treo. Se thu lai voi session moi."
            )

        # Giai doan 2: Cho response HOAN THANH (toi da 4 phut)
        DONE_TIMEOUT_S = 240
        t_done = time.time()
        done_reached = False
        for i in range(DONE_TIMEOUT_S // 2):
            await asyncio.sleep(2)
            is_done = await backend.is_response_done(page)
            if is_done:
                await asyncio.sleep(2)
                if await backend.is_response_done(page):
                    print(f"  [Translator] Response hoan thanh! ({appeared_at + i*2}s)")
                    await asyncio.sleep(1)
                    done_reached = True
                    break
            if i > 0 and i % 15 == 0:
                elapsed = appeared_at + i * 2
                print(f"  [Translator] Van dang cho hoan thanh... ({elapsed}s)")

        done_ms = round((time.time() - t_done) * 1000)
        # Scrape response cuoi cung
        text = await backend.get_last_response_text(page)
        if text:
            print(f"  [Translator] Nhan duoc {len(text)} ky tu")
        else:
            print("  [Translator] CANH BAO: Khong nhan duoc response!")
        log_event("web.response_scraped",
                  backend=self._backend_name,
                  generation_ms=done_ms,
                  done_signal_reached=done_reached,
                  text_chars=len(text or ""),
                  total_ms=round((time.time() - t_send) * 1000))
        return text

    async def _send_via_bridge(self, prompt: str) -> str:
        """Hybrid transport: day prompt qua bridge, doi userscript tra ket qua.

        Thay cho toan bo vong send_input -> count_responses -> is_response_done
        -> get_last_response_text (giờ chạy trong userscript). Giữ nguyên các
        log_event quan trọng để chương kết quả DATN vẫn có số (latency/throughput).
        Raise khi lỗi/timeout → vòng retry/failover sẵn có tự xử lý.
        """
        from app.services import hybrid_bridge

        t_send = time.time()
        log_event("web.prompt_send_start",
                  backend=self._backend_name,
                  prompt_chars=len(prompt),
                  transport="hybrid")
        try:
            job_id = await hybrid_bridge.submit(prompt, self._backend_name)
        except Exception as e:
            log_event("web.hybrid_submit_failed",
                      backend=self._backend_name, error=str(e)[:200])
            raise
        print(f"  [Translator] [hybrid:{self._backend_name}] job {job_id} — "
              "cho worker (tab AI) xu ly...")

        try:
            job = await hybrid_bridge.wait(job_id)
        except asyncio.TimeoutError as e:
            log_event("web.hybrid_job_timeout",
                      backend=self._backend_name, job_id=job_id)
            raise TimeoutError(
                f"{self._backend_name} (hybrid) khong phan hoi job {job_id} — "
                "kiem tra tab AI + userscript con mo va dang nhap."
            ) from e

        text = job.get("result") or ""
        timings = job.get("timings") or {}
        log_event("web.response_scraped",
                  backend=self._backend_name,
                  transport="hybrid",
                  job_id=job_id,
                  generation_ms=timings.get("generate_ms"),
                  text_chars=len(text),
                  total_ms=round((time.time() - t_send) * 1000))
        if text:
            print(f"  [Translator] [hybrid] nhan {len(text)} ky tu (job {job_id})")
        else:
            print(f"  [Translator] [hybrid] CANH BAO: job {job_id} tra chuoi rong")
        return text

    async def start_new_chat(self, page: Page):
        """Bat dau session moi — delegate toi backend cu the."""
        await self._backend.start_new_chat(page)

    async def build_glossary(self, page: Page, latex_content: str) -> dict[str, str]:
        """Xây dựng bảng thuật ngữ thống nhất cho toàn bài báo."""
        prompt = (
            "Đọc nội dung LaTeX sau và trích xuất tất cả thuật ngữ khoa học/kỹ thuật. "
            "Với mỗi thuật ngữ, đề xuất cách dịch sang tiếng Việt phù hợp nhất. "
            'Trả về CHỈ dạng JSON, không giải thích: {"term_en": "term_vi", ...}\n\n'
            f"```latex\n{latex_content[:8000]}\n```"
        )
        response = await self._send_prompt_and_get_response(page, prompt)
        # TODO: Parse JSON response and populate self.glossary
        return self.glossary

    async def translate_chunk(self, page: Page, latex_chunk: str) -> str:
        """Dịch một đoạn LaTeX, giữ nguyên tất cả markup."""
        # Filter glossary to terms appearing in this chunk; locked terms get
        # a stronger directive section in the prompt.
        from app.pdf.glossary import filter_glossary_for_chunk, format_glossary_for_prompt
        filtered = filter_glossary_for_chunk(
            self.glossary, latex_chunk, locked=self.locked_terms
        )
        glossary_section = format_glossary_for_prompt(filtered, locked=self.locked_terms)

        prompt = (
            "Dịch nội dung LaTeX sau sang tiếng Việt.\n\n"
            "=== QUY TẮC BẮT BUỘC (TUYỆT ĐỐI KHÔNG ĐƯỢC VI PHẠM) ===\n"
            "1. GIỮ NGUYÊN 100% tất cả LaTeX commands, environments, math mode ($...$, $$...$$, \\[...\\]), comments (%...)\n"
            "2. TUYỆT ĐỐI KHÔNG ĐƯỢC xóa, sửa, hay bỏ sót bất kỳ \\begin{...} hoặc \\end{...} nào. "
            "Mỗi \\begin{...} PHẢI có \\end{...} tương ứng trong output.\n"
            "3. KHÔNG xóa, sửa, hay di chuyển các lệnh cấu trúc: "
            "\\section, \\subsection, \\caption, \\label, \\ref, \\cite, \\footnote, "
            "\\begin{figure}, \\end{figure}, \\begin{table}, \\end{table}, "
            "\\begin{abstract}, \\end{abstract}, \\begin{equation}, \\end{equation}, v.v.\n"
            "4. CHỈ dịch phần TEXT THUẦN tiếng Anh sang tiếng Việt. Giữ nguyên mọi thứ khác.\n"
            "5. TUYỆT ĐỐI KHÔNG thêm bất kỳ giải thích, ghi chú, câu hỏi, hay bình luận nào. "
            "KHÔNG hỏi 'Bạn có muốn...', KHÔNG viết 'Lưu ý:', KHÔNG thêm gì ngoài LaTeX.\n"
            "6. Trả về CHỈ code LaTeX đã dịch bên trong block ```latex ... ```. "
            "TUYỆT ĐỐI KHÔNG có bất kỳ text nào trước hoặc sau block ```latex...```.\n\n"
            "=== VÍ DỤ ===\n"
            "Input:  \\begin{abstract} This is a test. \\end{abstract}\n"
            "Output: ```latex\n\\begin{abstract} Đây là một bài kiểm tra. \\end{abstract}\n```\n"
            "(Chú ý: CHỈ có block ```latex...``` trong output, không có gì khác)\n\n"
        )
        if glossary_section:
            prompt += glossary_section
        prompt += f"=== NỘI DUNG CẦN DỊCH ===\n```latex\n{latex_chunk}\n```"

        t0 = time.time()
        success = False
        try:
            raw_response = await self._send_prompt_and_get_response(page, prompt)
            translated = self._extract_latex_from_response(raw_response)
            validated = self._validate_environments(latex_chunk, translated)
            success = bool(validated and validated.strip())
            return validated
        finally:
            # Report per-chunk outcome to the scheduler's history so the
            # adaptive strategy can re-weight accounts by recent success/latency.
            if SCHEDULER_ENABLED and self._account is not None:
                latency = time.time() - t0
                try:
                    from app.pools.account_pool import get_account_pool
                    get_account_pool().report_outcome(
                        self._account.email, success=success, latency=latency,
                    )
                    log_event("scheduler.outcome_reported",
                              account_email=self._account.email,
                              success=success,
                              latency_seconds=round(latency, 3),
                              source="latex_chunk")
                except Exception as e:
                    logger.debug("report_outcome failed: %s", e)
                    log_event("scheduler.report_outcome_failed",
                              account_email=self._account.email,
                              error=str(e)[:200])

    async def translate_chunk_improved(
        self,
        page,
        src_latex: str,
        bad_mt_latex: str,
        score_pct: float,
        glossary: dict | None = None,
    ) -> str:
        """Dịch lại chunk có chất lượng thấp.

        Dùng HeuristicCritic để sinh error list cụ thể → inject vào prompt Refiner
        thay vì chỉ nói "điểm thấp, dịch lại". Giúp Gemini biết chính xác cần sửa gì.
        """
        import re as _re

        # ── Critic: phân tích lỗi cụ thể ─────────────────────────────────────
        try:
            from app.pdf.critic import HeuristicCritic
            # Strip LaTeX commands để critic phân tích text thuần
            def _strip_latex(s: str) -> str:
                s = _re.sub(r'\\[a-zA-Z]+\*?(\[.*?\])?(\{[^}]*\})*', ' ', s)
                return _re.sub(r'\s+', ' ', s).strip()

            src_plain = _strip_latex(src_latex)
            mt_plain  = _strip_latex(bad_mt_latex)
            critic = HeuristicCritic()
            critique = critic.critique(src_plain, mt_plain, glossary=glossary or {}, block_id=0)
            critique_text = critique.format_errors_for_prompt() if critique.has_errors() else ""
        except Exception:
            critique_text = ""

        # ── Build prompt ──────────────────────────────────────────────────────
        if critique_text:
            # Refiner prompt: có error list → sửa đúng chỗ
            glossary_section = ""
            if glossary:
                terms = [f'"{en}" → "{vi}"' for en, vi in list(glossary.items())[:30]]
                if terms:
                    glossary_section = "=== BẢNG THUẬT NGỮ ===\n" + "\n".join(terms) + "\n\n"
            prompt = (
                "Bạn là chuyên gia hiệu đính bản dịch học thuật Anh-Việt (LaTeX).\n\n"
                f"Bản dịch dưới đây có điểm chất lượng thấp ({score_pct:.0f}%). "
                "Hãy sửa đúng theo danh sách lỗi được chỉ ra.\n\n"
                + glossary_section
                + "=== QUY TẮC BẮT BUỘC ===\n"
                "1. GIỮ NGUYÊN 100% LaTeX commands, environments, math, comments\n"
                "2. TUYỆT ĐỐI KHÔNG xóa \\begin{...}/\\end{...} nào\n"
                "3. CHỈ sửa những lỗi được liệt kê — KHÔNG thay đổi phần đã dịch đúng\n"
                "4. Trả về CHỈ code LaTeX bên trong block ```latex ... ```, không thêm gì khác\n\n"
                "=== VĂN BẢN GỐC (EN) ===\n"
                f"```latex\n{src_latex}\n```\n\n"
                "=== BẢN DỊCH CŨ (VI) — CÓ LỖI ===\n"
                f"```latex\n{bad_mt_latex}\n```\n\n"
                "=== LỖI CẦN SỬA ===\n"
                f"{critique_text}\n\n"
                "Viết lại bản dịch đã sửa lỗi bên trong block ```latex ... ```:"
            )
        else:
            # Không detect được lỗi cụ thể → fallback: yêu cầu dịch lại tổng quát
            prompt = (
                f"Bản dịch sau đây có điểm chất lượng thấp ({score_pct:.0f}%). "
                "Hãy dịch lại chính xác và tự nhiên hơn.\n\n"
                "=== QUY TẮC BẮT BUỘC ===\n"
                "1. GIỮ NGUYÊN 100% tất cả LaTeX commands, environments, math, comments\n"
                "2. TUYỆT ĐỐI KHÔNG xóa \\begin{...}/\\end{...} nào\n"
                "3. CHỈ dịch TEXT THUẦN tiếng Anh, giữ nguyên mọi thứ khác\n"
                "4. Trả về CHỈ code LaTeX bên trong block ```latex ... ```, không thêm gì khác\n\n"
                "=== VĂN BẢN GỐC (EN) ===\n"
                f"```latex\n{src_latex}\n```\n\n"
                "=== BẢN DỊCH CŨ (VI) — chất lượng thấp, cần cải thiện ===\n"
                f"```latex\n{bad_mt_latex}\n```\n\n"
                "Hãy tạo bản dịch VI mới, tốt hơn bản cũ:"
            )
        raw_response = await self._send_prompt_and_get_response(page, prompt)
        translated = self._extract_latex_from_response(raw_response)
        validated = self._validate_environments(src_latex, translated)
        return validated

    async def refine_with_hint(
        self,
        page,
        original: str,
        prev_translation: str,
        hint: str,
        is_latex: bool = True,
    ) -> str:
        """Re-translate one chunk steered by a user hint.

        Used by the per-chunk "Suggest correction" UI: user types a phrasing
        preference (e.g. "translate 'transformer' as 'mô hình biến đổi'") and
        Gemini regenerates the chunk honoring that hint.

        For LaTeX (is_latex=True), preserves all markup and runs environment
        validation. For PDF, the original is plain numbered text blocks
        (`[1] ...\\n[2] ...`); the prompt instructs the model to keep block
        numbers and structure.
        """
        hint_clean = (hint or "").strip()

        if is_latex:
            prompt = (
                "Bạn là chuyên gia hiệu đính bản dịch học thuật Anh-Việt (LaTeX).\n\n"
                "Người dùng yêu cầu chỉnh sửa bản dịch theo gợi ý cụ thể bên dưới. "
                "Hãy dịch lại đoạn này, áp dụng đúng gợi ý đó, đồng thời giữ nguyên "
                "100% LaTeX commands, environments, math, comments.\n\n"
                "=== QUY TẮC BẮT BUỘC ===\n"
                "1. GIỮ NGUYÊN 100% LaTeX commands, environments ($...$, \\begin/\\end), math, comments\n"
                "2. TUYỆT ĐỐI KHÔNG xóa hoặc bỏ sót \\begin{...}/\\end{...} nào\n"
                "3. ÁP DỤNG GỢI Ý của người dùng — đó là yêu cầu cao nhất\n"
                "4. Giữ những phần đã dịch tốt; chỉ sửa phần liên quan đến gợi ý "
                "trừ khi gợi ý yêu cầu dịch lại toàn bộ\n"
                "5. Trả về CHỈ code LaTeX bên trong block ```latex ... ```\n\n"
                "=== VĂN BẢN GỐC (EN) ===\n"
                f"```latex\n{original}\n```\n\n"
                "=== BẢN DỊCH HIỆN TẠI (VI) ===\n"
                f"```latex\n{prev_translation}\n```\n\n"
                "=== GỢI Ý CỦA NGƯỜI DÙNG ===\n"
                f"{hint_clean}\n\n"
                "Viết lại bản dịch đã áp dụng gợi ý bên trong block ```latex ... ```:"
            )
            raw = await self._send_prompt_and_get_response(page, prompt)
            translated = self._extract_latex_from_response(raw)
            return self._validate_environments(original, translated)

        # PDF / plain text path — original looks like "[1] ...\n[2] ...".
        prompt = (
            "Bạn là chuyên gia hiệu đính bản dịch học thuật Anh-Việt.\n\n"
            "Người dùng yêu cầu chỉnh sửa bản dịch theo gợi ý cụ thể bên dưới. "
            "Hãy dịch lại đoạn này, áp dụng đúng gợi ý đó.\n\n"
            "=== QUY TẮC BẮT BUỘC ===\n"
            "1. GIỮ NGUYÊN số block [1], [2], ... — không gộp/tách block\n"
            "2. ÁP DỤNG GỢI Ý của người dùng — đó là yêu cầu cao nhất\n"
            "3. Giữ những câu đã dịch tốt; chỉ sửa phần liên quan đến gợi ý\n"
            "4. KHÔNG dịch placeholder dạng MATH_PLACEHOLDER_xxx — giữ nguyên\n"
            "5. Trả về CHỈ phần văn bản đã dịch, không thêm giải thích\n\n"
            "=== VĂN BẢN GỐC (EN) ===\n"
            f"{original}\n\n"
            "=== BẢN DỊCH HIỆN TẠI (VI) ===\n"
            f"{prev_translation}\n\n"
            "=== GỢI Ý CỦA NGƯỜI DÙNG ===\n"
            f"{hint_clean}\n\n"
            "Viết lại bản dịch đã áp dụng gợi ý:"
        )
        raw = await self._send_prompt_and_get_response(page, prompt)
        # For PDF text, no latex extraction; just strip code-fence wrappers if any
        text = raw.strip()
        import re as _re
        m = _re.search(r"```(?:[a-z]*)?\s*\n?(.*?)```", text, _re.DOTALL)
        if m:
            text = m.group(1).strip()
        return text

    @staticmethod
    def _extract_latex_from_response(response: str) -> str:
        """Trích xuất code LaTeX từ response, bỏ phần giải thích bên ngoài block."""
        if not response:
            return response
        import re
        # Tìm block ```latex ... ``` hoặc ``` ... ```
        match = re.search(r'```(?:latex)?\s*\n(.*?)```', response, re.DOTALL)
        if match:
            return match.group(1).strip()

        # Fallback: loại bỏ chatbot artifacts phổ biến
        text = response.strip()
        # Bỏ các dòng "Code snippet" ở đầu
        if text.lower().startswith("code snippet"):
            text = text.split("\n", 1)[-1].strip() if "\n" in text else text
        # Bỏ các câu hỏi/ghi chú cuối response từ chatbot
        # Bao gồm cả prompt markers khi Gemini echo lại prompt dịch
        lines = text.split("\n")
        clean_lines = []
        for line in lines:
            stripped = line.strip()
            # Detect chatbot artifacts
            if re.match(r'^(Bạn có muốn|Lưu ý|Note:|Chú ý:|Would you|Let me know|Nếu bạn cần|Hy vọng)', stripped, re.IGNORECASE):
                break
            # Detect prompt leakage — Gemini echoing back the translation prompt
            if re.match(r'^(===\s*(QUY TẮC|NỘI DUNG CẦN DỊCH|VÍ DỤ|BẢNG THUẬT NGỮ)|Dịch nội dung LaTeX sau sang tiếng Việt)', stripped):
                break
            clean_lines.append(line)
        # Bỏ dòng trống thừa ở cuối
        while clean_lines and not clean_lines[-1].strip():
            clean_lines.pop()
        return "\n".join(clean_lines)

    @staticmethod
    def _validate_environments(original: str, translated: str) -> str:
        """Kiểm tra và sửa lỗi mất \\begin/\\end trong bản dịch.

        So sánh danh sách \\begin{env} và \\end{env} giữa bản gốc và bản dịch.
        Nếu bản dịch thiếu \\end{env} hoặc \\begin{env} so với bản gốc, thêm lại.
        """
        if not translated:
            return translated

        import re

        def extract_env_markers(text: str) -> list[str]:
            """Trả về danh sách theo thứ tự các \\begin{env} và \\end{env}."""
            return re.findall(r'\\(?:begin|end)\{[^}]+\}', text)

        orig_markers = extract_env_markers(original)
        trans_markers = extract_env_markers(translated)

        if orig_markers == trans_markers:
            return translated  # OK, khớp hoàn toàn

        # Đếm từng marker
        from collections import Counter
        orig_counts = Counter(orig_markers)
        trans_counts = Counter(trans_markers)

        missing = []
        for marker, count in orig_counts.items():
            diff = count - trans_counts.get(marker, 0)
            if diff > 0:
                missing.extend([marker] * diff)

        if missing:
            print(f"  [Translator] CANH BAO: Ban dich thieu {len(missing)} environment marker(s): {missing}")
            # Thêm các marker bị thiếu vào cuối bản dịch
            # Ưu tiên thêm \end{...} vào cuối (trường hợp phổ biến nhất)
            ends = [m for m in missing if m.startswith('\\end')]
            begins = [m for m in missing if m.startswith('\\begin')]
            if begins:
                translated = '\n'.join(begins) + '\n' + translated
            if ends:
                translated = translated + '\n' + '\n'.join(ends)

        return translated
