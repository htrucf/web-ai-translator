"""GlossaryAgent — Quản lý bảng thuật ngữ chuyên ngành.

Vai trò:
  1. Pre-seed glossary từ global store (cross-document terms đã verify)
  2. Extract glossary ban đầu từ vài chunks đầu (abstract/intro)
  3. (Trong vòng dịch) Refresh glossary từ chunks đã dịch
  4. Cung cấp filter/format helpers cho Translator/Critic

Khác biệt với app/pdf/glossary.py (module gốc):
  - GlossaryAgent điều phối logic cấp cao (gọi Gemini, merge, resume)
  - glossary.py vẫn giữ nguyên các pure-function helpers (extract prompt,
    parse response, filter, format) — agent gọi vào chúng

Note: Refresh trong vòng dịch nằm ở TranslatorAgent (per-chunk hook) hoặc
Coordinator chứ không gọi từ đây — agent này chạy 1 lần ban đầu.
"""

from __future__ import annotations

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.glossary import (
    build_extraction_prompt,
    parse_extraction_response,
    parse_extraction_fields,
    extract_new_terms_prompt,
    parse_extraction_response as parse_new_terms,
    merge_glossary,
)
from app.pdf.processor import chunk_to_text


class GlossaryAgent(BaseAgent):
    """Trích xuất + duy trì glossary cho job.

    Input  (từ ctx):
      - ctx.chunks (đã có từ PlannerAgent)
      - ctx.translator + ctx.ensure_page (để gọi Gemini)

    Output (set vào ctx + AgentResult):
      - ctx.glossary : dict[str, str]
      - progress["glossary"] = {"terms": ..., "enabled": True}
    """

    name = "GlossaryAgent"

    def __init__(self, sample_chunks: int = 3, max_sample_chars: int = 4000):
        self.sample_chunks = sample_chunks
        self.max_sample_chars = max_sample_chars

    async def run(self, ctx: AgentContext) -> AgentResult:
        if not ctx.chunks:
            return AgentResult.fail(
                "No chunks in context — Planner chưa chạy?",
                recoverable=False,
            )

        # 1. Pre-seed từ global glossary
        seeded = self._seed_from_global()
        if seeded:
            self.log(f"Seeded {len(seeded)} terms from global glossary")

        ctx.glossary = dict(seeded)

        # 2. Extract glossary + lĩnh vực từ Gemini (vài chunks đầu)
        extracted, extracted_fields, extraction_error = await self._extract_from_chunks(ctx)
        if extracted:
            ctx.glossary = merge_glossary(ctx.glossary, extracted)
            self.log(f"Extracted {len(extracted)} new terms (total: {len(ctx.glossary)}), "
                     f"{len(extracted_fields)} có lĩnh vực")

        # 3. Persist
        ctx.progress["glossary"] = {
            "terms": ctx.glossary,
            "seed_terms": seeded,
            "document_terms": extracted or {},
            "enabled": ctx.glossary_enabled,
            "locked": list(ctx.locked_terms or []),
            "fields": extracted_fields or {},
            "extraction": {
                "attempted": True,
                "ok": bool(extracted),
                "error": extraction_error or "",
                "seed_count": len(seeded),
                "document_count": len(extracted or {}),
            },
        }
        ctx.save_progress()

        # 4. Print sample
        if ctx.glossary:
            for en, vi in list(ctx.glossary.items())[:5]:
                self.log(f"  {en} → {vi}")
            if len(ctx.glossary) > 5:
                self.log(f"  ... and {len(ctx.glossary) - 5} more")

        return AgentResult.ok(
            data=ctx.glossary,
            num_seeded=len(seeded),
            num_extracted=len(extracted) if extracted else 0,
            num_total=len(ctx.glossary),
        )

    # ── Step 1: Seed from global ───────────────────────────────────────────────

    @staticmethod
    def _seed_from_global() -> dict[str, str]:
        """Pre-seed từ global glossary store (cross-document)."""
        try:
            from app.database import get_global_glossary
            terms = get_global_glossary(min_confidence=0.6, min_frequency=2)
            return dict(terms) if terms else {}
        except Exception as e:
            print(f"[GlossaryAgent] Global glossary seed failed (non-fatal): {e}")
            return {}

    # ── Step 2: Extract from sample chunks ─────────────────────────────────────

    async def _extract_from_chunks(self, ctx: AgentContext) -> tuple[dict[str, str], dict[str, str], str]:
        """Gọi Gemini extract terminology (+ lĩnh vực) từ vài chunks đầu.

        Trả về ``(terms, fields)`` — `fields` map en→lĩnh vực cho các thuật ngữ
        Gemini phân loại được.
        """
        if not ctx.translator or not ctx.ensure_page:
            self.log("Translator/page not available — skipping extraction", "warn")
            return {}, {}, "Translator/page not available"

        sample_count = min(self.sample_chunks, len(ctx.chunks))
        sample_text = self._gather_sample_text(ctx.chunks[:sample_count])

        self.log(f"Extracting glossary from first {sample_count} chunks "
                 f"({len(sample_text)} chars)...")

        try:
            page = await ctx.ensure_page()
            prompt = build_extraction_prompt(sample_text)
            raw = await ctx.translator._send_prompt_and_get_response(page, prompt)
            terms = parse_extraction_response(raw)
            fields = parse_extraction_fields(raw)
            return terms, fields, ""
        except Exception as e:
            self.log(f"Extraction failed: {e}", "warn")
            return {}, {}, str(e)

    def _gather_sample_text(self, sample_chunks: list[list]) -> str:
        parts = [chunk_to_text(c) for c in sample_chunks]
        text = "\n\n".join(parts)
        if len(text) > self.max_sample_chars:
            text = text[: self.max_sample_chars]
        return text

    # ── Helper: refresh during translation (called by Coordinator) ─────────────

    async def refresh_from_chunk(
        self,
        ctx: AgentContext,
        original: str,
        translated: str,
    ) -> dict[str, str]:
        """Extract NEW term pairs sau khi dịch 1 chunk.

        Coordinator gọi định kỳ (mỗi N chunks) để bồi đắp glossary.
        Trả về dict các term mới (chưa có trong ctx.glossary).
        """
        if not ctx.translator or not ctx.ensure_page:
            return {}

        try:
            page = await ctx.ensure_page()
            prompt = extract_new_terms_prompt(original, translated)
            raw = await ctx.translator._send_prompt_and_get_response(page, prompt)
            new_terms = parse_new_terms(raw)
            # Filter terms đã có
            truly_new = {k: v for k, v in new_terms.items() if k not in ctx.glossary}
            if truly_new:
                ctx.glossary = merge_glossary(ctx.glossary, truly_new)
                ctx.progress["glossary"]["terms"] = ctx.glossary
                ctx.save_progress()
                self.log(f"Refresh: +{len(truly_new)} terms (total: {len(ctx.glossary)})")
            return truly_new
        except Exception as e:
            self.log(f"Refresh failed (non-fatal): {e}", "warn")
            return {}
