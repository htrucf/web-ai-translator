"""ChatGPT end-to-end with the 3 fixes:
  1. Dismiss cookie banner
  2. Target visible div#prompt-textarea directly (not the hidden fallback textarea)
  3. Submit via Enter (no send button until user types)
"""
import asyncio
import io
import os
import sys
import time
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-notifications",
]
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
"""
PROMPT = (
    "Translate this English sentence to Vietnamese (reply with the Vietnamese only, no quotes): "
    "Hello world, this is a test of the translation system."
)


async def main():
    user_data_dir = os.path.abspath("./browser_data_test_chatgpt")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            args=STEALTH_ARGS,
            viewport={"width": 1366, "height": 820},
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://chatgpt.com", timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(6)

        # FIX 1: Dismiss cookie banner
        for sel in ('button:has-text("Reject non-essential")',
                    'button:has-text("Reject")',
                    'button:has-text("Accept all")'):
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    print(f"[cookie] dismissed via {sel!r}")
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        # FIX 2: Target visible div#prompt-textarea directly
        try:
            await page.wait_for_selector(
                'div#prompt-textarea[contenteditable="true"]', timeout=15000
            )
            await page.click('div#prompt-textarea[contenteditable="true"]')
            print("[input] focused div#prompt-textarea")
        except Exception as e:
            print(f"[input] FAIL: {e!r}")
            await context.close()
            return

        # Type the prompt
        await page.keyboard.type(PROMPT, delay=10)
        await asyncio.sleep(1)

        # FIX 3: Submit via Enter
        await page.keyboard.press("Enter")
        print("[send] pressed Enter")
        t_send = time.time()

        # Wait for assistant response: poll for an assistant turn appearing
        timeout = 60
        last_count = 0
        done = False
        last_text = ""
        while time.time() - t_send < timeout:
            els = await page.query_selector_all(
                'article[data-testid^="conversation-turn-"][data-testid$="-assistant"], '
                'div[data-message-author-role="assistant"], '
                '.agent-turn'
            )
            count = len(els)
            stop_btn = await page.query_selector(
                'button[aria-label="Stop streaming"], button[data-testid="stop-button"]'
            )
            stop_visible = (await stop_btn.is_visible()) if stop_btn else False
            if count > last_count and not stop_visible:
                # Got a turn AND not generating → likely done
                last_text = (await els[-1].inner_text()).strip()
                # double-check stable: wait 2s, read again
                await asyncio.sleep(2)
                stop_btn2 = await page.query_selector(
                    'button[aria-label="Stop streaming"], button[data-testid="stop-button"]'
                )
                if stop_btn2 and await stop_btn2.is_visible():
                    last_count = count
                    continue
                done = True
                break
            await asyncio.sleep(1)

        elapsed = round(time.time() - t_send, 2)
        print(f"[recv] done={done} elapsed={elapsed}s")
        print(f"[recv] response text:")
        print("-" * 60)
        print(last_text or "(empty)")
        print("-" * 60)

        try:
            shot = os.path.abspath("./chatgpt_fixed.png")
            await page.screenshot(path=shot)
            print(f"[shot] {shot}")
        except Exception as e:
            print(f"[shot] err {e}")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
