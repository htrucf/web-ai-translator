"""LibreOffice headless converter — turns a translated .docx into PDF
so the frontend can preview it inline (the source file remains the canonical
download).

LibreOffice is autodetected on PATH first, then at common install locations.
If not found, callers should swallow the RuntimeError and mark
`has_preview=False` in progress — the pipeline must not fail just because
preview rendering is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess


def find_soffice() -> str | None:
    """Locate the LibreOffice CLI binary, or None if not installed."""
    for candidate in ("soffice", "soffice.exe"):
        path = shutil.which(candidate)
        if path:
            return path
    guesses = (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    )
    for g in guesses:
        if os.path.isfile(g):
            return g
    return None


def is_available() -> bool:
    return find_soffice() is not None


def build_preview_pdf(office_path: str, preview_pdf_path: str,
                      timeout: int = 180) -> str:
    """Convert a .docx file to PDF via `soffice --convert-to pdf`.

    LibreOffice always writes `<basename>.pdf` next to --outdir; we rename it
    to the caller's target if it differs (e.g. `preview.pdf`).
    """
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice không được cài. Cài tại https://www.libreoffice.org/download/ "
            "rồi dịch lại để xem preview."
        )

    out_dir = os.path.dirname(preview_pdf_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        "--norestore",
        "--convert-to", "pdf",
        "--outdir", out_dir,
        office_path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"LibreOffice timeout sau {timeout}s") from e

    if proc.returncode != 0:
        raise RuntimeError(
            f"LibreOffice convert failed (exit={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )

    base = os.path.splitext(os.path.basename(office_path))[0]
    actual_pdf = os.path.join(out_dir, base + ".pdf")
    if actual_pdf != preview_pdf_path:
        if os.path.exists(preview_pdf_path):
            os.remove(preview_pdf_path)
        if os.path.exists(actual_pdf):
            os.rename(actual_pdf, preview_pdf_path)

    if not os.path.exists(preview_pdf_path):
        raise RuntimeError(f"PDF không được sinh tại {preview_pdf_path}")

    return preview_pdf_path
