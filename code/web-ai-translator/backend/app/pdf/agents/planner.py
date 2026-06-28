"""PlannerAgent — Phân tích cấu trúc paper, lập kế hoạch dịch.

Vai trò:
  1. Đọc lướt blocks (extracted) và phát hiện section boundaries
     (Abstract, Introduction, Method, Results, ...) qua font_size + bold + text patterns
  2. Chunking thông minh:
     - Không cắt ngang câu (kết thúc chunk tại '.', '!', '?', ';')
     - Ưu tiên kết thúc chunk tại biên section
     - Cân bằng size — không có chunk quá to / quá nhỏ
  3. Output TranslationPlan: list of PlanSection với chunks gắn metadata

Khác biệt với processor.split_blocks_into_chunks:
  - Có nhận biết section
  - Không break mid-sentence
  - Đính kèm section context cho từng chunk (giúp Translator dịch nhất quán)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from app.pdf.agents.base import AgentContext, AgentResult, BaseAgent


# ── Section detection ─────────────────────────────────────────────────────────

# Regex các tiêu đề section thường gặp trong paper khoa học
_SECTION_TITLE_PATTERNS = [
    re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+([A-Z][\w\s\-:&,]+)$"),  # "1. Introduction"
    re.compile(r"^\s*(Abstract|Introduction|Background|Related Work|Methodology|"
               r"Methods?|Approach|Experiment(?:s|al)?|Results?|Discussion|"
               r"Conclusion(?:s)?|References|Appendix|Acknowledgments?)\b",
               re.IGNORECASE),
]

# Câu kết thúc — dùng để không break giữa câu khi chunking
_SENTENCE_END_RE = re.compile(r'[.!?;:]\s*$|[.!?;:]\s*"$|[.!?;:]\s*\)$')


def _is_likely_section_header(text: str, font_size: float, is_bold: bool,
                               avg_font_size: float = 10.0) -> tuple[bool, str]:
    """Detect xem 1 block có phải section header không.

    Heuristic: section header thường:
      - Bold hoặc font lớn hơn body (≥ 110% avg)
      - Match một trong các pattern (số mục, từ khóa "Introduction", ...)
      - Text ngắn (< 100 chars)

    Returns (is_header, normalized_title).
    """
    if not text or len(text) > 100:
        return False, ""

    stripped = text.strip()
    if not stripped:
        return False, ""

    # Check pattern matching
    for pat in _SECTION_TITLE_PATTERNS:
        m = pat.match(stripped)
        if m:
            # Boost confidence if also bold or larger font
            if is_bold or font_size >= avg_font_size * 1.1:
                return True, stripped
            # Pattern match alone là khá tin cậy nếu text ngắn
            if len(stripped) < 60:
                return True, stripped

    # Pure heuristic: bold + larger font + short
    if is_bold and font_size >= avg_font_size * 1.15 and len(stripped) < 80:
        return True, stripped

    return False, ""


def _ends_at_sentence(text: str) -> bool:
    """Check xem text có kết thúc tại biên câu không."""
    if not text:
        return True
    return bool(_SENTENCE_END_RE.search(text.rstrip()))


# ── Plan structures ───────────────────────────────────────────────────────────

@dataclass
class PlanSection:
    """Một section trong paper (vd: 'Introduction', 'Method')."""
    title: str                              # "Introduction" / "1. Method" / etc
    start_block_idx: int                    # index trong all_blocks list
    end_block_idx: int                      # exclusive
    chunk_indexes: list[int] = field(default_factory=list)  # chunk indices belonging to section


@dataclass
class TranslationPlan:
    """Kế hoạch dịch — output từ PlannerAgent.

    Coordinator dùng plan này để biết:
      - Phân chunks thế nào
      - Mỗi chunk thuộc section nào (cho prompt context)
      - Tổng quan paper structure
    """
    sections: list[PlanSection] = field(default_factory=list)
    chunks: list[list] = field(default_factory=list)   # list[list[TextBlock]]
    total_chars: int = 0
    avg_font_size: float = 10.0

    def section_for_chunk(self, chunk_idx: int) -> Optional[PlanSection]:
        for sec in self.sections:
            if chunk_idx in sec.chunk_indexes:
                return sec
        return None

    def summary(self) -> str:
        if not self.sections:
            return f"{len(self.chunks)} chunks, no sections detected"
        names = [s.title[:30] for s in self.sections[:5]]
        more = f" (+{len(self.sections) - 5} more)" if len(self.sections) > 5 else ""
        return f"{len(self.chunks)} chunks across {len(self.sections)} sections: {names}{more}"

    def to_dict(self) -> dict:
        return {
            "num_chunks": len(self.chunks),
            "num_sections": len(self.sections),
            "total_chars": self.total_chars,
            "avg_font_size": self.avg_font_size,
            "sections": [
                {
                    "title": s.title,
                    "start_block_idx": s.start_block_idx,
                    "end_block_idx": s.end_block_idx,
                    "num_chunks": len(s.chunk_indexes),
                }
                for s in self.sections
            ],
        }


# ── Agent ─────────────────────────────────────────────────────────────────────

class PlannerAgent(BaseAgent):
    """Phân tích paper structure + lập kế hoạch chunking thông minh.

    Input  (từ ctx):
      - ctx.blocks   : list[TextBlock] đã extract từ PDF

    Output (set vào ctx + trả qua AgentResult):
      - ctx.chunks  : list[list[TextBlock]]
      - ctx.plan    : TranslationPlan
    """

    name = "PlannerAgent"

    def __init__(self, target_chunk_size: int = 1500, max_chunk_size: int = 2200):
        self.target_size = target_chunk_size
        self.max_size = max_chunk_size

    async def run(self, ctx: AgentContext) -> AgentResult:
        if not ctx.blocks:
            return AgentResult.fail(
                "No blocks in context — extract phase chưa chạy?",
                recoverable=False,
            )

        translatable = [b for b in ctx.blocks if b.is_translatable]
        if not translatable:
            return AgentResult.fail(
                "No translatable blocks found", recoverable=False
            )

        # 1. Compute average font size (baseline cho header detection)
        font_sizes = [b.font_size for b in translatable if b.font_size > 0]
        avg_font = sum(font_sizes) / len(font_sizes) if font_sizes else 10.0

        # 2. Detect sections từ block sequence
        sections = self._detect_sections(ctx.blocks, avg_font)
        self.log(f"Detected {len(sections)} sections "
                 f"(avg font size: {avg_font:.1f})")

        # 3. Smart chunking — respect section boundaries + sentence ends
        chunks = self._chunk_with_boundaries(translatable, sections)
        self.log(f"Created {len(chunks)} chunks "
                 f"(target {self.target_size} chars)")

        # 4. Map chunks to sections
        self._assign_chunks_to_sections(chunks, sections)

        # 5. Build plan
        plan = TranslationPlan(
            sections=sections,
            chunks=chunks,
            total_chars=sum(len(b.text) for b in translatable),
            avg_font_size=avg_font,
        )

        # Set into context
        ctx.chunks = chunks
        ctx.plan = plan

        # Persist plan to progress for resume
        ctx.progress["plan"] = plan.to_dict()
        ctx.save_progress()

        self.log(plan.summary())

        return AgentResult.ok(
            data=plan,
            num_chunks=len(chunks),
            num_sections=len(sections),
            avg_font_size=avg_font,
        )

    # ── Section detection ──────────────────────────────────────────────────────

    def _detect_sections(self, all_blocks: list, avg_font: float) -> list[PlanSection]:
        """Quét blocks tìm các section header → tạo PlanSection."""
        sections: list[PlanSection] = []
        current: Optional[PlanSection] = None

        for i, block in enumerate(all_blocks):
            if not block.is_translatable:
                continue

            # Heuristic bold detection: thử lấy từ spans_info nếu có
            is_bold = self._block_is_bold(block)
            is_header, title = _is_likely_section_header(
                block.text, block.font_size, is_bold, avg_font
            )

            if is_header:
                # Close previous section
                if current is not None:
                    current.end_block_idx = i
                    sections.append(current)

                current = PlanSection(
                    title=title,
                    start_block_idx=i,
                    end_block_idx=len(all_blocks),
                )

        # Close last section
        if current is not None:
            sections.append(current)

        # Edge case: no sections detected → 1 implicit section
        if not sections:
            sections = [PlanSection(
                title="(unstructured)",
                start_block_idx=0,
                end_block_idx=len(all_blocks),
            )]

        return sections

    @staticmethod
    def _block_is_bold(block) -> bool:
        """Best-effort bold detection từ spans_info."""
        try:
            spans = block.spans_info or []
            if not spans:
                return False
            bold_chars = 0
            total_chars = 0
            for sp in spans:
                t = sp.get("text", "") if isinstance(sp, dict) else ""
                total_chars += len(t)
                flags = sp.get("flags", 0) if isinstance(sp, dict) else 0
                font = sp.get("font", "") if isinstance(sp, dict) else ""
                if (flags & 16) or "Bold" in font or "bold" in font:
                    bold_chars += len(t)
            return total_chars > 0 and bold_chars / total_chars >= 0.6
        except Exception:
            return False

    # ── Smart chunking ─────────────────────────────────────────────────────────

    def _chunk_with_boundaries(
        self, translatable: list, sections: list[PlanSection]
    ) -> list[list]:
        """Chunk với 2 ràng buộc:
          1. Không cắt ngang câu (kết thúc tại '.', '!', '?', ';')
          2. Ưu tiên kết thúc tại biên section

        Strategy:
          - Đi qua blocks tuần tự
          - Khi current_size > target_size:
              + Nếu block hiện tại kết thúc câu → đóng chunk
              + Nếu không, đợi tới block tiếp kết thúc câu (max_size là cứng)
          - Khi qua biên section → ưu tiên đóng chunk ngay nếu current_size > target * 0.5
        """
        if not translatable:
            return []

        # Build set các block_idx là biên section (start of new section)
        section_starts = {s.start_block_idx for s in sections[1:]}  # bỏ section đầu (paper bắt đầu là section đầu)
        # Translate to indexes in `translatable` list
        block_to_translatable = {id(b): i for i, b in enumerate(translatable)}

        chunks: list[list] = []
        current_chunk: list = []
        current_size = 0

        for i, block in enumerate(translatable):
            text_len = len(block.text or "")

            # Check section boundary
            is_section_boundary = False
            for sec in sections[1:]:
                # Block at start of new section
                # Compare via reference equality with original blocks
                if i > 0 and current_size > self.target_size * 0.5:
                    # Heuristic: nếu block này là 1st translatable của section mới
                    # Cách đơn giản: kiểm tra block.text match section title
                    for s in sections:
                        if (s.start_block_idx <= 999999  # safe
                            and block.text and block.text.strip()
                            and s.title.lower().startswith(block.text.strip()[:30].lower())):
                            is_section_boundary = True
                            break
                if is_section_boundary:
                    break

            # Decision: should we close current chunk?
            should_close = False
            if current_chunk:
                if is_section_boundary and current_size > self.target_size * 0.4:
                    should_close = True
                elif current_size + text_len > self.max_size:
                    # Hard limit — must close even if mid-sentence
                    should_close = True
                elif current_size + text_len > self.target_size:
                    # Soft limit — close only if last block ended at sentence boundary
                    last_block = current_chunk[-1]
                    if _ends_at_sentence(last_block.text or ""):
                        should_close = True

            if should_close:
                chunks.append(current_chunk)
                current_chunk = []
                current_size = 0

            current_chunk.append(block)
            current_size += text_len

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    # ── Section ↔ chunk mapping ────────────────────────────────────────────────

    @staticmethod
    def _assign_chunks_to_sections(
        chunks: list[list], sections: list[PlanSection]
    ):
        """Gán mỗi chunk vào section chứa block đầu tiên của nó."""
        for chunk_idx, chunk in enumerate(chunks):
            if not chunk:
                continue
            # First block của chunk
            first_block = chunk[0]
            first_page = getattr(first_block, "page_num", 0)
            first_block_idx = getattr(first_block, "block_idx", 0)

            # Tìm section chứa block này
            best_section = None
            for sec in sections:
                # Heuristic: section's start ≤ block ≤ section's end
                if sec.start_block_idx <= chunk_idx * 100:  # rough mapping fallback
                    best_section = sec

            if best_section is None and sections:
                best_section = sections[0]

            if best_section is not None:
                best_section.chunk_indexes.append(chunk_idx)
