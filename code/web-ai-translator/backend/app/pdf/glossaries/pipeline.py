"""3-layer glossary pipeline for Math / CS / AI academic papers.

Layers:
  Layer 1 — Seed glossary    : ~400 built-in standard terms, always present
  Layer 2 — Document extract : paper-specific jargon from abstract/intro
  Layer 3 — Chunk discovery  : incremental new terms found during translation

The GlossaryPipeline object is created once per translation job and passed
through the pipeline. It accumulates terms across all three layers and
injects the relevant subset into each chunk's translation prompt.

Usage (in pdf/pipeline.py):

    from app.pdf.glossaries.pipeline import GlossaryPipeline

    gp = GlossaryPipeline.from_progress(progress)

    # Layer 2 — run once before translating
    await gp.extract_document_terms(abstract_text, translator)

    # Per chunk — get prompt prefix + update after translation
    prompt_prefix = gp.prompt_for_chunk(chunk_text)
    ...translate chunk...
    gp.update_from_translation(chunk_text, translated_text, translator)

    # Persist
    gp.save_to_progress(progress)
"""

import re
import logging
from dataclasses import dataclass, field

from .seed import get_seed, get_dnt_set, is_dnt
from .extractor import extract_from_text, parse_extraction_response

logger = logging.getLogger(__name__)

# Max terms injected into a single chunk prompt
MAX_TERMS_PER_PROMPT = 40

# Min chars for a chunk to warrant discovery (avoid tiny chunks)
MIN_CHUNK_LEN_FOR_DISCOVERY = 300

# Discovery frequency: run term discovery every N chunks
DISCOVERY_INTERVAL = 5


@dataclass
class GlossaryStats:
    seed_count: int = 0
    document_count: int = 0
    discovered_count: int = 0
    injections: int = 0          # how many times we injected into prompts
    total_terms_injected: int = 0

    def to_dict(self) -> dict:
        return {
            "seed_count": self.seed_count,
            "document_count": self.document_count,
            "discovered_count": self.discovered_count,
            "total": self.seed_count + self.document_count + self.discovered_count,
            "injections": self.injections,
            "avg_terms_per_injection": (
                round(self.total_terms_injected / self.injections, 1)
                if self.injections > 0 else 0
            ),
        }


