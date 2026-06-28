"""Test: tach chunk LaTeX va compile PDF tu source da tai."""

import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from app.services.latex_processor import extract_source, split_into_chunks, compile_to_pdf


def main():
    archive = "./workspace/downloads/2603.01285.tar.gz"
    if not os.path.exists(archive):
        print(f"Chua co file {archive}, hay chay test_arxiv.py truoc.")
        return

    # 1. Giai nen
    print("[1] Giai nen source...")
    tex_path = extract_source(archive, "./workspace/test_latex")
    print(f"    File .tex chinh: {tex_path}")

    # 2. Doc noi dung
    with open(tex_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    print(f"    Tong do dai: {len(content)} ky tu")

    # 3. Tach chunk
    print("\n[2] Tach thanh cac chunk...")
    chunks = split_into_chunks(content)
    print(f"    So chunk: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        preview = chunk[:80].replace('\n', ' ').strip()
        print(f"    [{i}] {len(chunk)} ky tu - '{preview}...'")

    # 4. Compile PDF
    print("\n[3] Compile PDF (can pdflatex)...")
    try:
        pdf_path = compile_to_pdf(tex_path, "./workspace/test_latex/output")
        pdf_size = os.path.getsize(pdf_path)
        print(f"    Thanh cong! {pdf_path} ({pdf_size} bytes)")
    except RuntimeError as e:
        print(f"    {e}")
        # Kiem tra pdflatex co cai chua
        import shutil
        if not shutil.which("pdflatex"):
            print("    pdflatex CHUA CAI. Can cai MiKTeX hoac TeX Live.")
        else:
            # Doc log loi
            log_path = os.path.splitext(os.path.basename(tex_path))[0] + ".log"
            log_full = os.path.join("./workspace/test_latex/output", log_path)
            if os.path.exists(log_full):
                with open(log_full, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                # Tim dong loi
                errors = [l.strip() for l in lines if l.startswith("!")]
                if errors:
                    print("    Loi compile:")
                    for e in errors[:5]:
                        print(f"      {e}")

    print("\nDone!")


if __name__ == "__main__":
    main()
