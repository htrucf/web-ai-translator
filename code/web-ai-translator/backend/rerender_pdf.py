"""Re-render translated.pdf from cached chunks without re-translating.

Useful after fixing rebuild_pdf_inplace bugs: avoids re-running the Gemini
pipeline on a long document. Requires a finished job with:
  - original.pdf                       (source)
  - progress.json with chunk_block_map (block-to-chunk mapping)
  - chunks/chunk_NNN_translated.txt    (cached AI output)

Usage:
  python rerender_pdf.py <job_dir>
  python rerender_pdf.py workspace/users/trucnb/jobs/pdf_BABOK_Guide_v3_Member
"""
import json
import os
import sys

from app.pdf.processor import (
    extract_text_blocks,
    parse_translated_chunk,
    rebuild_pdf_inplace,
)


def rerender(job_dir: str) -> str:
    if not os.path.isdir(job_dir):
        raise SystemExit(f"Job dir not found: {job_dir}")
    original = os.path.join(job_dir, "original.pdf")
    progress_path = os.path.join(job_dir, "progress.json")
    chunks_dir = os.path.join(job_dir, "chunks")
    if not os.path.isfile(original):
        raise SystemExit(f"Missing original.pdf at {original}")
    if not os.path.isfile(progress_path):
        raise SystemExit(f"Missing progress.json at {progress_path}")
    if not os.path.isdir(chunks_dir):
        raise SystemExit(f"Missing chunks/ dir at {chunks_dir}")

    with open(progress_path, "r", encoding="utf-8") as f:
        progress = json.load(f)

    chunk_map = progress.get("chunk_block_map", {}).get("chunks")
    if not chunk_map:
        raise SystemExit("progress.json has no chunk_block_map.chunks — cannot remap")

    print(f"[rerender] Re-extracting blocks from {original}...")
    all_blocks = extract_text_blocks(original)
    by_key = {(b.page_num, b.block_idx): b for b in all_blocks}
    print(f"[rerender] Extracted {len(all_blocks)} blocks")

    matched = 0
    missing_chunks = 0
    for chunk_idx, entries in enumerate(chunk_map):
        chunk = []
        for e in entries:
            key = (int(e["page"]), int(e["block_idx"]))
            b = by_key.get(key)
            if b is not None:
                chunk.append(b)
        if not chunk:
            continue
        txt_path = os.path.join(chunks_dir, f"chunk_{chunk_idx:03d}_translated.txt")
        if not os.path.isfile(txt_path):
            missing_chunks += 1
            for b in chunk:
                if not b.translated_text:
                    b.translated_text = b.text
            continue
        with open(txt_path, "r", encoding="utf-8") as f:
            translated_text = f.read()
        parse_translated_chunk(translated_text, chunk)
        matched += len(chunk)

    print(f"[rerender] Matched {matched} blocks across {len(chunk_map)} chunks"
          f" ({missing_chunks} chunk files missing — fell back to original text)")

    # Backfill any block we didn't see in chunk_block_map (math, headers, etc.)
    for b in all_blocks:
        if b.is_translatable and not b.translated_text:
            b.translated_text = b.text

    output_dir = os.path.join(job_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "translated.pdf")

    print(f"[rerender] Rebuilding PDF (in-place mode) -> {output_path}")
    meta = progress.get("translation_meta")
    rebuild_pdf_inplace(original, all_blocks, output_path, translation_meta=meta)
    size = os.path.getsize(output_path)
    print(f"[rerender] Done. Size: {size / 1024 / 1024:.2f} MB")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    rerender(sys.argv[1])
