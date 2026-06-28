"""Quick probe: load chatgpt.com via the same Playwright setup the backend uses.

Detects Cloudflare challenge via: page title, URL, known DOM markers, text snippet.
Saves a screenshot for visual inspection.
"""
import asyncio
import os
import sys
import time
from playwright.async_api import async_playwright

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-notifications",
]
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
"""

CF_DOM_SELECTORS = [
    "#challenge-form",
    "#challenge-stage",
    ".cf-challenge",
    "iframe[src*='challenges.cloudflare.com']",
    "div[class*='challenge']",
]
CF_TEXT_PATTERNS = [
    "just a moment",
    "verify you are human",
    "checking your browser",
    "needs to review the security",
    "ray id",
    "cloudflare",
]


async def probe(url: str, wait_seconds: int = 15) -> dict:
    out = {"url_initial": url}
    user_data_dir = os.path.abspath("./browser_data_test_chatgpt")
    os.makedirs(user_data_dir, exist_ok=True)

    async with async_playwright() as pw:
        t_launch = time.time()
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
        out["launch_seconds"] = round(time.time() - t_launch, 2)

        t_nav = time.time()
        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            out["nav_seconds"] = round(time.time() - t_nav, 2)
            out["nav_ok"] = True
        except Exception as e:
            out["nav_seconds"] = round(time.time() - t_nav, 2)
            out["nav_ok"] = False
            out["nav_error"] = repr(e)

        await asyncio.sleep(wait_seconds)

        out["url_final"] = page.url
        out["title"] = await page.title()

        try:
            body_text = (await page.evaluate("document.body.innerText"))[:2000]
        except Exception:
            body_text = ""
        out["body_text_head"] = body_text[:400]

        lower = body_text.lower()
        out["cf_text_hits"] = [p for p in CF_TEXT_PATTERNS if p in lower]

        dom_hits = []
        for sel in CF_DOM_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    dom_hits.append(sel)
            except Exception:
                pass
        out["cf_dom_hits"] = dom_hits

        out["cloudflare_blocked"] = bool(out["cf_text_hits"] or out["cf_dom_hits"])

        shot_path = os.path.abspath("./chatgpt_cloudflare_probe.png")
        try:
            await page.screenshot(path=shot_path, full_page=False)
            out["screenshot"] = shot_path
        except Exception as e:
            out["screenshot_error"] = repr(e)

        await context.close()
    return out


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://chatgpt.com"
    result = await probe(url, wait_seconds=15)
    print("=" * 60)
    for k, v in result.items():
        print(f"{k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
