"""Glossary module for consistent terminology translation.

Separate module — does not affect existing pipeline logic.
Pipeline calls glossary functions if enabled; skips if disabled or on error.
"""

import re

# Maximum terms to inject into a single chunk prompt
MAX_TERMS_PER_PROMPT = 40


def build_extraction_prompt(sample_text: str) -> str:
    """Build a prompt to extract EN→VI terminology (+ lĩnh vực) from sample text."""
    return (
        "Từ đoạn văn bản học thuật sau, trích xuất các thuật ngữ chuyên ngành "
        "quan trọng và dịch sang tiếng Việt.\n\n"
        "=== QUY TẮC ===\n"
        "1. Chỉ trích xuất thuật ngữ chuyên ngành (technical terms), "
        "KHÔNG trích từ thông dụng.\n"
        "2. Mỗi dòng một bộ ba, format: English term → Thuật ngữ tiếng Việt → lĩnh vực\n"
        "3. 'lĩnh vực' là chuyên ngành ngắn gọn của thuật ngữ "
        "(vd: Học máy, Thị giác máy tính, Đại số tuyến tính, Sinh học phân tử). "
        "Nếu không chắc, để trống phần sau dấu → thứ hai.\n"
        "4. Giữ nguyên viết tắt trong ngoặc nếu có, ví dụ: "
        "Convolutional Neural Network (CNN) → Mạng nơ-ron tích chập (CNN) → Học sâu\n"
        "5. Trả về trong block ```text ... ```.\n"
        "6. Tối đa 60 thuật ngữ quan trọng nhất.\n\n"
        "=== VÍ DỤ OUTPUT ===\n"
        "```text\n"
        "machine learning → học máy → Trí tuệ nhân tạo\n"
        "gradient descent → hạ gradient → Tối ưu hóa\n"
        "overfitting → quá khớp → Học máy\n"
        "```\n\n"
        f"=== VĂN BẢN ===\n```text\n{sample_text}\n```"
    )


def _iter_term_lines(response: str):
    """Yield (en, vi, field|None) for each parsable line of an extraction response.

    Tolerates 2-column (en → vi) and 3-column (en → vi → lĩnh vực) formats, with
    either → or -> as the separator. Shared by `parse_extraction_response` and
    `parse_extraction_fields` so both see exactly the same lines.
    """
    if not response:
        return

    # Extract from ```text ... ``` block
    match = re.search(r'```(?:text)?\s*\n(.*?)```', response, re.DOTALL)
    text = match.group(1).strip() if match else response.strip()

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Normalize ASCII arrow then split into ≤3 segments on the → separator.
        parts = [p.strip().strip('"\'') for p in line.replace("->", "→").split("→")]
        if len(parts) < 2:
            continue
        en, vi = parts[0], parts[1]
        field = parts[2] if len(parts) >= 3 else None
        if en and vi and len(en) > 1:
            yield en, vi, (field or None)


def parse_extraction_response(response: str) -> dict[str, str]:
    """Parse Gemini's glossary extraction response into a dict.

    Returns:
        dict mapping English term (lowercase) → Vietnamese translation.
        The optional 'lĩnh vực' column is ignored here (see
        `parse_extraction_fields`).
    """
    return {en.lower(): vi for en, vi, _field in _iter_term_lines(response)}


def parse_extraction_fields(response: str) -> dict[str, str]:
    """Parse the optional 'lĩnh vực' column → {en_term_lowercase: field}.

    Only includes terms where Gemini actually supplied a non-empty field.
    """
    fields: dict[str, str] = {}
    for en, _vi, field in _iter_term_lines(response):
        if field:
            fields[en.lower()] = field[:64]
    return fields


