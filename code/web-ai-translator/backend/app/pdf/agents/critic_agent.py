"""CriticAgent — Review bản dịch và auto-fix các block xấu.

Vai trò:
  1. Quét tất cả blocks (đã dịch) → tìm fixable (untranslated, glossary mismatch, length anomaly)
  2. Critique mỗi block xấu → sinh error list cụ thể (HeuristicCritic + LLMCritic nếu có Ollama)
  3. Refiner: dùng error list để sinh prompt sửa đúng chỗ
  4. Apply translations sửa, đo số block đã fix
  5. Lặp tối đa MAX_ROUNDS vòng

Khác biệt với pipeline.py._fix_quality_issues:
  - Tách riêng thành agent → có thể chạy độc lập (offline mode trên job đã có)
  - Trả AgentResult với metrics (rounds_run, blocks_fixed, errors_found)

Note: agent này phụ thuộc page (Playwright) cho refine step → cần ctx.translator.
Nếu không có page (offline), chỉ critique mà không fix.
"""

from __future__ import annotations

import asyncio
import re

from dataclasses import dataclass

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.critic import critique_blocks, format_critique_for_prompt
from app.pdf.glossary import (
    filter_glossary_for_chunk,
    format_glossary_for_prompt,
)
from app.pdf.quality import find_fixable_blocks


@dataclass(frozen=True)
class RepairDecision:
    """Quyết định sửa cho một attempt trong eval-loop."""

    action: str
    model: str
    candidate_models: tuple[str, ...]
    reason: str
    attempt_no: int
    source_strategy: str

    @property
    def should_stop(self) -> bool:
        return self.action == "stop"

    def provenance(self) -> dict:
        return {
            "repair_action": self.action,
            "repair_reason": self.reason,
            "repair_attempt": self.attempt_no,
            "repair_source_strategy": self.source_strategy,
        }


