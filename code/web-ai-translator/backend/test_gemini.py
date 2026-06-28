"""Test: gui prompt dich LaTeX len Gemini free va lay ket qua."""

import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app.services.translator import WebAITranslator


async def main():
    translator = WebAITranslator(user_data_dir="./browser_data")

    print("[1] Dang mo browser...")
    context, page = await translator.launch_browser()

    print("[2] Doi Gemini load...")
    await asyncio.sleep(3)

    # Chup screenshot truoc khi gui
    await page.screenshot(path="gemini_before.png")
    print("[3] Da chup gemini_before.png")

    # Gui prompt dich thu
    test_latex = r"""\section{Introduction}
Deep learning has achieved remarkable success in various fields,
including computer vision and natural language processing.
In this paper, we propose a novel method for image classification
using convolutional neural networks (CNNs)."""

    prompt = (
        "Dich noi dung LaTeX sau sang tieng Viet. "
        "GIU NGUYEN tat ca LaTeX commands. "
        "CHI dich phan text tieng Anh. "
        "Tra ve CHINH XAC noi dung LaTeX da dich, khong them giai thich.\n\n"
        f"```latex\n{test_latex}\n```"
    )

    print("[4] Dang gui prompt dich...")
    result = await translator._send_prompt_and_get_response(page, prompt)

    # Chup screenshot sau khi nhan response
    await page.screenshot(path="gemini_after.png")
    print("[5] Da chup gemini_after.png")

    print("\n=== KET QUA ===")
    print(result)
    print("================")

    print("\n[6] Dong browser sau 5 giay...")
    await asyncio.sleep(5)
    await context.close()
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