class GlossaryPipeline:
    """Manages the 3-layer glossary for a single translation job."""

    def __init__(
        self,
        seed: dict[str, str] | None = None,
        document_terms: dict[str, str] | None = None,
        discovered_terms: dict[str, str] | None = None,
        enabled: bool = True,
        chunk_counter: int = 0,
    ):
        # Layer 1: seed (immutable reference)
        self._seed: dict[str, str] = seed if seed is not None else get_seed()
        # Layer 2: document-specific terms
        self._document: dict[str, str] = document_terms or {}
        # Layer 3: incrementally discovered terms
        self._discovered: dict[str, str] = discovered_terms or {}

        self.enabled = enabled
        self._chunk_counter = chunk_counter
        self.stats = GlossaryStats(
            seed_count=len(self._seed),
            document_count=len(self._document),
            discovered_count=len(self._discovered),
        )

    # ── Constructors ─────────────────────────────────────────────────

    @classmethod
    def from_progress(cls, progress: dict) -> "GlossaryPipeline":
        """Restore pipeline from progress.json (resume support)."""
        g = progress.get("glossary_v2", {})
        return cls(
            document_terms=g.get("document_terms", {}),
            discovered_terms=g.get("discovered_terms", {}),
            enabled=g.get("enabled", True),
            chunk_counter=g.get("chunk_counter", 0),
        )

    def save_to_progress(self, progress: dict) -> None:
        """Persist pipeline state to progress dict (call before json.dump)."""
        progress["glossary_v2"] = {
            "document_terms": self._document,
            "discovered_terms": self._discovered,
            "enabled": self.enabled,
            "chunk_counter": self._chunk_counter,
            "stats": self.stats.to_dict(),
        }

    # ── Layer 2: document extraction ─────────────────────────────────

    def extract_document_terms(
        self,
        abstract_intro_text: str,
        translator_fn,
    ) -> dict[str, str]:
        """Run Layer 2: extract paper-specific terms via Gemini.

        Call this ONCE before the translation loop, passing the
        abstract + intro text (~first 2 pages).

        Args:
            abstract_intro_text: Plain text from abstract/introduction.
            translator_fn: Callable(prompt) -> str  (calls Gemini).

        Returns:
            New terms extracted (also stored internally).
        """
        if not self.enabled:
            return {}

        combined = {**self._seed, **self._document}
        new_terms = extract_from_text(abstract_intro_text, combined, translator_fn)

        self._document.update(new_terms)
        self.stats.document_count = len(self._document)
        logger.info(
            f"[GlossaryPipeline] Layer 2 extracted {len(new_terms)} new terms "
            f"(document total: {len(self._document)})"
        )
        return new_terms

    # ── Layer 3: chunk discovery ──────────────────────────────────────

    def update_from_translation(
        self,
        original_chunk: str,
        translated_chunk: str,
        translator_fn,
    ) -> dict[str, str]:
        """Run Layer 3 discovery: find new terms in this chunk's translation.

        Only runs every DISCOVERY_INTERVAL chunks to avoid too many Gemini calls.
        Skips short chunks.

        Returns:
            New terms discovered (also stored internally).
        """
        if not self.enabled:
            return {}

        self._chunk_counter += 1

        # Skip: too short, or not at discovery interval
        if (
            len(original_chunk) < MIN_CHUNK_LEN_FOR_DISCOVERY
            or self._chunk_counter % DISCOVERY_INTERVAL != 0
        ):
            return {}

        prompt = self._build_discovery_prompt(original_chunk, translated_chunk)
        try:
            response = translator_fn(prompt)
            new_terms = parse_extraction_response(response)

            # Filter out seed + existing terms
            existing = {**self._seed, **self._document, **self._discovered}
            new_terms = {k: v for k, v in new_terms.items() if k not in existing}

            self._discovered.update(new_terms)
            self.stats.discovered_count = len(self._discovered)

            if new_terms:
                logger.info(
                    f"[GlossaryPipeline] Layer 3 chunk #{self._chunk_counter}: "
                    f"+{len(new_terms)} terms discovered"
                )
            return new_terms

        except Exception as e:
            logger.warning(f"[GlossaryPipeline] Layer 3 discovery failed: {e}")
            return {}

    def _build_discovery_prompt(self, original: str, translated: str) -> str:
        return (
            "So sánh bản gốc và bản dịch. Liệt kê thuật ngữ chuyên ngành "
            "Toán/CS/AI mới (nếu có) được dịch trong đoạn này.\n"
            "Format: English term → Bản dịch tiếng Việt\n"
            "Chỉ thuật ngữ kỹ thuật, KHÔNG từ thông dụng. "
            "Trả về block ```glossary ... ```. Block rỗng nếu không có gì mới.\n\n"
            f"=== GỐC ===\n{original[:1500]}\n\n"
            f"=== DỊCH ===\n{translated[:1500]}"
        )

    # ── Prompt injection ──────────────────────────────────────────────

    def prompt_for_chunk(self, chunk_text: str) -> str:
        """Build the glossary prefix to prepend to a chunk's translation prompt.

        Filters merged glossary to only terms appearing in the chunk,
        caps at MAX_TERMS_PER_PROMPT, prioritizes:
          1. Document-specific terms (most relevant)
          2. Discovered terms
          3. Seed terms
        The longer / more specific a term, the higher priority.

        Returns empty string if no relevant terms or glossary disabled.
        """
        if not self.enabled:
            return ""

        matched = self._filter_for_chunk(chunk_text)
        if not matched:
            return ""

        self.stats.injections += 1
        self.stats.total_terms_injected += len(matched)

        lines = [f'  "{en}" → "{vi}"' for en, vi in sorted(matched.items())]
        return (
            "=== BẢNG THUẬT NGỮ (dùng ĐÚNG bản dịch này, không thay đổi) ===\n"
            + "\n".join(lines)
            + "\n\n"
        )

    def _filter_for_chunk(self, chunk_text: str) -> dict[str, str]:
        """Return terms from merged glossary that appear in chunk_text."""
        chunk_lower = chunk_text.lower()

        # Merge all layers: document > discovered > seed (later wins on conflict)
        merged: dict[str, str] = {**self._seed, **self._discovered, **self._document}

        matched: dict[str, str] = {}
        for en, vi in merged.items():
            if en in chunk_lower:
                matched[en] = vi

        if len(matched) <= MAX_TERMS_PER_PROMPT:
            return matched

        # Prioritize: document terms first, then by term length (more specific)
        def priority(item):
            en, _ = item
            layer_score = 3 if en in self._document else (2 if en in self._discovered else 1)
            return (layer_score, len(en))

        top = sorted(matched.items(), key=priority, reverse=True)
        return dict(top[:MAX_TERMS_PER_PROMPT])

    # ── Accessors ────────────────────────────────────────────────────

    @property
    def all_terms(self) -> dict[str, str]:
        """Merged glossary from all 3 layers (document overrides seed)."""
        return {**self._seed, **self._discovered, **self._document}

    @property
    def document_terms(self) -> dict[str, str]:
        return dict(self._document)

    @property
    def discovered_terms(self) -> dict[str, str]:
        return dict(self._discovered)

    @property
    def total_count(self) -> int:
        return len(self.all_terms)

    def add_user_terms(self, terms: dict[str, str]) -> None:
        """Manually add/override terms (user edits via API).

        User-provided terms go into document layer → highest priority.
        """
        for en, vi in terms.items():
            self._document[en.lower().strip()] = vi.strip()
        self.stats.document_count = len(self._document)

    def to_api_dict(self) -> dict:
        """Serialize for API response."""
        return {
            "enabled": self.enabled,
            "total": self.total_count,
            "layers": {
                "seed": self.stats.seed_count,
                "document": self.stats.document_count,
                "discovered": self.stats.discovered_count,
            },
            "stats": self.stats.to_dict(),
            "document_terms": self._document,
            "discovered_terms": self._discovered,
        }
