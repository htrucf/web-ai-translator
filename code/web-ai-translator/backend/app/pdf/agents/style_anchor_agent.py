"""StyleAnchorAgent — Sinh "neo văn phong" cho các model song song dùng chung.

Vai trò (con người tương ứng): trưởng nhóm dịch — soạn một mẫu ngắn để
toàn đội theo cùng văn phong (xưng "chúng tôi", giữ thuật ngữ EN trong
ngoặc, dùng giọng học thuật).

Vấn đề cần giải:
  Khi mở K tab song song trong 1 model (hoặc K model nối tiếp), mỗi worker
  KHÔNG thấy lịch sử dịch của worker khác → văn phong drift:
    - Worker A: "Chúng tôi đề xuất..."
    - Worker B: "Tác giả đề xuất..."
    - Worker C: "Bài báo này đề xuất..."

Cách giải:
  Trước phase parallel, dịch 1 đoạn đại diện ở phần đầu tài liệu bằng
  model chính. Bản dịch này KHÔNG vào output cuối — chỉ làm tham chiếu
  văn phong inject vào MỌI prompt sau đó.

State ghi:
  ctx.progress["style_anchor"] = {"en": "...", "vi": "...", "source_model": "gemini"}

Note: agent này KHÔNG bắt buộc — nếu fail, ModelPassAgent vẫn chạy được
nhưng có thể drift văn phong.
"""

from __future__ import annotations

import re

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.processor import chunk_to_text


# Độ dài tối đa cho 1 anchor — cần đủ dài để thấy nhịp câu, thuật ngữ và cách
# xưng hô. Nếu quá ngắn, các worker song song vẫn dễ lệch văn phong.
MAX_ANCHOR_CHARS = 2200
MIN_ANCHOR_CHARS = 900


def _pick_anchor_text(chunks: list) -> str:
    """Lấy đoạn đại diện đầu tài liệu làm anchor.

    Ưu tiên chunk đầu (thường chứa abstract/intro — văn phong đại diện)
    nhưng có thể ghép thêm chunk kế tiếp để mẫu không quá ngắn.
    """
    if not chunks:
        return ""

    parts: list[str] = []
    for chunk in chunks[:5]:  # thử vài chunk đầu để mẫu đủ dài
        text = chunk_to_text(chunk)
        if not text or len(text.strip()) < 50:
            continue
        parts.append(text.strip())
        joined = "\n\n".join(parts)
        if len(joined) >= MIN_ANCHOR_CHARS:
            break

    text = "\n\n".join(parts).strip()
    if not text:
        return ""
    if len(text) <= MAX_ANCHOR_CHARS:
        return text

    cutoff = text[:MAX_ANCHOR_CHARS]
    last_period = max(cutoff.rfind("."), cutoff.rfind("?"), cutoff.rfind("!"))
    if last_period > MAX_ANCHOR_CHARS * 0.55:
        return cutoff[: last_period + 1]
    return cutoff


def _build_anchor_prompt(text: str, glossary_text: str = "") -> str:
    """Prompt riêng cho anchor — yêu cầu dịch + tự mô tả văn phong dùng."""
    return (
        "Dịch đoạn văn bản học thuật sau sang tiếng Việt với chất lượng cao.\n\n"
        + glossary_text
        + "=== QUY TẮC ===\n"
        "1. Văn phong học thuật trang trọng.\n"
        "2. Xưng 'chúng tôi' cho tác giả.\n"
        "3. Giữ nguyên thuật ngữ tiếng Anh trong ngoặc khi cần làm rõ.\n"
        "4. Trả kết quả trong block ```text ... ```.\n\n"
        f"=== NỘI DUNG ===\n```text\n{text}\n```"
    )


def _extract_translated(response: str) -> str:
    if not response:
        return ""
    m = re.search(r"```(?:text)?\s*\n(.*?)```", response, re.DOTALL)
    text = m.group(1).strip() if m else response.strip()
    # Bỏ chatbot leakage
    lines = text.split("\n")
    clean = []
    for line in lines:
        s = line.strip()
        if re.match(r"^(Bạn có muốn|Lưu ý|Note:|Would you|Let me know)", s, re.I):
            break
        clean.append(line)
    return "\n".join(clean).strip()


class StyleAnchorAgent(BaseAgent):
    """Dịch 1 đoạn ngắn → lưu (EN, VI) làm anchor cho phase parallel.

    Idempotent: nếu progress["style_anchor"] đã có → skip.
    """

    name = "StyleAnchorAgent"

    async def run(self, ctx: AgentContext) -> AgentResult:
        if ctx.is_cancelled():
            return AgentResult.fail("Cancelled", recoverable=True)

        existing = ctx.progress.get("style_anchor")
        if existing and existing.get("en") and existing.get("vi"):
            self.log("Style anchor already exists, skipping")
            return AgentResult.ok(
                data=existing, source="cache", chars=len(existing["en"])
            )

        if not ctx.chunks:
            return AgentResult.fail(
                "No chunks available — run PlannerAgent first", recoverable=False
            )

        anchor_en = _pick_anchor_text(ctx.chunks)
        if not anchor_en:
            return AgentResult.fail(
                "Could not pick anchor text from chunks", recoverable=True
            )

        # Glossary hint nếu có
        glossary_text = ""
        if ctx.glossary and ctx.glossary_enabled:
            try:
                from app.pdf.glossary import (
                    filter_glossary_for_chunk,
                    format_glossary_for_prompt,
                )
                filtered = filter_glossary_for_chunk(
                    ctx.glossary, anchor_en, locked=ctx.locked_terms
                )
                glossary_text = format_glossary_for_prompt(
                    filtered, locked=ctx.locked_terms
                )
            except Exception as e:
                self.log(f"Glossary filter failed (non-fatal): {e}", "warn")

        prompt = _build_anchor_prompt(anchor_en, glossary_text=glossary_text)

        try:
            page = await ctx.ensure_page()
            raw = await ctx.translator._send_prompt_and_get_response(page, prompt)
        except Exception as e:
            return AgentResult.fail(
                f"Anchor translation failed: {e}", recoverable=True
            )

        anchor_vi = _extract_translated(raw)
        if not anchor_vi or len(anchor_vi) < 20:
            return AgentResult.fail(
                f"Anchor translation too short ({len(anchor_vi)} chars)",
                recoverable=True,
            )

        source_model = getattr(ctx.translator, "backend_name", "unknown")
        ctx.progress["style_anchor"] = {
            "en": anchor_en,
            "vi": anchor_vi,
            "source_model": source_model,
        }
        ctx.save_progress()

        self.log(
            f"Anchor created ({len(anchor_en)} EN → {len(anchor_vi)} VI chars, "
            f"source={source_model})"
        )
        return AgentResult.ok(
            data=ctx.progress["style_anchor"],
            source=source_model,
            chars=len(anchor_vi),
        )