def filter_glossary_for_chunk(
    glossary: dict[str, str], chunk_text: str,
    locked: set[str] | list[str] | None = None,
) -> dict[str, str]:
    """Filter glossary to only terms that appear in the chunk text.

    Returns at most MAX_TERMS_PER_PROMPT terms. Locked terms are kept first
    (they MUST be in the prompt); remaining slots fill with longer terms.
    """
    if not glossary or not chunk_text:
        return {}

    locked_set = {k.lower() for k in (locked or [])}
    chunk_lower = chunk_text.lower()
    matched = {en: vi for en, vi in glossary.items() if en in chunk_lower}

    if len(matched) <= MAX_TERMS_PER_PROMPT:
        return matched

    # Locked terms always included; remaining slots: longer (more specific) first
    locked_matched = {en: vi for en, vi in matched.items() if en in locked_set}
    others = {en: vi for en, vi in matched.items() if en not in locked_set}
    remaining = MAX_TERMS_PER_PROMPT - len(locked_matched)
    if remaining <= 0:
        return locked_matched
    extra = sorted(others.items(), key=lambda x: len(x[0]), reverse=True)[:remaining]
    return {**locked_matched, **dict(extra)}


def format_glossary_for_prompt(
    filtered_glossary: dict[str, str],
    locked: set[str] | list[str] | None = None,
) -> str:
    """Format filtered glossary as text to prepend to translation prompt.

    Locked terms are listed in a separate stronger-directive section so the
    translator treats them as inviolable.
    """
    if not filtered_glossary:
        return ""

    locked_set = {k.lower() for k in (locked or [])}
    locked_pairs = [(en, vi) for en, vi in filtered_glossary.items() if en in locked_set]
    soft_pairs = [(en, vi) for en, vi in filtered_glossary.items() if en not in locked_set]

    sections = []
    if locked_pairs:
        lines = [f'  "{en}" → "{vi}"' for en, vi in sorted(locked_pairs)]
        sections.append(
            "=== THUẬT NGỮ KHÓA (BẮT BUỘC giữ nguyên bản dịch này, không được thay thế) ===\n"
            + "\n".join(lines)
        )
    if soft_pairs:
        lines = [f'  "{en}" → "{vi}"' for en, vi in sorted(soft_pairs)]
        sections.append(
            "=== BẢNG THUẬT NGỮ (ưu tiên dùng bản dịch này) ===\n"
            + "\n".join(lines)
        )

    return "\n\n".join(sections) + "\n\n" if sections else ""


def extract_new_terms_prompt(original: str, translated: str) -> str:
    """Build a prompt to extract new term pairs from a translated chunk.

    This is a lightweight prompt — only used if we want active term discovery.
    """
    return (
        "So sánh bản gốc và bản dịch sau, liệt kê các thuật ngữ chuyên ngành "
        "mới (chưa có trong glossary) được dịch trong đoạn này.\n\n"
        "Format mỗi dòng: English term → Thuật ngữ tiếng Việt\n"
        "Chỉ liệt kê thuật ngữ chuyên ngành, KHÔNG liệt kê từ thông dụng.\n"
        "Trả về trong block ```text ... ```. Nếu không có thuật ngữ mới, "
        "trả về block rỗng.\n\n"
        f"=== BẢN GỐC ===\n```text\n{original[:2000]}\n```\n\n"
        f"=== BẢN DỊCH ===\n```text\n{translated[:2000]}\n```"
    )


def merge_glossary(
    existing: dict[str, str], new_terms: dict[str, str]
) -> dict[str, str]:
    """Merge new terms into existing glossary.

    Existing terms are NOT overwritten — first translation wins for consistency.
    Locked terms are inherently safe under this rule (they're already in `existing`).
    """
    merged = dict(existing)
    for en, vi in new_terms.items():
        key = en.lower()
        if key not in merged:
            merged[key] = vi
    return merged


def normalize_locked(locked) -> list[str]:
    """Normalize locked field to a sorted unique list of lowercase keys."""
    if not locked:
        return []
    if isinstance(locked, dict):
        # Tolerate dict form {key: bool}
        keys = [k for k, v in locked.items() if v]
    else:
        keys = list(locked)
    return sorted({str(k).lower().strip() for k in keys if k})
