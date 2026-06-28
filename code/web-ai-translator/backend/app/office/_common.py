"""Chunking + prompt format + parsing + inline-format tags — cho DOCX pipeline."""

from __future__ import annotations

import re
from typing import Protocol


class _Block(Protocol):
    """Duck-typed Block — must expose `.text` (str) and `.translated_text` (str)."""
    text: str
    translated_text: str


# ── Inline formatting tags ───────────────────────────────────────────────────
# docx_processor bọc các đoạn chữ in đậm/nghiêng bằng thẻ `[[#k]]...[[/#k]]` để
# AI dịch CẢ đoạn (giữ ngữ cảnh + trật tự từ tiếng Việt) mà vẫn biết khúc nào
# cần định dạng. Sau đó injection cắt theo thẻ, tái dựng run với đúng <w:rPr>.
# Dùng `#` để KHÔNG đụng đánh số đoạn `[N]` (regex `\[(\d+)\]` không khớp `[[#1]]`).
_INLINE_ANY_RE = re.compile(r"\[\[/?#\d+\]\]")
_INLINE_OPEN_RE = re.compile(r"\[\[#(\d+)\]\]")


def inline_open(tag_id: int) -> str:
    return f"[[#{tag_id}]]"


def inline_close(tag_id: int) -> str:
    return f"[[/#{tag_id}]]"


def strip_inline_tags(s: str) -> str:
    """Bỏ MỌI thẻ `[[#k]]`/`[[/#k]]` (kể cả mảnh lỗi) — không để lọt ra output/judge."""
    return _INLINE_ANY_RE.sub("", s or "")


def split_inline_segments(s: str) -> list[tuple[str, int | None]]:
    """Cắt chuỗi có thẻ thành list `(text, tag_id|None)`, KHOAN DUNG với thẻ hỏng.

    - Thiếu thẻ đóng / thẻ lồng / số lệch → phần đó coi như plain (tag_id=None).
    - Tự dọn mọi mảnh thẻ còn sót trong từng segment → output không bao giờ chứa `[[#`.
    """
    s = s or ""
    raw: list[tuple[str, int | None]] = []
    i = 0
    while i < len(s):
        m = _INLINE_OPEN_RE.search(s, i)
        if not m:
            raw.append((s[i:], None))
            break
        if m.start() > i:
            raw.append((s[i:m.start()], None))
        tag_id = int(m.group(1))
        close = inline_close(tag_id)
        close_at = s.find(close, m.end())
        if close_at == -1:                       # thẻ mở không có thẻ đóng
            raw.append((s[m.end():], None))
            break
        raw.append((s[m.end():close_at], tag_id))
        i = close_at + len(close)

    out: list[tuple[str, int | None]] = []
    for text, tid in raw:
        clean = strip_inline_tags(text)
        if clean:
            out.append((clean, tid))
    return out


def split_into_chunks(blocks: list, max_chars: int = 1500) -> list[list]:
    """Group blocks into chunks of ~max_chars total text.

    Blocks are kept in their original order — never reordered, never merged
    across chunks. Paragraphs longer than max_chars become their own chunk.
    """
    chunks: list[list] = []
    current: list = []
    current_len = 0
    for b in blocks:
        text_len = len(b.text or "")
        if current and current_len + text_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(b)
        current_len += text_len
    if current:
        chunks.append(current)
    return chunks


def chunk_to_numbered_text(chunk: list) -> str:
    """Render a chunk as `[1] ... \\n\\n[2] ... \\n\\n[3] ...` for the prompt.

    Internal newlines/multi-spaces are collapsed so the `[N]` separator stays
    unambiguous — the loss of line breaks is acceptable for paragraph-level
    DOCX content (the source format keeps paragraph boundaries itself).
    """
    parts = []
    for i, b in enumerate(chunk):
        normalized = " ".join((b.text or "").split())
        parts.append(f"[{i + 1}] {normalized}")
    return "\n\n".join(parts)


_NUM_RE = re.compile(r"\[(\d+)\]\s*(.*?)(?=\n\[\d+\]|\Z)", re.DOTALL)


def parse_numbered_response(response: str, chunk: list) -> int:
    """Parse `[N] ...` segments and fill `chunk[N-1].translated_text`.

    Returns the count of blocks that received a translation. Existing
    translations are NOT overwritten (resume-safe).
    """
    if not response:
        return 0
    filled = 0
    for num_str, text in _NUM_RE.findall(response):
        try:
            idx = int(num_str) - 1
        except ValueError:
            continue
        if 0 <= idx < len(chunk):
            translated = text.strip()
            if translated and not chunk[idx].translated_text:
                chunk[idx].translated_text = translated
                filled += 1
    return filled


def build_translation_prompt(numbered_text: str) -> str:
    """Build the Gemini prompt for one chunk."""
    return (
        "Bạn là người dịch chuyên nghiệp. Hãy dịch các đoạn văn sau từ tiếng "
        "Anh sang tiếng Việt một cách tự nhiên, chính xác, giữ nguyên thuật "
        "ngữ chuyên ngành.\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "1. GIỮ NGUYÊN đánh số [1], [2], [3]... ở đầu mỗi đoạn.\n"
        "2. Mỗi đoạn output cũng bắt đầu bằng [N] tương ứng.\n"
        "3. Các đoạn cách nhau bằng dòng trống.\n"
        "4. KHÔNG thêm giải thích, KHÔNG dịch số [N].\n"
        "5. KHÔNG dùng markdown (không **, *, _, #, ```).\n"
        "6. Giữ nguyên tên riêng, công thức, ký hiệu toán học, code.\n"
        "7. Một số đoạn có thẻ định dạng [[#1]]...[[/#1]] (đánh dấu in đậm/nghiêng). "
        "GIỮ NGUYÊN y hệt các thẻ này (cả con số), đặt chúng bao quanh đúng phần "
        "chữ tương ứng trong bản dịch; KHÔNG xoá, KHÔNG dịch, KHÔNG đổi số.\n"
        "   Ví dụ: [1] The [[#1]]quick[[/#1]] fox → [1] Con cáo [[#1]]nhanh[[/#1]]\n\n"
        f"ĐOẠN CẦN DỊCH:\n{numbered_text}"
    )


def clean_response(response: str) -> str:
    """Strip code fences / leading commentary from a Gemini reply."""
    if not response:
        return ""
    cleaned = response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].lstrip().startswith("```"):
            cleaned = "\n".join(lines[1:-1]).strip()
    return cleaned
