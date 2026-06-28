"""Inspect ChatGPT landing-page DOM to find the actual input + send selectors.

Dumps candidates with their attributes so we can patch ChatGPTBackend.
"""
import asyncio
import io
import os
import sys
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

DOM_PROBE_JS = r"""
() => {
  const summarize = (el) => {
    const attrs = {};
    for (const a of el.attributes || []) attrs[a.name] = a.value;
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      attrs,
      visible: rect.width > 0 && rect.height > 0,
      rect: { x: rect.x|0, y: rect.y|0, w: rect.width|0, h: rect.height|0 },
      text: (el.innerText || '').slice(0, 80),
    };
  };

  const input_candidates = Array.from(document.querySelectorAll(
    'textarea, [contenteditable="true"], [role="textbox"], input[type="text"]'
  )).map(summarize);

  const button_candidates = Array.from(document.querySelectorAll('button')).filter(b => {
    const al = (b.getAttribute('aria-label') || '').toLowerCase();
    const tid = (b.getAttribute('data-testid') || '').toLowerCase();
    const t = (b.innerText || '').toLowerCase();
    return al.includes('send') || al.includes('submit') ||
           tid.includes('send') || tid.includes('submit') ||
           t.includes('send');
  }).map(summarize);

  return { input_candidates, button_candidates, url: location.href };
}
"""


async def main():
    user_data_dir = os.path.abspath("./browser_data_test_chatgpt")
    os.makedirs(user_data_dir, exist_ok=True)

    async with async_playwright() as pw:
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

        await page.goto("https://chatgpt.com", timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(5)

        data = await page.evaluate(DOM_PROBE_JS)
        print(f"URL: {data['url']}")
        print("\n=== INPUT CANDIDATES ===")
        for i, c in enumerate(data["input_candidates"]):
            print(f"[{i}] tag={c['tag']} visible={c['visible']} rect={c['rect']}")
            print(f"    attrs={c['attrs']}")
            if c["text"]:
                print(f"    text={c['text']!r}")
        print(f"\n=== SEND-LIKE BUTTONS ({len(data['button_candidates'])}) ===")
        for i, c in enumerate(data["button_candidates"]):
            print(f"[{i}] tag={c['tag']} visible={c['visible']} rect={c['rect']}")
            print(f"    attrs={c['attrs']}")
            if c["text"]:
                print(f"    text={c['text']!r}")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
