"""End-to-end ChatGPT backend test: navigate, send a translation prompt, scrape.

Reuses the project's ChatGPTBackend so the same selectors and VLM fallback
that production uses are exercised here.
"""
import asyncio
import io
import os
import sys
import time
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.services.translator import ChatGPTBackend

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
    "Translate the following English sentence into Vietnamese. "
    "Reply with the Vietnamese translation only, no quotes.\n\n"
    "Hello world, this is a test of the translation system."
)


async def main():
    user_data_dir = os.path.abspath("./browser_data_test_chatgpt")
    os.makedirs(user_data_dir, exist_ok=True)
    backend = ChatGPTBackend()

    result = {"prompt": PROMPT}

    async with async_playwright() as pw:
        t0 = time.time()
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            args=STEALTH_ARGS,
            viewport={"width": 1366, "height": 820},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = context.pages[0] if context.pages else await context.new_page()
        print(f"[step] launch ok ({time.time() - t0:.2f}s)")

        await page.goto(backend.url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(6)
        # Pre-check whether the hardcoded selector resolves
        for sel in ("#prompt-textarea", 'div[contenteditable="true"]', "textarea[placeholder]"):
            el = await page.query_selector(sel)
            print(f"[probe] selector {sel!r}: {'FOUND' if el else 'missing'}")
        result["url_after_nav"] = page.url
        result["title_after_nav"] = await page.title()
        print(f"[step] navigated → {page.url} (title={result['title_after_nav']!r})")

        # Detect login wall
        body = (await page.evaluate("document.body.innerText"))[:1500]
        result["body_snippet"] = body[:300]
        login_required = (
            "Log in" in body and "Sign up" in body and "Where should we begin?" not in body
        )
        result["login_required"] = login_required

        # Try send
        before = await backend.count_responses(page)
        result["count_before"] = before
        print(f"[step] count_responses before: {before}")

        try:
            t_send = time.time()
            await backend.send_input(page, PROMPT)
            result["send_seconds"] = round(time.time() - t_send, 2)
            result["send_ok"] = True
            print(f"[step] send_input ok ({result['send_seconds']}s)")
        except Exception as e:
            result["send_ok"] = False
            result["send_error"] = repr(e)
            print(f"[step] send_input FAILED: {e}")

        if result["send_ok"]:
            # Poll for response done
            t_wait = time.time()
            timeout = 90
            last_text = ""
            while time.time() - t_wait < timeout:
                done = await backend.is_response_done(page)
                cnt = await backend.count_responses(page)
                if done and cnt > before:
                    break
                await asyncio.sleep(1.5)
            result["wait_seconds"] = round(time.time() - t_wait, 2)
            result["count_after"] = await backend.count_responses(page)
            result["response"] = (await backend.get_last_response_text(page))[:500]
            print(f"[step] response after {result['wait_seconds']}s")

        try:
            shot = os.path.abspath("./chatgpt_end_to_end.png")
            await page.screenshot(path=shot)
            result["screenshot"] = shot
        except Exception as e:
            result["screenshot_error"] = repr(e)

        await context.close()

    print("=" * 60)
    for k, v in result.items():
        print(f"{k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
