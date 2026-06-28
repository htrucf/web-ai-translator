"""CrossModelAgreementAgent — Giữ văn phong khi chuyển giữa các model.

Agent này KHÔNG merge nhiều bản dịch thành bản cuối. Eval-loop hiện đã chọn
best-so-far theo từng attempt. Vai trò còn lại của CrossModelAgreementAgent là
tạo "handoff anchor": vài cặp nguồn--dịch gần nhất đã có để model mới bám theo
thuật ngữ, đại từ xưng hô, mức trang trọng và cách diễn đạt của model trước.
"""

from __future__ import annotations

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent
from app.pdf.processor import chunk_to_text


class CrossModelAgreementAgent(BaseAgent):
    """Tạo ngữ cảnh chuyển model để văn phong model cũ và mới khớp nhau."""

    name = "CrossModelAgreementAgent"

    def __init__(self, window: int = 2, max_chars: int = 700):
        self.window = max(1, window)
        self.max_chars = max(200, max_chars)

    async def run(self, ctx: AgentContext) -> AgentResult:
        handoffs = ctx.progress.get("cross_model_handoffs", {})
        active = ctx.progress.get("cross_model_active_handoffs", {})
        state = ctx.progress.get("cross_model_state", {})
        return AgentResult.ok(
            data={"handoffs": handoffs, "active_handoffs": active, "state": state},
            handoff_count=len(handoffs),
            active_handoff_count=len(active),
        )

    def record_success(
        self,
        ctx: AgentContext,
        chunk_index: int,
        *,
        model: str,
    ):
        """Ghi model đang giữ văn phong thành công gần nhất."""
        if not model:
            return
        state = ctx.progress.setdefault("cross_model_state", {})
        state["current_style_owner"] = model
        state["last_success_chunk"] = chunk_index

    def prepare_handoff(
        self,
        ctx: AgentContext,
        chunk_index: int,
        *,
        from_model: str,
        to_model: str,
        codec=None,
    ) -> dict | None:
        """Lưu anchor văn phong cho lần dịch `chunk_index` bằng `to_model`."""
        if not to_model or from_model == to_model:
            return None

        anchor = self.build_handoff_anchor(
            ctx,
            chunk_index,
            from_model=from_model,
            to_model=to_model,
            codec=codec,
        )
        if not anchor:
            return None

        key = self.handoff_key(chunk_index, to_model)
        ctx.progress.setdefault("cross_model_handoffs", {})[key] = anchor
        save = getattr(ctx, "save_progress", None)
        if callable(save):
            try:
                save()
            except Exception:
                pass
        return anchor

    def prepare_global_handoff(
        self,
        ctx: AgentContext,
        chunk_index: int,
        *,
        from_model: str,
        to_model: str,
        reason: str = "model_failover",
        codec=None,
    ) -> dict | None:
        """Tạo handoff dùng chung cho mọi chunk kế tiếp của `to_model`."""
        from_model = from_model or "unknown"
        if not to_model or from_model == to_model:
            return None

        anchor = self.build_handoff_anchor(
            ctx,
            chunk_index,
            from_model=from_model,
            to_model=to_model,
            codec=codec,
        )
        if not anchor:
            return None

        anchor = dict(anchor)
        anchor["scope"] = "global"
        anchor["reason"] = reason

        active = ctx.progress.setdefault("cross_model_active_handoffs", {})
        active[to_model] = anchor
        ctx.progress.setdefault("cross_model_handoff_history", []).append(anchor)
        save = getattr(ctx, "save_progress", None)
        if callable(save):
            try:
                save()
            except Exception:
                pass
        return anchor

    @classmethod
    def get_handoff_anchor(
        cls,
        ctx: AgentContext,
        chunk_index: int,
        model: str,
    ) -> dict | None:
        """Lấy handoff phù hợp nhất: local chunk trước, global target sau."""
        local = ctx.progress.get("cross_model_handoffs", {}).get(
            cls.handoff_key(chunk_index, model)
        )
        if local:
            return local
        return ctx.progress.get("cross_model_active_handoffs", {}).get(model)

    @staticmethod
    def current_style_owner(ctx: AgentContext) -> str | None:
        return ctx.progress.get("cross_model_state", {}).get("current_style_owner")

    def build_handoff_anchor(
        self,
        ctx: AgentContext,
        chunk_index: int,
        *,
        from_model: str,
        to_model: str,
        codec=None,
    ) -> dict | None:
        examples = self._collect_previous_examples(ctx, chunk_index, codec=codec)
        if not examples:
            return None

        en_parts = []
        vi_parts = []
        for i, (src, mt) in enumerate(examples, 1):
            en_parts.append(f"[handoff {i}] {src}")
            vi_parts.append(f"[handoff {i}] {mt}")

        en = "\n\n".join(en_parts)[: self.max_chars]
        vi = "\n\n".join(vi_parts)[: self.max_chars]
        if not en.strip() or not vi.strip():
            return None

        return {
            "en": en,
            "vi": vi,
            "from_model": from_model,
            "to_model": to_model,
            "chunk_index": chunk_index,
            "reason": "cross_model_style_handoff",
        }

    @staticmethod
    def merge_style_anchor(
        base_anchor: dict | None,
        handoff_anchor: dict | None,
    ) -> dict | None:
        """Ghép style anchor gốc với handoff anchor cho prompt dịch."""
        if not handoff_anchor:
            return base_anchor
        if not base_anchor:
            return handoff_anchor

        en = "\n\n".join(
            part for part in (base_anchor.get("en"), handoff_anchor.get("en")) if part
        )
        vi = "\n\n".join(
            part for part in (base_anchor.get("vi"), handoff_anchor.get("vi")) if part
        )
        merged = dict(base_anchor)
        merged.update({
            "en": en,
            "vi": vi,
            "handoff": handoff_anchor,
        })
        return merged

    @staticmethod
    def handoff_key(chunk_index: int, model: str) -> str:
        return f"{chunk_index}:{model}"

    def _collect_previous_examples(
        self,
        ctx: AgentContext,
        chunk_index: int,
        *,
        codec=None,
    ) -> list[tuple[str, str]]:
        examples: list[tuple[str, str]] = []
        start = max(0, chunk_index - self.window)
        for idx in range(start, chunk_index):
            if idx >= len(ctx.chunks):
                continue
            src = self._source_text(ctx, idx, codec=codec)
            mt = self._translation_text(ctx, idx, codec=codec)
            if src and mt:
                examples.append((src, mt))
        return examples

    @staticmethod
    def _source_text(ctx: AgentContext, idx: int, *, codec=None) -> str:
        try:
            if codec is not None:
                return codec.to_source_text(ctx.chunks[idx]).strip()
            return chunk_to_text(ctx.chunks[idx]).strip()
        except Exception:
            return ""

    @staticmethod
    def _translation_text(ctx: AgentContext, idx: int, *, codec=None) -> str:
        final = ctx.progress.get("translated_chunks", {})
        saved = final.get(str(idx), "")
        if saved:
            return saved.strip()
        try:
            if codec is not None:
                return codec.to_translation_text(ctx.chunks[idx]).strip()
            parts = []
            for i, block in enumerate(ctx.chunks[idx], 1):
                text = getattr(block, "translated_text", None) or ""
                if text.strip():
                    parts.append(f"[{i}] {text.strip()}")
            return "\n\n".join(parts).strip()
        except Exception:
            return ""
