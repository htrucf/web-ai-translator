"""Domain-specific glossary packs (loader).

Packs are JSON files in `backend/glossary_packs/` — one file per domain. Each
pack is a self-contained EN→VI glossary the user can import into a job's
glossary during the HITL review gate.

Schema (per file):
    {
      "id": "math",
      "name": "Toán học",
      "description": "...",
      "version": "1.0",
      "language": "en-vi",
      "terms": {"theorem": "định lý", ...}
    }

Adding a new pack: drop a `<id>.json` file into the directory; it shows up
on the next API call (no restart needed). The directory is rescanned on
every list_packs() call — cheap, the files are small.
"""

import json
import os
from typing import Iterable

# `backend/glossary_packs/` — sibling of the `app/` package, outside the
# Python package so users can drop new files in without touching code.
_PACKS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "glossary_packs")
)


def packs_dir() -> str:
    """Absolute path to the pack directory. Useful for tests / overrides."""
    return _PACKS_DIR


def _load_pack_file(path: str) -> dict | None:
    """Read one pack JSON. Returns None on parse error so a single broken
    file doesn't break the whole listing."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("terms"), dict):
        return None
    # Derive id from filename if missing
    if not data.get("id"):
        data["id"] = os.path.splitext(os.path.basename(path))[0]
    return data


def _iter_pack_files() -> Iterable[str]:
    if not os.path.isdir(_PACKS_DIR):
        return
    for name in sorted(os.listdir(_PACKS_DIR)):
        if name.endswith(".json"):
            yield os.path.join(_PACKS_DIR, name)


def list_packs() -> list[dict]:
    """Return metadata for every pack (no terms — keep the index light)."""
    out = []
    for path in _iter_pack_files():
        data = _load_pack_file(path)
        if not data:
            continue
        out.append({
            "id": data["id"],
            "name": data.get("name") or data["id"],
            "description": data.get("description", ""),
            "version": data.get("version", "1.0"),
            "language": data.get("language", "en-vi"),
            "term_count": len(data["terms"]),
        })
    return out


def get_pack(pack_id: str) -> dict | None:
    """Return the full pack content (incl. terms) or None if not found."""
    safe_id = os.path.basename(pack_id).strip()
    if not safe_id or safe_id.startswith(".") or "/" in pack_id or "\\" in pack_id:
        return None
    path = os.path.join(_PACKS_DIR, f"{safe_id}.json")
    if not os.path.isfile(path):
        return None
    return _load_pack_file(path)


def merge_packs_into_glossary(
    existing_terms: dict[str, str],
    pack_ids: list[str],
) -> tuple[dict[str, str], int, int, list[str]]:
    """Merge selected packs into an existing glossary.

    First-wins: existing entries (Gemini-extracted, user-edited, locked) are
    never overwritten. Only fills gaps. This matches `glossary.merge_glossary`
    semantics so the pipeline behavior stays consistent.

    Returns (merged_terms, added_count, skipped_count, missing_pack_ids).
    """
    merged = dict(existing_terms)
    seen_lower = {k.lower() for k in merged}
    added = 0
    skipped = 0
    missing: list[str] = []

    for pid in pack_ids:
        pack = get_pack(pid)
        if not pack:
            missing.append(pid)
            continue
        for en, vi in pack["terms"].items():
            key = en.lower()
            if key in seen_lower:
                skipped += 1
                continue
            merged[en] = vi  # preserve original casing of pack key
            seen_lower.add(key)
            added += 1

    return merged, added, skipped, missing
