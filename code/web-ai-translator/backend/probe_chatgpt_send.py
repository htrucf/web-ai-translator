"""Step-by-step trace of ChatGPTBackend.send_input to find the failing line."""
import asyncio
import io
import os
import sys
import traceback
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

PROMPT = "Translate to Vietnamese: Hello world."


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

        input_sel = (
            '#prompt-textarea, '
            'div[contenteditable="true"][data-id="root"], '
            'textarea[placeholder]'
        )
        print(f"[1] wait_for_selector({input_sel!r}, timeout=10000)")
        try:
            await page.wait_for_selector(input_sel, timeout=10000)
            print("    ok")
        except Exception as e:
            print(f"    FAIL: {e!r}")

        print(f"[2] page.click({input_sel!r})")
        try:
            await page.click(input_sel)
            print("    ok")
        except Exception as e:
            print(f"    FAIL: {e!r}")

        print("[3] clipboard write + Ctrl+V")
        try:
            await page.evaluate("async (t) => { await navigator.clipboard.writeText(t); }", PROMPT)
            await page.keyboard.press("Control+KeyV")
            await asyncio.sleep(1)
            print("    ok")
        except Exception as e:
            print(f"    FAIL: {e!r}")
            traceback.print_exc()

        # Read back what's in the input
        try:
            val = await page.evaluate(
                "() => { const el = document.querySelector('#prompt-textarea'); "
                "return el ? (el.innerText || el.value || '') : null; }"
            )
            print(f"[4] input value after paste: {val!r}")
        except Exception as e:
            print(f"[4] read input FAIL: {e!r}")

        # Check send button
        send_sels = [
            'button[data-testid="send-button"]',
            'button[aria-label="Send prompt"]',
            'button[aria-label="Send message"]',
        ]
        for s in send_sels:
            el = await page.query_selector(s)
            visible = (await el.is_visible()) if el else False
            print(f"[5] send selector {s!r}: {'visible' if visible else ('hidden' if el else 'missing')}")

        # Brute force: list all buttons currently visible near bottom
        buttons = await page.evaluate(r"""
            () => Array.from(document.querySelectorAll('button')).map(b => {
                const r = b.getBoundingClientRect();
                return {
                    visible: r.width > 0 && r.height > 0,
                    rect: { x: r.x|0, y: r.y|0, w: r.width|0, h: r.height|0 },
                    aria_label: b.getAttribute('aria-label'),
                    testid: b.getAttribute('data-testid'),
                    text: (b.innerText || '').slice(0, 40),
                };
            }).filter(b => b.visible && b.rect.y > 300)
        """)
        print(f"[6] visible buttons (y>300): {len(buttons)}")
        for b in buttons[:15]:
            print(f"    {b}")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
