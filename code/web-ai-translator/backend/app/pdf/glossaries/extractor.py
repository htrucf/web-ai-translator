"""Layer 2 — Document-level term extraction via Gemini.

Sends the abstract + introduction of a paper to Gemini and asks it to
extract domain-specific terms not already covered by the seed glossary.

Keeps the extracted dict small (≤60 terms) and focused on THIS paper's
specific jargon (model names the authors coined, dataset names, novel
metrics, etc.).
"""

import re
import logging
from .seed import SEED_GLOSSARY, DNT_SET

logger = logging.getLogger(__name__)

# How many terms to request from Gemini
MAX_EXTRACT_TERMS = 60

# Min English term length to be worth extracting (single-word terms must be ≥5 chars)
MIN_TERM_LEN = 3
MIN_SINGLE_WORD_LEN = 5


def build_extraction_prompt(sample_text: str, existing_terms: set[str]) -> str:
    """Build a Gemini prompt for domain-specific term extraction.

    Args:
        sample_text: Abstract + introduction text (plain text, ~2000 chars).
        existing_terms: Terms already in seed + previous glossary (to skip).
    """
    # Show a few seed examples so Gemini knows the format
    examples = [
        "attention mechanism → cơ chế chú ý",
        "overfitting → quá khớp",
        "eigenvalue → giá trị riêng",
    ]

    skip_hint = ""
    if existing_terms:
        # Show a sample of what to skip (not all — too long)
        sample_skip = sorted(existing_terms)[:15]
        skip_hint = (
            "\nCÁC THUẬT NGỮ ĐÃ CÓ (KHÔNG trích lại):\n"
            + ", ".join(sample_skip)
            + "\n"
        )

    return (
        "Bạn là chuyên gia dịch thuật học thuật Toán/Khoa học máy tính/AI.\n"
        "Từ đoạn văn bản bên dưới, hãy trích xuất các thuật ngữ chuyên ngành "
        f"ĐẶC THÙ của bài báo này (tối đa {MAX_EXTRACT_TERMS} thuật ngữ).\n\n"
        "=== QUY TẮC ===\n"
        "1. Chỉ lấy thuật ngữ kỹ thuật, KHÔNG lấy từ thông dụng.\n"
        "2. Ưu tiên: tên phương pháp mới, tên metric, tên module đặc thù của bài.\n"
        "3. Các tên riêng (BERT, ImageNet, ResNet...) — GHI NGUYÊN, KHÔNG dịch.\n"
        "4. Mỗi dòng: English term → Bản dịch tiếng Việt\n"
        "5. Trả về trong block ```glossary ... ```\n"
        f"{skip_hint}\n"
        "=== VÍ DỤ OUTPUT ===\n"
        "```glossary\n"
        + "\n".join(examples) + "\n"
        "```\n\n"
        f"=== VĂN BẢN ===\n{sample_text[:2500]}"
    )


def parse_extraction_response(response: str) -> dict[str, str]:
    """Parse Gemini's glossary response into a dict[lowercase_en → vi].

    Filters out:
    - Terms shorter than MIN_TERM_LEN
    - Terms already in seed glossary
    - DNT terms (kept as-is, no translation needed)
    """
    if not response:
        return {}

    # Extract from ```glossary ... ``` or ```text ... ``` or fallback
    match = re.search(r'```(?:glossary|text)?\s*\n(.*?)```', response, re.DOTALL)
    raw = match.group(1) if match else response

    extracted: dict[str, str] = {}

    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Match: "term → translation" or "term -> translation"
        # Use alternation (not character class) to avoid matching '-' in "key-value"
        m = re.match(r'^(.+?)\s*(?:→|->)\s*(.+)$', line)
        if not m:
            continue

        en = m.group(1).strip().strip('"\'').lower()
        vi = m.group(2).strip().strip('"\'')

        if len(en) < MIN_TERM_LEN or not vi:
            continue
        # Single-word terms must be longer (avoid noise like "key", "map", "set")
        if " " not in en and len(en) < MIN_SINGLE_WORD_LEN:
            continue
        if en in SEED_GLOSSARY:
            continue  # already covered by seed
        if en in DNT_SET:
            continue  # do not translate

        extracted[en] = vi

    logger.info(f"[Extractor] Extracted {len(extracted)} new terms from document")
    return extracted


def extract_from_text(
    text: str,
    existing_glossary: dict[str, str],
    translator_fn,
) -> dict[str, str]:
    """Extract document-specific terms via Gemini.

    Args:
        text: Abstract + intro text (will be truncated to ~2500 chars).
        existing_glossary: Current glossary (seed + any prior terms).
        translator_fn: Async or sync callable that sends a prompt to Gemini
                       and returns the response string.
                       Signature: translator_fn(prompt: str) -> str

    Returns:
        dict of NEW terms (not in existing_glossary) found in this document.
    """
    existing_terms = set(existing_glossary.keys())
    prompt = build_extraction_prompt(text, existing_terms)

    try:
        response = translator_fn(prompt)
        return parse_extraction_response(response)
    except Exception as e:
        logger.warning(f"[Extractor] Gemini extraction failed: {e}")
        return {}
