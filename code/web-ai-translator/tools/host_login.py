"""One-time Gemini login helper — chay tren HOST de tao Playwright profile.

Cach dung:
    1. Cai dependencies tren host (KHONG can full backend env):
         pip install playwright
         python -m playwright install chromium

    2. Chay script:
         python tools/host_login.py
       (mac dinh tao thu muc ./browser_data ngay canh script)

    3. Mot cua so Chrome se mo, di toi gemini.google.com.
       Dang nhap Google account binh thuong (Email + Password + 2FA).
       Khi thay giao dien chat Gemini -> dong cua so (hoac Ctrl+C terminal).

    4. Copy profile vao Docker volume:
         a) Cach don gian (bind mount): sua docker-compose.yml,
            doi `browser_data:/data/browser_data`
            thanh   `./browser_data:/data/browser_data`
         b) Hoac copy vao named volume:
            docker run --rm \\
              -v "$(pwd)/browser_data":/src:ro \\
              -v web-ai-translator_browser_data:/dst \\
              alpine sh -c "cp -a /src/. /dst/"

    5. Khoi dong stack:
         docker compose up -d

Luu y:
    - Khong dang nhap qua scripted login — Google se block.
    - 2FA: hoan thanh tren cua so browser (script khong tu dong duoc).
    - Cookie Gemini song lau (vai tuan); refresh dinh ky bang cach chay lai.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path


GEMINI_URL = "https://gemini.google.com/"


async def run_login(profile_dir: Path, headless: bool, target_url: str) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "[host_login] Thieu Playwright. Cai bang:\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    profile_dir.mkdir(parents=True, exist_ok=True)
    abs_profile = str(profile_dir.resolve())
    print(f"[host_login] Profile dir: {abs_profile}")
    print(f"[host_login] Mo {target_url} ...")
    print("[host_login] Dang nhap xong -> dong cua so Chrome de thoat.\n")

    stealth_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-notifications",
    ]

    async with async_playwright() as p:
        # Uu tien Chrome that (channel=chrome) — it bi detect hon Playwright
        # Chromium. Fallback ve Chromium neu Chrome chua cai.
        try:
            context = await p.chromium.launch_persistent_context(
                abs_profile,
                channel="chrome",
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=stealth_args,
            )
            print("[host_login] Launched: system Chrome (channel=chrome)")
        except Exception as e:
            print(f"[host_login] Chrome khong co ({e}), fallback Chromium")
            context = await p.chromium.launch_persistent_context(
                abs_profile,
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=stealth_args,
            )

        # Xoa dau hieu automation
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined}); window.chrome = { runtime: {} };"
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(target_url, timeout=120_000, wait_until="domcontentloaded")
        print("[host_login] Cua so da mo. Sau khi dang nhap xong, dong cua so.")

        # Cho user dong cua so. Khi tat ca page dong -> context close -> loop exit.
        closed_event = asyncio.Event()
        context.on("close", lambda _ctx: closed_event.set())

        try:
            await closed_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await context.close()
            except Exception:
                pass

    print(f"\n[host_login] Profile da luu tai: {abs_profile}")
    print("[host_login] Buoc tiep theo: copy/mount vao container — xem docs/DOCKER.md")


def main() -> None:
    parser = argparse.ArgumentParser(description="Host-side Gemini login helper.")
    parser.add_argument(
        "--profile-dir",
        default=os.environ.get("HOST_PROFILE_DIR", "./browser_data"),
        help="Thu muc luu Playwright profile (mac dinh: ./browser_data)",
    )
    parser.add_argument(
        "--url",
        default=GEMINI_URL,
        help=f"URL muc tieu (mac dinh: {GEMINI_URL})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Chay headless (KHONG khuyen — khong the dang nhap thu cong)",
    )
    args = parser.parse_args()

    profile_dir = Path(args.profile_dir)
    asyncio.run(run_login(profile_dir, args.headless, args.url))


if __name__ == "__main__":
    main()