class CriticAgent(BaseAgent):
    """Review + auto-fix vòng lặp.

    Input  (từ ctx):
      - ctx.blocks (đã dịch)
      - ctx.glossary

    Output:
      - Modify ctx.blocks in-place (assign translated_text mới)
      - AgentResult.metrics: rounds_run, blocks_fixed, errors_found
    """

    name = "CriticAgent"

    MAX_ROUNDS = 2
    MAX_FIX_BLOCKS = 30
    MINI_CHUNK_CHARS = 1500

    def __init__(
        self,
        max_rounds: int | None = None,
        judge_model: str | None = None,
        max_repair_attempts: int | None = None,
    ):
        if max_rounds is not None:
            self.MAX_ROUNDS = max_rounds
        self.judge_model = judge_model or "qwen2.5:7b"
        self.max_repair_attempts = max_repair_attempts

    def decide_repair(
        self,
        model_plan,
        *,
        heuristic_score: float | None = None,
        mqm_score: float | None = None,
        errors: list | None = None,
    ) -> RepairDecision:
        """Chọn policy sửa cho eval-loop: dịch/refine/đổi model/ensemble/stop.

        ModelPassAgent cấp attempt number + model order. CriticAgent quyết định
        hành động sửa dựa trên plan đó; các score/error truyền vào để mở rộng
        policy sau này mà không phải đụng EvalPipeline.
        """
        attempt_no = int(getattr(model_plan, "attempt_no", 1) or 1)
        strategy = str(getattr(model_plan, "strategy", "") or "").lower()
        model = str(getattr(model_plan, "model", "") or "")
        candidates = tuple(getattr(model_plan, "candidate_models", ()) or ())

        if self.max_repair_attempts is not None and attempt_no > self.max_repair_attempts:
            return RepairDecision(
                action="stop",
                model=model,
                candidate_models=(),
                reason="repair_budget_exhausted",
                attempt_no=attempt_no,
                source_strategy=strategy,
            )

        if not model:
            return RepairDecision(
                action="stop",
                model="",
                candidate_models=(),
                reason="no_model_available",
                attempt_no=attempt_no,
                source_strategy=strategy,
            )

        if strategy == "refine":
            return RepairDecision(
                action="refine",
                model=model,
                candidate_models=(model,),
                reason="critic_refine",
                attempt_no=attempt_no,
                source_strategy=strategy,
            )
        if strategy == "escalate":
            return RepairDecision(
                action="change_model",
                model=model,
                candidate_models=(model,),
                reason="switch_model_after_failed_repair",
                attempt_no=attempt_no,
                source_strategy=strategy,
            )
        if strategy == "ensemble":
            unique_candidates = tuple(dict.fromkeys(candidates or (model,)))
            return RepairDecision(
                action="ensemble",
                model=model,
                candidate_models=unique_candidates,
                reason="compare_candidates_and_keep_best",
                attempt_no=attempt_no,
                source_strategy=strategy,
            )

        return RepairDecision(
            action="translate",
            model=model,
            candidate_models=(model,),
            reason="first_translation_attempt",
            attempt_no=attempt_no,
            source_strategy=strategy or "initial",
        )

    async def run(self, ctx: AgentContext) -> AgentResult:
        if not ctx.blocks:
            return AgentResult.fail("No blocks in context", recoverable=False)

        # Check Ollama availability for LLMCritic
        use_llm = self._ollama_available()
        if use_llm:
            self.log(f"Using LLM ({self.judge_model}) + heuristic")
        else:
            self.log("Using heuristic only (Ollama not available)")

        active_glossary = ctx.glossary if ctx.glossary_enabled else {}

        total_fixed = 0
        total_errors = 0
        rounds_run = 0

        for fix_round in range(self.MAX_ROUNDS):
            if ctx.is_cancelled():
                self.log("Cancelled mid-fix", "warn")
                break

            fixable = find_fixable_blocks(ctx.blocks, active_glossary or None)
            if not fixable:
                self.log(f"Round {fix_round + 1}: no fixable blocks → stop")
                break

            fixable = fixable[: self.MAX_FIX_BLOCKS]
            self.log(f"Round {fix_round + 1}/{self.MAX_ROUNDS}: "
                     f"critiquing {len(fixable)} blocks...")

            # Step 1: Critic — generate error list
            critiques = critique_blocks(
                fixable,
                glossary=active_glossary or None,
                use_llm=use_llm,
                llm_model=self.judge_model,
            )
            total_errors += len(critiques)
            self.log(f"Critic found errors in {len(critiques)}/{len(fixable)} blocks")

            ctx.progress["status"] = (
                f"critic+refine ({len(critiques)} errors, "
                f"round {fix_round + 1}/{self.MAX_ROUNDS})"
            )
            ctx.save_progress()

            # Step 2: Refiner — group blocks → send refine prompts
            if not ctx.translator or not ctx.ensure_page:
                self.log("No translator/page — skipping refine step", "warn")
                break

            try:
                page = await ctx.ensure_page()
                await ctx.translator.start_new_chat(page)
            except Exception as e:
                self.log(f"Failed to open fresh session: {e}", "warn")

            fixed_this_round = await self._refine_round(
                ctx, fixable, critiques, active_glossary
            )
            total_fixed += fixed_this_round
            rounds_run += 1
            self.log(f"Round {fix_round + 1}: refined "
                     f"{fixed_this_round}/{len(fixable)} blocks")

            if fixed_this_round == 0:
                break

        return AgentResult.ok(
            data={"blocks_fixed": total_fixed},
            rounds_run=rounds_run,
            blocks_fixed=total_fixed,
            errors_found=total_errors,
            llm_used=use_llm,
        )

    # ── Per-chunk refine (dùng trong vòng dịch khép kín) ───────────────────────

    async def refine_chunk(
        self,
        chunk: list,
        *,
        page,
        translator,
        glossary: dict[str, str] | None = None,
        locked_terms: list | None = None,
        errors: list | None = None,
        codec=None,
    ) -> str:
        """Refine ĐÚNG 1 chunk. Là bước SỬA của Critic-hub trong eval-loop.

        `errors` (nếu có) = error-list do panel judge (Local/Glossary/...) gom
        sẵn → dùng trực tiếp để xây critique, KHÔNG tính lại. Không truyền thì
        tự tính bằng HeuristicCritic (dùng độc lập / fallback).

        `codec` (EvalCodec, nếu có) cấp cách render text nguồn/bản dịch theo
        định dạng (PDF/Office/LaTeX). None → dùng _blocks_to_numbered (PDF).

        Build refine prompt → gửi qua `translator` trên `page` → trả text `[N]`
        đã sửa ("" nếu fail). Critique rỗng → dịch lại sạch để vẫn có lần thử mới.
        """
        active_glossary = glossary or {}
        locked = locked_terms or []
        if codec is not None:
            original_text = codec.to_source_text(chunk)
            bad_translation = codec.to_translation_text(chunk)
        else:
            original_text = self._blocks_to_numbered(chunk, "original")
            bad_translation = self._blocks_to_numbered(chunk, "translation")

        if errors:
            critique_text = "\n".join(e.format_for_prompt() for e in errors)
        else:
            critiques = critique_blocks(chunk, active_glossary or None, use_llm=False)
            critique_text = format_critique_for_prompt(critiques) if critiques else ""

        glossary_text = ""
        if active_glossary:
            filtered = filter_glossary_for_chunk(
                active_glossary, original_text, locked=locked
            )
            glossary_text = format_glossary_for_prompt(filtered, locked=locked)

        if critique_text:
            prompt = self._build_refine_prompt(
                original_text, bad_translation, critique_text, glossary_text
            )
        else:
            prompt = self._build_translate_prompt(original_text, glossary_text)

        try:
            raw = await translator._send_prompt_and_get_response(page, prompt)
        except Exception as e:
            self.log(f"refine_chunk send failed: {e}", "warn")
            return ""
        return self._extract_text(raw)

    # ── Refine round ──────────────────────────────────────────────────────────

    async def _refine_round(
        self,
        ctx: AgentContext,
        fixable: list,
        critiques: dict,
        active_glossary: dict[str, str],
    ) -> int:
        """Chạy 1 vòng refine: group → build prompt → send → apply."""
        mini_chunks = self._group_blocks(fixable)
        fixed_total = 0

        for ci, mini_chunk in enumerate(mini_chunks):
            if ctx.is_cancelled():
                break

            original_text = self._blocks_to_numbered(mini_chunk, "original")
            bad_translation = self._blocks_to_numbered(mini_chunk, "translation")

            # Lấy critique tương ứng với blocks trong chunk này
            chunk_start = sum(len(mini_chunks[j]) for j in range(ci))
            chunk_critiques = {
                (k - chunk_start): v
                for k, v in critiques.items()
                if chunk_start <= k < chunk_start + len(mini_chunk)
            }
            critique_text = format_critique_for_prompt(chunk_critiques)

            glossary_text = ""
            if active_glossary:
                filtered = filter_glossary_for_chunk(active_glossary, original_text, locked=ctx.locked_terms)
                glossary_text = format_glossary_for_prompt(filtered, locked=ctx.locked_terms)

            # Build prompt — refine if errors, fresh translate if no errors
            if critique_text:
                prompt = self._build_refine_prompt(
                    original_text, bad_translation, critique_text, glossary_text
                )
                self.log(f"Refine chunk {ci + 1}/{len(mini_chunks)} "
                         f"({len(chunk_critiques)} blocks with errors)")
            else:
                prompt = self._build_translate_prompt(original_text, glossary_text)
                self.log(f"Refresh chunk {ci + 1}/{len(mini_chunks)} (no errors)")

            try:
                page = await ctx.ensure_page()
                raw = await ctx.translator._send_prompt_and_get_response(page, prompt)
            except Exception as e:
                self.log(f"Refine chunk {ci + 1} failed: {e}", "warn")
                continue

            translated = self._extract_text(raw)
            if translated:
                fixed = self._apply_fix(translated, mini_chunk)
                fixed_total += fixed

            delay = ctx.settings.get("delay_between_chunks", 2)
            if delay > 0 and ci < len(mini_chunks) - 1:
                await asyncio.sleep(delay)

        return fixed_total

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _ollama_available() -> bool:
        try:
            from app.pdf.llm_judge import is_available as ollama_available
            return ollama_available()
        except Exception:
            return False

    @staticmethod
    def _group_blocks(blocks: list, max_chars: int = 1500) -> list[list]:
        chunks = []
        current = []
        current_len = 0
        for b in blocks:
            tlen = len(b.text or "")
            if current and current_len + tlen > max_chars:
                chunks.append(current)
                current = []
                current_len = 0
            current.append(b)
            current_len += tlen
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _blocks_to_numbered(blocks: list, field: str) -> str:
        parts = []
        for i, b in enumerate(blocks):
            if field == "translation":
                t = (b.translated_text or b.text or "").strip()
            else:
                t = (b.text or "").strip()
            parts.append(f"[{i + 1}] {t}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_refine_prompt(
        text: str, bad_translation: str, critique_text: str, glossary_text: str = ""
    ) -> str:
        return (
            "Bạn là chuyên gia hiệu đính bản dịch học thuật Anh-Việt.\n\n"
            "Bản dịch dưới đây có LỖI. Hãy sửa đúng theo danh sách lỗi được chỉ ra.\n\n"
            + glossary_text
            + "=== QUY TẮC BẮT BUỘC ===\n"
            "1. Giữ nguyên đánh số [1], [2], [3]... trong output.\n"
            "2. CHỈ sửa những lỗi được liệt kê — KHÔNG thay đổi phần đã dịch đúng.\n"
            "3. GIỮ NGUYÊN: công thức toán học, ký hiệu, số liệu, tên riêng, viết tắt.\n"
            "4. KHÔNG thêm giải thích. Trả về bên trong block ```text ... ```.\n\n"
            "=== VĂN BẢN GỐC (EN) ===\n"
            f"```text\n{text}\n```\n\n"
            "=== BẢN DỊCH CŨ (VI) — CÓ LỖI ===\n"
            f"```text\n{bad_translation}\n```\n\n"
            "=== LỖI CẦN SỬA ===\n"
            f"{critique_text}\n\n"
            "=== YÊU CẦU ===\n"
            "Viết lại bản dịch đã sửa lỗi bên trong block ```text ... ```:"
        )

    @staticmethod
    def _build_translate_prompt(text: str, glossary_text: str = "") -> str:
        # Same as TranslatorAgent — tránh circular import: viết lại ngắn gọn
        return (
            "Dịch các đoạn văn bản sau sang tiếng Việt.\n\n"
            + glossary_text
            + "=== QUY TẮC BẮT BUỘC ===\n"
            "1. Mỗi đoạn được đánh số [1], [2], [3]... Giữ nguyên đánh số.\n"
            "2. CHỈ dịch text tiếng Anh sang tiếng Việt.\n"
            "3. GIỮ NGUYÊN 100%: công thức, ký hiệu, số liệu, tên riêng, citations.\n"
            "4. KHÔNG thêm giải thích. Trả về trong block ```text ... ```.\n\n"
            f"=== NỘI DUNG ===\n```text\n{text}\n```"
        )

    @staticmethod
    def _extract_text(response: str) -> str:
        if not response:
            return ""
        match = re.search(r'```(?:text)?\s*\n(.*?)```', response, re.DOTALL)
        text = match.group(1).strip() if match else response.strip()
        # Strip chatbot artifacts
        lines = text.split("\n")
        clean = []
        for line in lines:
            s = line.strip()
            if re.match(
                r'^(Bạn có muốn|Lưu ý|Note:|Chú ý:|Would you|Let me know|'
                r'Nếu bạn cần|Hy vọng|Tôi có thể hỗ trợ|Tôi có thể giúp|'
                r'Nếu bạn muốn|Hãy cho tôi biết|If you)',
                s, re.IGNORECASE,
            ):
                break
            clean.append(line)
        while clean and not clean[-1].strip():
            clean.pop()
        return "\n".join(clean)

    @staticmethod
    def _apply_fix(translated_text: str, blocks: list) -> int:
        """Parse [N] markers và assign back vào blocks. Returns count fixed."""
        from app.pdf.quality import _has_vietnamese

        pattern = re.compile(r'\[(\d+)\]\s*(.*?)(?=\n\[|\Z)', re.DOTALL)
        matches = pattern.findall(translated_text)

        fixed = 0
        for num_str, text in matches:
            idx = int(num_str) - 1
            if 0 <= idx < len(blocks):
                new_text = text.strip()
                old_text = (blocks[idx].translated_text or "").strip()
                if new_text and new_text != old_text and _has_vietnamese(new_text):
                    blocks[idx].translated_text = new_text
                    fixed += 1
        return fixed
