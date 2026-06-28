"""Glossary package for Math / CS / AI academic paper translation.

Public API:
    from app.pdf.glossaries import GlossaryPipeline
    from app.pdf.glossaries.seed import SEED_GLOSSARY, DNT_SET
"""

from .pipeline import GlossaryPipeline
from .seed import SEED_GLOSSARY, DNT_SET, get_seed, is_dnt

__all__ = [
    "GlossaryPipeline",
    "SEED_GLOSSARY",
    "DNT_SET",
    "get_seed",
    "is_dnt",
]
