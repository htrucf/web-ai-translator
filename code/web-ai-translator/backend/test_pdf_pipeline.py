"""Standalone test script for PDF-only translation pipeline.

Usage:
    # Test 1: Extract text blocks from a PDF (no translation)
    python test_pdf_pipeline.py extract path/to/paper.pdf

    # Test 2: Extract + rebuild (no translation, just verify round-trip)
    python test_pdf_pipeline.py roundtrip path/to/paper.pdf

    # Test 3: Full pipeline with Gemini translation
    python test_pdf_pipeline.py translate path/to/paper.pdf [job_id]

    # Test 4: Run standalone API server on port 8001
    python test_pdf_pipeline.py serve
"""

import asyncio
import os
import sys

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add parent dir to path so imports work
sys.path.insert(0, os.path.dirname(__file__))


def test_extract(pdf_path: str):
    """Test text extraction — prints blocks with classification."""
    from app.pdf.processor import extract_text_blocks, split_blocks_into_chunks

    print(f"\n{'='*60}")
    print(f"EXTRACTING: {pdf_path}")
    print(f"{'='*60}\n")

    blocks = extract_text_blocks(pdf_path)

    translatable = [b for b in blocks if b.is_translatable]
    math_blocks = [b for b in blocks if b.is_math]
    skipped = [b for b in blocks if not b.is_translatable and not b.is_math]

    print(f"Total blocks: {len(blocks)}")
    print(f"  Translatable: {len(translatable)}")
    print(f"  Math: {len(math_blocks)}")
    print(f"  Skipped (headers/footers/short): {len(skipped)}")
    print()

    # Print first 10 translatable blocks
    print("--- First 10 translatable blocks ---")
    for i, b in enumerate(translatable[:10]):
        text_preview = b.text[:100].replace("\n", " ")
        print(f"  [{i}] Page {b.page_num}, "
              f"bbox=({b.bbox[0]:.0f},{b.bbox[1]:.0f},{b.bbox[2]:.0f},{b.bbox[3]:.0f}), "
              f"font={b.font_name} size={b.font_size:.1f}")
        print(f"      \"{text_preview}...\"")
        print()

    # Chunking
    chunks = split_blocks_into_chunks(blocks)
    print(f"Chunks for translation: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        total_chars = sum(len(b.text) for b in chunk)
        print(f"  Chunk {i}: {len(chunk)} blocks, {total_chars} chars")

    print(f"\n{'='*60}")
    print("EXTRACTION COMPLETE")
    print(f"{'='*60}")


def test_roundtrip(pdf_path: str):
    """Test extract + rebuild without translation (identity transform)."""
    from app.pdf.processor import (
        extract_text_blocks, rebuild_pdf, get_pdf_info,
    )

    print(f"\n{'='*60}")
    print(f"ROUND-TRIP TEST: {pdf_path}")
    print(f"{'='*60}\n")

    # Extract
    blocks = extract_text_blocks(pdf_path)
    translatable = [b for b in blocks if b.is_translatable]
    print(f"Extracted {len(translatable)} translatable blocks")

    # Set "translated" text = original text (identity)
    for b in translatable:
        b.translated_text = b.text

    # Rebuild
    output_path = pdf_path.replace(".pdf", "_roundtrip.pdf")
    rebuild_pdf(pdf_path, blocks, output_path)

    # Compare
    orig_info = get_pdf_info(pdf_path)
    out_info = get_pdf_info(output_path)

    print(f"\nOriginal:  {orig_info['page_count']} pages, "
          f"{os.path.getsize(pdf_path)} bytes")
    print(f"Roundtrip: {out_info['page_count']} pages, "
          f"{os.path.getsize(output_path)} bytes")
    print(f"\nOutput saved to: {output_path}")
    print(f"{'='*60}")


def test_translate(pdf_path: str, job_id: str = "test_pdf"):
    """Test full pipeline with Gemini translation."""

    async def _run():
        from app.pdf.pipeline import PdfTranslationPipeline

        print(f"\n{'='*60}")
        print(f"FULL TRANSLATION: {pdf_path}")
        print(f"Job ID: {job_id}")
        print(f"{'='*60}\n")

        pipeline = PdfTranslationPipeline(work_dir="./workspace")
        result = await pipeline.run(pdf_path, job_id)
        print(f"\nResult: {result}")

    asyncio.run(_run())


def serve():
    """Run standalone API server for testing."""
    import uvicorn
    print("\nStarting PDF Translation API on http://localhost:8001")
    print("Endpoints:")
    print("  POST /api/pdf-translate/upload  — Upload PDF and start translation")
    print("  GET  /api/pdf-translate/{id}/status — Check progress")
    print("  GET  /api/pdf-translate/{id}/original — Download original PDF")
    print("  GET  /api/pdf-translate/{id}/translated — Download translated PDF")
    print("  POST /api/pdf-translate/{id}/cancel — Cancel translation")
    print("  GET  /api/pdf-translate/jobs — List all PDF jobs")
    print()
    uvicorn.run("app.pdf.routes:app", host="0.0.0.0", port=8001, reload=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "extract":
        if len(sys.argv) < 3:
            print("Usage: python test_pdf_pipeline.py extract <pdf_path>")
            sys.exit(1)
        test_extract(sys.argv[2])

    elif command == "roundtrip":
        if len(sys.argv) < 3:
            print("Usage: python test_pdf_pipeline.py roundtrip <pdf_path>")
            sys.exit(1)
        test_roundtrip(sys.argv[2])

    elif command == "translate":
        if len(sys.argv) < 3:
            print("Usage: python test_pdf_pipeline.py translate <pdf_path> [job_id]")
            sys.exit(1)
        pdf = sys.argv[2]
        jid = sys.argv[3] if len(sys.argv) > 3 else "test_pdf"
        test_translate(pdf, jid)

    elif command == "serve":
        serve()

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)
