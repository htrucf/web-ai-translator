"""Test: chay full pipeline dich thuat tu file LaTeX da tai."""

import asyncio
import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app.services.latex_processor import extract_source
from app.services.pipeline import TranslationPipeline


async def main():
    archive = "./workspace/downloads/2603.01285.tar.gz"
    if not os.path.exists(archive):
        print(f"Chua co file {archive}, hay chay test_arxiv.py truoc.")
        return

    # 1. Giai nen source
    print("=== GIAI NEN SOURCE ===")
    extract_dir = "./workspace/pipeline_test"
    tex_path = extract_source(archive, extract_dir)
    source_dir = os.path.dirname(tex_path)
    print(f"File .tex: {tex_path}")
    print(f"Source dir: {source_dir}")

    # 2. Chay pipeline
    print("\n=== BAT DAU PIPELINE DICH THUAT ===")
    pipeline = TranslationPipeline(work_dir="./workspace")

    try:
        pdf_path = await pipeline.run(
            tex_path=tex_path,
            job_id="test_2603.01285",
            source_dir=source_dir,
        )
        print(f"\n=== HOAN TAT ===")
        print(f"PDF da dich: {pdf_path}")
        print(f"Kich thuoc: {os.path.getsize(pdf_path)} bytes")
    except Exception as e:
        print(f"\n=== LOI ===")
        print(f"{e}")
        print("Kiem tra workspace/jobs/test_2603.01285/progress.json de xem tien trinh")
        print("Chay lai script nay se resume tu chunk cuoi cung da dich")


if __name__ == "__main__":
    asyncio.run(main())
