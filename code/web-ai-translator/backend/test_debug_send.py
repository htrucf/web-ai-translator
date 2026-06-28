"""Debug: tim nut Send tren Gemini sau khi da gui 1 prompt."""

import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app.services.translator import WebAITranslator


async def main():
    translator = WebAITranslator(user_data_dir="./browser_data")

    print("[1] Mo browser...")
    context, page = await translator.launch_browser()
    await asyncio.sleep(3)

    # Gui prompt dau tien bang tay - paste + tim nut send
    print("[2] Gui prompt 1...")
    input_sel = 'div.ql-editor[role="textbox"], div[contenteditable="true"][role="textbox"]'
    await page.wait_for_selector(input_sel, timeout=15000)
    await page.click(input_sel)
    await page.evaluate(
        """async (text) => { await navigator.clipboard.writeText(text); }""",
        "Xin chao, tra loi ngan thoi: 1+1=?",
    )
    await page.keyboard.press("Control+KeyV")
    await asyncio.sleep(1)

    # Chup truoc khi gui
    await page.screenshot(path="debug_before_send.png")
    print("[3] Chup debug_before_send.png")

    # Tim tat ca button tren trang
    print("\n[4] Tat ca button co aria-label:")
    buttons = await page.evaluate("""() => {
        const results = [];
        document.querySelectorAll('button').forEach(el => {
            const label = el.getAttribute('aria-label') || '';
            const tooltip = el.getAttribute('mattooltip') || '';
            const text = (el.innerText || '').substring(0, 50);
            const visible = el.offsetParent !== null;
            const rect = el.getBoundingClientRect();
            results.push({
                ariaLabel: label,
                text: text,
                tooltip: tooltip,
                visible: visible,
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                classes: (el.className || '').substring(0, 100),
            });
        });
        return results;
    }""")
    for i, b in enumerate(buttons):
        if b.get('visible'):
            print(f"  [{i}] label='{b['ariaLabel']}' text='{b['text']}' tooltip='{b['tooltip']}' pos=({b['x']},{b['y']}) size={b['w']}x{b['h']}")

    # Tim element co the la nut send (icon, submit)
    print("\n[5] Tim nut send cu the...")
    send_candidates = await page.evaluate("""() => {
        const results = [];
        const keywords = ['send', 'submit', 'gui', 'Search'];
        document.querySelectorAll('button, [role="button"]').forEach(el => {
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            const text = (el.innerText || '').toLowerCase();
            const tooltip = (el.getAttribute('mattooltip') || '').toLowerCase();
            const all = label + ' ' + text + ' ' + tooltip;
            for (const kw of keywords) {
                if (all.includes(kw.toLowerCase())) {
                    results.push({
                        tag: el.tagName,
                        ariaLabel: el.getAttribute('aria-label'),
                        text: (el.innerText || '').substring(0, 50),
                        tooltip: el.getAttribute('mattooltip'),
                        visible: el.offsetParent !== null,
                        classes: (el.className || '').substring(0, 150),
                    });
                    break;
                }
            }
        });
        return results;
    }""")
    for c in send_candidates:
        print(f"  <{c['tag']}> label='{c['ariaLabel']}' text='{c['text']}' tooltip='{c['tooltip']}' visible={c['visible']}")
        print(f"    classes: {c['classes']}")

    print("\n[6] Dong browser sau 10 giay...")
    await asyncio.sleep(10)
    await context.close()
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
