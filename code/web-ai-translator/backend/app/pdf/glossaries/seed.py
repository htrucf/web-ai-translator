# -*- coding: utf-8 -*-
"""Seed glossary — intentionally empty.

The project no longer ships a hardcoded Math/CS/AI glossary. Glossary terms
come from two runtime sources only:

  1. Per-document extraction via Gemini (see ``app.pdf.glossary``)
  2. Cross-document promotion to ``database.global_terms`` (the "kho" pool),
     which the next job pre-seeds from at runtime.

This module is kept so legacy imports keep working, but every export is empty.
The v2 ``GlossaryPipeline`` (``glossaries/pipeline.py``) is not wired into the
production pipeline — production uses ``app.pdf.glossary`` (singular).
"""

# Keep the names so existing imports don't break, but ship no data.
DNT_SET: set[str] = set()
SEED_GLOSSARY: dict[str, str] = {}


def get_seed() -> dict[str, str]:
    """Return a copy of the (empty) seed glossary."""
    return dict(SEED_GLOSSARY)


def is_dnt(term: str) -> bool:
    """Always False — proper-noun detection now relies on runtime heuristics."""
    return term.lower().strip() in DNT_SET


def get_dnt_set() -> set[str]:
    """Return a copy of the (empty) DNT set."""
    return set(DNT_SET)
