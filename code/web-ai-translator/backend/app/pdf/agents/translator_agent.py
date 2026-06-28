"""TranslatorAgent — Tác tử dịch từng chunk qua Playwright.

Vai trò:
  Per-chunk dịch:
    1. Build prompt (có glossary filter + context memory inject)
    2. Send qua web AI (Gemini/ChatGPT) → đợi response
    3. Extract translated text → kiểm tra truncation
    4. Retry với exponential backoff nếu lỗi
    5. Browser relaunch nếu page chết

Khác biệt với pipeline.py._translate_chunk_with_retry:
  - Trả về AgentResult (success/data/errors) thay vì raw string
  - Có thể được Coordinator skip/retry cấp cao
  - Per-chunk operation — không loop nhiều chunks (Coordinator loop)
  - VLM fallback đã có sẵn trong WebAITranslator (vision_nav.py)

Lưu ý error handling: Web UI brittle:
  - TargetClosedError → relaunch
  - TimeoutError → fresh session + retry
  - Truncation → session rotation + retry
  - Generic exception → exponential backoff
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.glossary import filter_glossary_for_chunk, format_glossary_for_prompt
from app.pdf.math_protector import protect_chunk_math
from app.pdf.processor import chunk_to_text


# ── Helpers ──────────────────────────────────────────────────────────────────

_URL_OR_EMAIL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)\S+"
    r"|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"
    r"|\bdoi:\s*\S+"
    r"|\b10\.\d{4,9}/\S+"
)
_WORD_RE = re.compile(r"[A-Za-zÀ-ỹ]{2,}")


class _UrlProtector:
    def __init__(self):
        self._values: list[str] = []

    def protect(self, text: str) -> str:
        def repl(match):
            self._values.append(match.group(0))
            return f"<<URL_{len(self._values)}>>"

        return _URL_OR_EMAIL_RE.sub(repl, text or "")

    def restore(self, text: str) -> str:
        out = text or ""
        for i, value in enumerate(self._values, start=1):
            out = out.replace(f"<<URL_{i}>>", value)
        return out

    @property
    def protected_count(self) -> int:
        return len(self._values)


def _protect_chunk_urls(chunk: list) -> _UrlProtector:
    protector = _UrlProtector()
    for block in chunk:
        block.text = protector.protect(block.text or "")
    return protector


def _numbered_original_text(originals: list[str]) -> str:
    return "\n\n".join(f"[{i + 1}] {text}" for i, text in enumerate(originals))


def _should_bypass_ai(originals: list[str]) -> tuple[bool, str]:
    """Return True for chunks that should be preserved without an AI call."""
    raw_parts = [(text or "").strip() for text in originals if (text or "").strip()]
    raw = " ".join(raw_parts).strip()
    if not raw:
        return True, "empty"

    without_urls = _URL_OR_EMAIL_RE.sub(" ", raw)
    words = _WORD_RE.findall(without_urls)
    alpha_chars = sum(len(w) for w in words)

    if not without_urls.strip():
        return True, "url_only"
    if alpha_chars < 8 and len(raw) <= 40:
        return True, "too_short"
    return False, ""

def _build_translation_prompt(
    text: str,
    glossary_text: str = "",
    context_text: str = "",
    section_hint: str = "",
    math_protected: bool = False,
    style_anchor: dict | None = None,
    anti_hallucination: bool = False,
) -> str:
    """Build prompt cho dịch — bao gồm glossary, context memory, section hint.

    `math_protected=True` indicates the text contains <<MATH_N>> placeholders
    that the LLM must preserve verbatim — they will be substituted back with
    the original math expressions after translation.

    `style_anchor` (optional): {"en": "...", "vi": "..."} — mẫu văn phong
    chuẩn để worker song song (multi-tab / cross-model) bám theo. Khi parallel
    worker KHÔNG thấy context của worker khác, anchor là cách duy nhất giữ
    văn phong đồng nhất giữa các bản dịch.

    `anti_hallucination=True` thêm quy tắc cấm suy đoán nội dung chunk kế tiếp
    — dùng khi worker không có context memory.
    """
    section_block = ""
    if section_hint:
        section_block = (
            f"=== NGỮ CẢNH SECTION ===\n"
            f"Đoạn này thuộc phần: {section_hint}\n\n"
        )

    anchor_block = ""
    if style_anchor and style_anchor.get("en") and style_anchor.get("vi"):
        anchor_block = (
            "=== VĂN PHONG CHUẨN (BẮT BUỘC THEO) ===\n"
            "Dưới đây là 1 đoạn đã dịch chuẩn — hãy giữ NGUYÊN văn phong này "
            "(xưng hô, cách dùng thuật ngữ, độ trang trọng):\n"
            f"EN: {style_anchor['en'][:1800]}\n"
            f"VI: {style_anchor['vi'][:1800]}\n\n"
        )

    math_rule = (
        "8. TUYỆT ĐỐI giữ nguyên các placeholder dạng <<MATH_1>>, <<MATH_2>>... "
        "không sửa, không dịch, không thêm khoảng trắng. Chúng sẽ được thay "
        "lại bằng công thức gốc sau khi dịch.\n"
        if math_protected else ""
    )

    url_rule = (
        "10. TUYỆT ĐỐI giữ nguyên các placeholder dạng <<URL_1>>, <<URL_2>>... "
        "không dịch, không rút gọn và không đổi thứ tự. Chúng sẽ được thay lại "
        "bằng đường dẫn, DOI hoặc email gốc sau khi dịch.\n"
    )

    anti_hallu_rule = (
        "9. KHÔNG suy đoán nội dung nối với chunk trước hoặc sau. CHỈ dịch "
        "những gì có trong đoạn này, kể cả khi câu bị cắt giữa chừng — "
        "giữ nguyên ranh giới.\n"
        if anti_hallucination else ""
    )

    return (
        "Dịch các đoạn văn bản sau sang tiếng Việt.\n\n"
        + section_block
        + anchor_block
        + context_text
        + glossary_text
        + "=== QUY TẮC BẮT BUỘC ===\n"
        "1. Mỗi đoạn được đánh số [1], [2], [3]... Giữ nguyên đánh số trong output.\n"
        "2. CHỈ dịch phần text tiếng Anh sang tiếng Việt.\n"
        "3. GIỮ NGUYÊN 100%: công thức toán học, ký hiệu, số liệu, tên riêng, "
        "viết tắt khoa học, citations, đường dẫn URL, DOI và email.\n"
        "4. KHÔNG thêm giải thích, ghi chú, câu hỏi. CHỈ trả về bản dịch.\n"
        "5. Trả về bên trong block ```text ... ```.\n"
        + ("6. BẮT BUỘC dùng đúng bản dịch trong BẢNG THUẬT NGỮ ở trên.\n"
           if glossary_text else "")
        + ("7. NHẤT QUÁN văn phong với VĂN PHONG CHUẨN / NGỮ CẢNH DỊCH THUẬT ở trên.\n"
           if (context_text or anchor_block) else "")
        + math_rule
        + anti_hallu_rule
        + url_rule
        + "\n=== VÍ DỤ ===\n"
        "Input:\n"
        "[1] This paper proposes a new method for image classification.\n\n"
        "[2] Our approach achieves 95.3% accuracy on ImageNet.\n\n"
        "Output:\n"
        "```text\n"
        "[1] Bài báo này đề xuất một phương pháp mới cho phân loại hình ảnh.\n\n"
        "[2] Phương pháp của chúng tôi đạt độ chính xác 95.3% trên ImageNet.\n"
        "```\n\n"
        f"=== NỘI DUNG CẦN DỊCH ===\n```text\n{text}\n```"
    )


def _extract_text_from_response(response: str) -> str:
    """Tách phần text dịch ra khỏi response (xử lý ```text``` block + chatbot artifacts)."""
    if not response:
        return response

    match = re.search(r'```(?:text)?\s*\n(.*?)```', response, re.DOTALL)
    text = match.group(1).strip() if match else response.strip()

    # Strip chatbot leakage
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
        if re.match(
            r'^(===\s*(QUY TẮC|NỘI DUNG CẦN DỊCH|VÍ DỤ)|'
            r'Dịch các đoạn văn bản sau sang tiếng Việt)',
            s,
        ):
            break
        clean.append(line)
    while clean and not clean[-1].strip():
        clean.pop()
    return "\n".join(clean)


def _is_truncated(original: str, translated: str) -> bool:
    if len(original) < 200:
        return False
    if not translated:
        return True
    return len(translated) / len(original) < 0.3


# ── Per-chunk request wrapper ─────────────────────────────────────────────────

@dataclass
class TranslateRequest:
    """Input cho 1 lần TranslatorAgent.translate_chunk()."""
    chunk_index: int
    chunk: list                  # list[TextBlock]
    section_hint: str = ""       # tên section đang dịch
    max_retries: int = 2
    base_backoff: int = 5
    # Parallel mode (multi-tab / multi-model) — worker không thấy lịch sử
    # các worker khác, dùng anchor + anti_hallucination để chống drift.
    style_anchor: dict | None = None      # {"en": "...", "vi": "..."}
    anti_hallucination: bool = False
    # Override page khi worker dùng tab riêng (multi-tab parallel)
    worker_page: object | None = None


# ── Agent ─────────────────────────────────────────────────────────────────────

class TranslatorAgent(BaseAgent):
    """Dịch 1 chunk qua web AI.

    Note: Agent này được gọi nhiều lần bởi Coordinator (mỗi chunk 1 lần).
    Chính `run()` không loop — coordinator phụ trách iterate.

    `run()` mặc định lấy chunk_index từ ctx.progress["_current_chunk_index"]
    để tương thích với BaseAgent interface. Tuy nhiên cách dùng phổ biến hơn
    là gọi trực tiếp `translate_chunk(ctx, request)`.
    """

    name = "TranslatorAgent"

    async def run(self, ctx: AgentContext) -> AgentResult:
        """Default run: lấy current chunk index từ ctx và dịch."""
        idx = ctx.progress.get("_current_chunk_index")
        if idx is None or idx >= len(ctx.chunks):
            return AgentResult.fail("No current chunk index in context")

        section_hint = ""
        if ctx.plan is not None:
            sec = ctx.plan.section_for_chunk(idx)
            if sec:
                section_hint = sec.title

        request = TranslateRequest(
            chunk_index=idx,
            chunk=ctx.chunks[idx],
            section_hint=section_hint,
            max_retries=ctx.settings.get("max_retries", 2),
            base_backoff=ctx.settings.get("base_backoff", 5),
        )
        return await self.translate_chunk(ctx, request)

    # ── Main entry point ──────────────────────────────────────────────────────

    async def translate_chunk(
        self, ctx: AgentContext, request: TranslateRequest
    ) -> AgentResult:
        """Dịch 1 chunk với retry + browser recovery."""
        chunk = request.chunk
        idx = request.chunk_index
        total = len(ctx.chunks)

        # Protect math expressions across the whole chunk with a SHARED
        # placeholder counter so we can restore from the LLM's concatenated
        # response in one pass. block.text is mutated in place — restore it
        # in `finally` so subsequent runs see clean source.
        originals_text, math_protector = protect_chunk_math(chunk)
        protected_count = math_protector.protected_count
        url_protector = _protect_chunk_urls(chunk)
        url_protected_count = url_protector.protected_count
        try:
            original_text = chunk_to_text(chunk)
            # Build prompt with the UNPROTECTED text for glossary matching:
            # placeholders break case-insensitive substring matching.
            unprotected_for_glossary = "\n\n".join(
                f"[{i + 1}] {orig}" for i, orig in enumerate(originals_text)
            )

            bypass, bypass_reason = _should_bypass_ai(originals_text)
            if bypass:
                translated = _numbered_original_text(originals_text)
                self.log(
                    f"Skipping AI for chunk {idx + 1}/{total} ({bypass_reason})"
                )
                return AgentResult.ok(
                    data={
                        "chunk_index": idx,
                        "original": unprotected_for_glossary,
                        "translated": translated,
                        "filtered_glossary": {},
                        "bypassed_ai": True,
                        "bypass_reason": bypass_reason,
                    },
                    num_glossary_terms=0,
                    has_context=False,
                    math_protected=protected_count,
                    url_protected=url_protected_count,
                )

            # Build prompt — ghép glossary + context memory + section hint
            glossary_text = ""
            num_terms = 0
            filtered: dict[str, str] = {}
            if ctx.glossary and ctx.glossary_enabled:
                filtered = filter_glossary_for_chunk(
                    ctx.glossary, unprotected_for_glossary, locked=ctx.locked_terms
                )
                glossary_text = format_glossary_for_prompt(filtered, locked=ctx.locked_terms)
                num_terms = len(filtered)

            context_text = ""
            if ctx.memory is not None:
                try:
                    context_text = ctx.memory.retrieve_context(
                        unprotected_for_glossary
                    )
                except Exception as e:
                    self.log(f"Context retrieve failed (non-fatal): {e}", "warn")

            prompt = _build_translation_prompt(
                original_text,
                glossary_text=glossary_text,
                context_text=context_text,
                section_hint=request.section_hint,
                math_protected=protected_count > 0,
                style_anchor=request.style_anchor,
                anti_hallucination=request.anti_hallucination,
            )

            sec_info = f" [{request.section_hint}]" if request.section_hint else ""
            gloss_info = f" gloss:{num_terms}" if num_terms else ""
            ctx_info = (f" mem:{ctx.memory.size}"
                        if (ctx.memory and ctx.memory.size) else "")
            math_info = f" math:{protected_count}" if protected_count else ""
            url_info = f" url:{url_protected_count}" if url_protected_count else ""
            self.log(f"Translating chunk {idx + 1}/{total}{sec_info}"
                     f"{gloss_info}{ctx_info}{math_info}{url_info}")

            # Retry loop — pass worker_page for multi-tab parallel mode
            translated = await self._translate_with_retry(
                ctx, prompt, original_text, idx, total,
                request.max_retries, request.base_backoff,
                worker_page=request.worker_page,
            )
        finally:
            # Always restore source `block.text` so a retry / next chunk does
            # not see corrupted placeholders from a previous attempt.
            for i, orig in enumerate(originals_text):
                if i < len(chunk):
                    chunk[i].text = orig

        # Restore math placeholders in the response string. Safe even if
        # `translated` is empty (returns "" unchanged).
        if protected_count > 0 and translated:
            translated = math_protector.restore(translated)
        if url_protected_count > 0 and translated:
            translated = url_protector.restore(translated)

        if not translated:
            return AgentResult(
                success=False,
                data={
                    "chunk_index": idx,
                    "original": chunk_to_text(chunk),
                    "translated": "",
                    "filtered_glossary": filtered,
                },
                errors=[f"Chunk {idx + 1} failed after retries"],
                recoverable=True,
            )

        return AgentResult.ok(
            data={
                "chunk_index": idx,
                "original": chunk_to_text(chunk),
                "translated": translated,
                "filtered_glossary": filtered,
            },
            num_glossary_terms=num_terms,
            has_context=bool(context_text),
            math_protected=protected_count,
            url_protected=url_protected_count,
        )

    # ── Retry / recovery ──────────────────────────────────────────────────────

    async def _translate_with_retry(
        self,
        ctx: AgentContext,
        prompt: str,
        original_text: str,
        chunk_idx: int,
        total: int,
        max_retries: int,
        base_backoff: int,
        worker_page: object | None = None,
    ) -> str:
        """Try translate với recovery cho các loại lỗi web UI.

        Khi `worker_page` được set (multi-tab parallel mode), worker dùng tab
        riêng của mình và KHÔNG đụng ctx.page (vì ctx.page là tab chính).
        Tab chết → return "" để ModelPassAgent quyết định relaunch tab.
        """
        from playwright._impl._errors import TargetClosedError, Error as PlaywrightError

        is_worker_tab = worker_page is not None

        for attempt in range(max_retries + 1):
            if ctx.is_cancelled():
                return ""

            try:
                page = worker_page if is_worker_tab else await ctx.ensure_page()
                raw_response = await ctx.translator._send_prompt_and_get_response(
                    page, prompt
                )
                translated = _extract_text_from_response(raw_response)

                # Truncation detection — rotate session and retry once
                if _is_truncated(original_text, translated):
                    self.log(f"Chunk {chunk_idx + 1} truncated "
                             f"(attempt {attempt + 1}), rotating session...", "warn")
                    if is_worker_tab:
                        # Worker tab: chỉ start_new_chat trên tab của mình
                        try:
                            await ctx.translator._backend.start_new_chat(page)
                        except Exception:
                            return ""
                    else:
                        page = await ctx.ensure_page()
                        await ctx.translator.start_new_chat(page)
                        ctx.page = page

                    raw_response = await ctx.translator._send_prompt_and_get_response(
                        page, prompt
                    )
                    translated = _extract_text_from_response(raw_response)

                    if _is_truncated(original_text, translated):
                        raise RuntimeError("Response still truncated after rotation")

                return translated

            except (TargetClosedError, PlaywrightError) as e:
                self.log(f"Browser closed during chunk {chunk_idx + 1}: {e}", "error")
                if is_worker_tab:
                    # Worker tab chết — return ngay, ModelPassAgent sẽ tự
                    # relaunch tab và push lại chunk vào queue.
                    return ""
                ctx.page = None
                ctx.context = None
                if attempt < max_retries:
                    self.log("Relaunching browser in 5s...")
                    await asyncio.sleep(5)
                else:
                    return ""

            except TimeoutError as e:
                self.log(f"Gemini timeout chunk {chunk_idx + 1}: {e}", "error")
                if is_worker_tab:
                    return ""
                ctx.page = None
                if attempt < max_retries:
                    await asyncio.sleep(5)
                    try:
                        if ctx.context:
                            new_page = await ctx.context.new_page()
                            await ctx.translator.start_new_chat(new_page)
                            ctx.page = new_page
                    except Exception:
                        ctx.page = None
                        ctx.context = None
                else:
                    return ""

            except Exception as e:
                if attempt < max_retries:
                    wait = base_backoff * (2 ** attempt)
                    self.log(f"Chunk {chunk_idx + 1} failed "
                             f"(attempt {attempt + 1}/{max_retries + 1}): {e}", "warn")
                    self.log(f"Retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    if is_worker_tab:
                        # Worker tab: chỉ rotate session, không đụng ctx.page
                        try:
                            await ctx.translator._backend.start_new_chat(worker_page)
                        except Exception:
                            return ""
                    else:
                        try:
                            page = await ctx.ensure_page()
                            await ctx.translator.start_new_chat(page)
                        except Exception:
                            ctx.page = None
                else:
                    self.log(f"Chunk {chunk_idx + 1} FAILED after "
                             f"{max_retries + 1} attempts: {e}", "error")
                    return ""

        return ""
