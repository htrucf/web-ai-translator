"""Pipeline dich thuat PDF-only: extract text blocks -> dich qua Gemini -> rebuild PDF.

Module tach biet, khong anh huong pipeline LaTeX hien tai.
"""

import asyncio
import json
import os
import re
import time

from app.pdf.processor import (
    TextBlock,
    extract_text_blocks,
    split_blocks_into_chunks,
    chunk_to_text,
    chunk_to_text_with_budget,
    parse_translated_chunk,
    rebuild_pdf,
    rebuild_pdf_inplace,
    get_pdf_info,
)
from app.utils.safe_io import atomic_write_json
from app.pdf.quality import check_translation_quality, find_fixable_blocks
from app.pdf.critic import critique_blocks, format_critique_for_prompt
from app.pdf.diagnostics import run_diagnostics
from app.pdf.glossary import (
    build_extraction_prompt,
    parse_extraction_response,
    parse_extraction_fields,
    filter_glossary_for_chunk,
    format_glossary_for_prompt,
    extract_new_terms_prompt,
    parse_extraction_response as parse_new_terms,
    merge_glossary,
)
from app.pdf.context_memory import ContextMemory
from app.pdf.math_protector import protect_chunk_math
from app.services.translator import WebAITranslator
from app.utils.translation_meta import build_meta
from app import paths
from app.audit import AuditLogger, set_current, clear_current, write_env_snapshot
from app.audit.logger import (
    PHASE_INIT, PHASE_EXTRACTION, PHASE_CHUNKING, PHASE_GLOSSARY,
    PHASE_TRANSLATING, PHASE_QUALITY_FIX, PHASE_REBUILDING,
    PHASE_QUALITY, PHASE_VALIDATION, PHASE_FINISHED,
)


class PdfTranslationPipeline:
    """Pipeline dich PDF-only: extract -> translate -> rebuild."""

    # Mode-specific settings
    MODE_SETTINGS = {
        "standard": {
            "chunks_per_session": 10,
            "delay_between_chunks": 2,   # seconds
            "max_retries": 2,
            "base_backoff": 5,           # seconds
        },
        "book": {
            "chunks_per_session": 5,
            "delay_between_chunks": 8,   # seconds — gentler on Gemini
            "max_retries": 4,
            "base_backoff": 15,          # seconds
        },
    }

    def __init__(self, work_dir: str | None = None, mode: str = "standard"):
        # Subprocess callers always pass an absolute work_dir; default kicks in
        # only for direct in-process construction (tests, ad-hoc scripts).
        self.work_dir = work_dir or paths.workspace_dir()
        self.mode = mode if mode in self.MODE_SETTINGS else "standard"
        self.settings = self.MODE_SETTINGS[self.mode]

        # MIX mode (experimental): split chunks 50/50 between two backends.
        # Enable with: MIX_BACKENDS_ENABLED=1 + MIX_BACKENDS_PAIR=gemini,chatgpt
        self._mix_pair: list[str] = []
        self._mix_swapped = False
        if os.environ.get("MIX_BACKENDS_ENABLED", "").lower() in ("1", "true", "yes"):
            pair_raw = os.environ.get("MIX_BACKENDS_PAIR", "gemini,chatgpt")
            candidates = [b.strip().lower() for b in pair_raw.split(",") if b.strip()]
            if len(candidates) >= 2:
                self._mix_pair = candidates[:2]
                print(f"[PdfPipeline] MIX backends enabled: "
                      f"first half={self._mix_pair[0]}, second half={self._mix_pair[1]}")

        if self._mix_pair:
            self.translator = WebAITranslator(backend=self._mix_pair[0])
        else:
            self.translator = WebAITranslator()
        self.progress_file = ""
        self._cancelled = False
        self._page = None       # Active Playwright page (stored for relaunch)
        self._context = None    # Active browser context
        self._audit: AuditLogger | None = None
        self._audit_token = None
        self._job_started_at: float = 0.0
        # Tracked so a top-level exception can record exactly where it died
        # (which chunk index, which phase) instead of just the exception repr.
        self._current_chunk_idx: int = -1
        self._current_phase: str = "init"

    # ── Progress management ─────────────────────────────────────

    def _get_job_dir(self, job_id: str) -> str:
        d = os.path.join(self.work_dir, "jobs", job_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _load_progress(self, job_id: str) -> dict:
        self.progress_file = os.path.join(
            self._get_job_dir(job_id), "progress.json"
        )
        if os.path.exists(self.progress_file):
            with open(self.progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        else:
            progress = {
                "translated_chunks": {},
                "status": "pending",
                "source_type": "pdf_only",
            }
        # Disk-first reconciliation — chunk files are the source of truth.
        # Recover from a corrupted/legacy progress.json (missing key, wrong
        # type) by re-reading whatever chunk_NNN_translated.txt exists.
        self._reconcile_translated_chunks_from_disk(job_id, progress)
        return progress

    def _reconcile_translated_chunks_from_disk(self, job_id: str, progress: dict):
        chunks_dir = os.path.join(self._get_job_dir(job_id), "chunks")
        if not isinstance(progress.get("translated_chunks"), dict):
            progress["translated_chunks"] = {}
        if not isinstance(progress.get("failed_chunks"), list):
            progress["failed_chunks"] = []
        if not os.path.isdir(chunks_dir):
            return
        recovered = 0
        for name in os.listdir(chunks_dir):
            if not name.endswith("_translated.txt"):
                continue
            try:
                idx = int(name.split("_")[1])
            except (IndexError, ValueError):
                continue
            key = str(idx)
            if key in progress["translated_chunks"]:
                continue
            try:
                with open(os.path.join(chunks_dir, name), "r", encoding="utf-8") as f:
                    text = f.read()
                if text.strip():
                    progress["translated_chunks"][key] = text
                    recovered += 1
            except Exception:
                continue
        if recovered:
            print(f"[PdfPipeline] Reconciled {recovered} chunk(s) from disk into progress.json")

    def _save_progress(self, progress: dict):
        # Atomic — readers (HTTP handlers polling status) never see torn JSON.
        atomic_write_json(self.progress_file, progress)

    def _save_chunk(self, job_id: str, chunk_index: int, original: str, translated: str):
        chunk_dir = os.path.join(self._get_job_dir(job_id), "chunks")
        os.makedirs(chunk_dir, exist_ok=True)
        with open(os.path.join(chunk_dir, f"chunk_{chunk_index:03d}_original.txt"),
                  "w", encoding="utf-8") as f:
            f.write(original)
        with open(os.path.join(chunk_dir, f"chunk_{chunk_index:03d}_translated.txt"),
                  "w", encoding="utf-8") as f:
            f.write(translated)

    def cancel(self):
        """Cancel the running pipeline."""
        self._cancelled = True

    def _enter_phase(self, phase: str) -> None:
        """Set audit phase + mirror to self._current_phase for error reports."""
        self._current_phase = phase
        if self._audit is not None:
            try:
                self._audit.set_phase(phase)
            except Exception:
                pass

    def _record_error(self, progress: dict, exc: BaseException) -> dict:
        """Capture a structured error_detail into `progress` and persist.

        Stores type, message, traceback, phase and chunk_idx_at_error so the
        UI and the retry wrapper can show *where* and *why* the run died
        instead of just the exception repr. Returns the error_detail dict.
        """
        import traceback as _tb
        from datetime import datetime, timezone

        tb_str = _tb.format_exc()
        # _current_phase mirrors set_phase(); fall back to audit logger if unset.
        phase = self._current_phase or getattr(self._audit, "_phase", "unknown")
        chunk_idx = self._current_chunk_idx
        detail = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "phase": phase,
            "chunk_idx_at_error": chunk_idx if chunk_idx >= 0 else None,
            "traceback": tb_str[-4000:],  # cap to last 4KB
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        progress["error_detail"] = detail
        # Short human-readable status — full payload lives in error_detail.
        where = f"chunk {chunk_idx + 1}" if chunk_idx >= 0 else phase
        progress["status"] = f"error in {where}: {detail['type']}: {detail['message'][:120]}"
        try:
            self._save_progress(progress)
        except Exception:
            pass
        if self._audit is not None:
            try:
                self._audit.log(
                    "job.error",
                    error_type=detail["type"],
                    error_message=detail["message"],
                    phase=phase,
                    chunk_idx_at_error=detail["chunk_idx_at_error"],
                )
            except Exception:
                pass
        return detail

    def _finalize_audit(self, status: str, **extra) -> None:
        """Log job.finished + close audit logger + clear contextvar.

        Idempotent — gọi nhiều lần không sao. Không raise.
        """
        if self._audit is None:
            return
        try:
            self._audit.set_phase(PHASE_FINISHED)
            self._audit.log(
                "job.finished",
                status=status,
                total_duration_seconds=round(time.time() - self._job_started_at, 3),
                **extra,
            )
        except Exception:
            pass
        try:
            self._audit.close()
        except Exception:
            pass
        try:
            clear_current(self._audit_token)
        except Exception:
            pass
        self._audit = None
        self._audit_token = None

    @staticmethod
    def _rebuild_memory_from_disk(
        memory: ContextMemory, job_dir: str, progress: dict
    ) -> int:
        """Rebuild context memory từ chunk files trên disk (resume support).

        Dùng khi progress.json không có context_memory section nhưng đã có
        translated_chunks (job cũ resume sang phiên bản mới có RAG).
        """
        chunks_dir = os.path.join(job_dir, "chunks")
        if not os.path.isdir(chunks_dir):
            return 0

        translated_chunks = progress.get("translated_chunks", {})
        if not translated_chunks:
            return 0

        added = 0
        for chunk_key in sorted(translated_chunks.keys(), key=lambda k: int(k)):
            try:
                chunk_idx = int(chunk_key)
            except ValueError:
                continue

            orig_path = os.path.join(chunks_dir, f"chunk_{chunk_idx:03d}_original.txt")
            trans_path = os.path.join(chunks_dir, f"chunk_{chunk_idx:03d}_translated.txt")
            if not (os.path.isfile(orig_path) and os.path.isfile(trans_path)):
                continue

            try:
                with open(orig_path, "r", encoding="utf-8") as f:
                    original = f.read()
                with open(trans_path, "r", encoding="utf-8") as f:
                    translated = f.read()
                if original.strip() and translated.strip():
                    memory.add(chunk_idx, original, translated)
                    added += 1
            except Exception as e:
                print(f"[PdfPipeline] Failed to load chunk {chunk_idx} for memory: {e}")

        if added:
            print(f"[PdfPipeline] Rebuilt context memory from disk: {added} chunks")
        return added

    @staticmethod
    def _collect_page_sizes(pdf_path: str) -> list[dict]:
        """Return [{page, width, height}, ...] in PDF point units, 0-indexed."""
        try:
            import fitz  # type: ignore
        except ImportError:
            import pymupdf as fitz  # type: ignore
        sizes: list[dict] = []
        doc = fitz.open(pdf_path)
        try:
            for i, page in enumerate(doc):
                sizes.append({
                    "page": i,
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                })
        finally:
            doc.close()
        return sizes

    @staticmethod
    def _build_chunk_block_map(
        chunks: list, page_sizes: list[dict]
    ) -> dict:
        """Map chunk index → list of {page, bbox} for the frontend overlay.

        Bboxes are kept in PDF point units (matches react-pdf's natural scale,
        which is then multiplied by the rendered scale).
        """
        chunk_blocks: list[list[dict]] = []
        for chunk_idx, chunk in enumerate(chunks):
            entries = []
            for block in chunk:
                bbox = getattr(block, "bbox", None)
                if not bbox or len(bbox) != 4:
                    continue
                entries.append({
                    "page": int(getattr(block, "page_num", 0)),
                    "block_idx": int(getattr(block, "block_idx", 0)),
                    "bbox": [float(bbox[0]), float(bbox[1]),
                             float(bbox[2]), float(bbox[3])],
                })
            chunk_blocks.append(entries)
        return {
            "chunks": chunk_blocks,
            "page_sizes": page_sizes,
        }

    async def _ensure_page(self):
        """Return a live Playwright page, relaunching browser if it was closed."""
        from playwright._impl._errors import TargetClosedError, Error as PlaywrightError

        # Test if current page is still alive
        if self._page is not None:
            try:
                await self._page.evaluate("1")
                return self._page
            except (TargetClosedError, PlaywrightError, Exception):
                print("[PdfPipeline] Browser was closed — relaunching...")
                try:
                    await self.translator.cleanup()
                except Exception:
                    pass
                self._page = None
                self._context = None

        # Launch fresh browser
        print("[PdfPipeline] Launching browser...")
        self._context, self._page = await self.translator.launch_browser()
        await self.translator.start_new_chat(self._page)
        return self._page

    async def _maybe_swap_backend(self, chunk_idx: int, total_chunks: int, progress: dict):
        """MIX mode: swap from backend A → B at the document midpoint.

        Triggers exactly once when chunk_idx >= total_chunks // 2. Cleans up
        the active translator, replaces self.translator with a fresh one for
        backend B, and resets the page/context so the next `_ensure_page` call
        launches B's browser.
        """
        if not self._mix_pair or self._mix_swapped:
            return
        midpoint = max(1, total_chunks // 2)
        if chunk_idx < midpoint:
            return

        a, b = self._mix_pair[0], self._mix_pair[1]
        print(f"[PdfPipeline] MIX mode: swapping {a} → {b} "
              f"at chunk {chunk_idx + 1}/{total_chunks}")
        try:
            await self.translator.cleanup()
        except Exception as e:
            print(f"[PdfPipeline] MIX: cleanup of {a} failed (non-fatal): {e}")
        self._page = None
        self._context = None
        self.translator = WebAITranslator(backend=b)
        self.translator.audit = self._audit
        self._mix_swapped = True
        progress["ai_backend"] = b
        progress["ai_backend_mix"] = {
            "first_half": a,
            "second_half": b,
            "swap_at_chunk": chunk_idx,
            "midpoint": midpoint,
        }
        self._save_progress(progress)
        if self._audit:
            try:
                self._audit.log(
                    "backend.swapped",
                    from_backend=a,
                    to_backend=b,
                    at_chunk=chunk_idx,
                    total_chunks=total_chunks,
                )
            except Exception:
                pass

    # ── Translation prompt ──────────────────────────────────────

    @staticmethod
    def _build_prompt(
        text: str,
        glossary_text: str = "",
        context_text: str = "",
        math_protected: bool = False,
        has_length_budget: bool = False,
    ) -> str:
        """Build a translation prompt for plain text (not LaTeX).

        Args:
            text: Source text to translate (numbered blocks).
            glossary_text: Glossary section formatted by glossary.format_glossary_for_prompt.
            context_text: Translation memory section formatted by
                ContextMemory.retrieve_context — chứa các đoạn đã dịch tương tự
                để giữ nhất quán văn phong & thuật ngữ xuyên session.
            math_protected: True if `text` contains <<MATH_N>> placeholders
                that the LLM must reproduce verbatim.
            has_length_budget: True if `text` contains "(max ~N chars)" hints
                per block (E1) — adds a rule asking the model to respect
                the budget while still translating accurately.
        """
        math_rule = (
            "8. TUYỆT ĐỐI giữ nguyên các placeholder dạng <<MATH_1>>, <<MATH_2>>... "
            "không sửa, không dịch, không thêm khoảng trắng. Chúng sẽ được thay "
            "lại bằng công thức gốc sau khi dịch.\n"
            if math_protected else ""
        )
        budget_rule = (
            "9. CÁCH NÉN AN TOÀN — Mỗi đoạn có ghi chú '(max ~N chars)' là "
            "dung lượng vật lý của ô trong PDF gốc. Hãy nén THEO THỨ TỰ ưu "
            "tiên sau, chỉ áp dụng đến khi vừa với budget:\n"
            "   (a) Bỏ hư từ thừa khi không gây hiểu nhầm:\n"
            "       • 'của' giữa hai danh từ: 'phương pháp của chúng tôi' → 'phương pháp chúng tôi'\n"
            "       • 'được' trong bị động khi chủ động tự nhiên hơn\n"
            "       • 'đã/đang/sẽ' khi thời thể hiện rõ qua ngữ cảnh\n"
            "       • 'một cách [tính từ]' khi không cần làm rõ\n"
            "       • 'thì', 'là', 'rằng', 'mà' khi thừa cú pháp\n"
            "       • 'các', 'những' khi số nhiều hiển nhiên\n"
            "   (b) Cụm danh từ thay mệnh đề:\n"
            "       • 'việc phân loại hình ảnh' → 'phân loại hình ảnh'\n"
            "       • 'cho phép chúng ta có thể' → 'cho phép'\n"
            "       • 'có khả năng [động từ]' → '[động từ] được'\n"
            "   (c) Đồng nghĩa ngắn hơn không đổi sắc thái:\n"
            "       • 'tiến hành thử nghiệm' → 'thử nghiệm'\n"
            "       • 'thực hiện đánh giá' → 'đánh giá'\n"
            "       • 'sử dụng [X] để [Y]' → 'dùng [X] để [Y]'\n"
            "   TUYỆT ĐỐI KHÔNG NÉN (luôn dịch đầy đủ, kể cả khi vượt budget):\n"
            "       • Thuật ngữ chuyên ngành, tên người, tổ chức, địa danh\n"
            "       • Citations: [1], (Smith, 2020), số tham chiếu Hình/Bảng\n"
            "       • Số liệu, công thức, ký hiệu, đơn vị\n"
            "       • Phủ định ('không', 'chưa'), khẳng định mạnh ('luôn', 'mọi')\n"
            "       • Lượng hóa ('một số', 'tất cả', 'phần lớn', 'hầu hết')\n"
            "       • Điều kiện ('nếu', 'trừ khi', 'với điều kiện', 'khi và chỉ khi')\n"
            "       • So sánh ('hơn', 'kém', 'ngang bằng', 'tốt nhất')\n"
            "       • Liên kết logic ('do đó', 'tuy nhiên', 'mặt khác', 'vì vậy')\n"
            "   Nếu sau khi áp dụng (a)(b)(c) vẫn vượt budget — TRẢ VỀ BẢN ĐẦY ĐỦ. "
            "Hệ thống có cơ chế giãn vùng vật lý, không bị cắt nội dung.\n"
            "10. Đoạn có '(table cell, ...)' là 1 ô bảng — dịch CỰC NGẮN, "
            "tránh chủ ngữ/từ nối thừa, giữ phong cách electronic table.\n"
            "11. Đoạn có '(caption, ...)' là chú thích Hình/Bảng — giữ format "
            "'Hình N: ...' hoặc 'Bảng N: ...', viết gọn 1 câu.\n"
            "12. KHÔNG copy bất kỳ ghi chú trong ngoặc nào ('(max ~...)', "
            "'(table cell, ...)', '(caption, ...)') vào output — chỉ tham khảo.\n"
            if has_length_budget else ""
        )
        return (
            "Dịch các đoạn văn bản sau sang tiếng Việt.\n\n"
            + context_text
            + glossary_text
            + "=== QUY TẮC BẮT BUỘC ===\n"
            "1. Mỗi đoạn được đánh số [1], [2], [3]... Giữ nguyên đánh số trong output.\n"
            "2. CHỈ dịch phần text tiếng Anh sang tiếng Việt.\n"
            "3. GIỮ NGUYÊN 100%: công thức toán học, ký hiệu, số liệu, tên riêng, "
            "viết tắt khoa học, citations.\n"
            "4. KHÔNG thêm giải thích, ghi chú, câu hỏi. CHỈ trả về bản dịch.\n"
            "4b. TÔN TRỌNG BẢN GỐC: KHÔNG bổ sung viết tắt trong ngoặc đơn "
            "(vd '(XAI)', '(ML)', '(NLP)') hay paraphrase nếu bản gốc không có. "
            "Số lượng cụm trong ngoặc đơn ở bản dịch phải KHỚP với bản gốc.\n"
            "5. Trả về bên trong block ```text ... ```.\n"
            + ("6. BẮT BUỘC sử dụng đúng bản dịch thuật ngữ trong BẢNG THUẬT NGỮ ở trên.\n"
               if glossary_text else "")
            + ("7. NHẤT QUÁN văn phong và thuật ngữ với NGỮ CẢNH DỊCH THUẬT ở trên.\n"
               if context_text else "")
            + math_rule
            + budget_rule
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

    @staticmethod
    def _build_refine_prompt(
        text: str,
        bad_translation: str,
        critique_text: str,
        glossary_text: str = "",
    ) -> str:
        """Prompt cho Refiner — nhận bản dịch xấu + error list từ Critic, yêu cầu sửa đúng chỗ.

        Khác với _build_prompt (dịch từ đầu), prompt này:
        1. Cung cấp bản dịch cũ làm tham chiếu
        2. Liệt kê cụ thể từng lỗi Critic phát hiện
        3. Yêu cầu sửa đúng những lỗi đó, giữ nguyên phần đã đúng
        """
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
    def _extract_text_from_response(response: str) -> str:
        """Extract translated text from Gemini response."""
        if not response:
            return response
        # Try ```text ... ``` block first
        match = re.search(r'```(?:text)?\s*\n(.*?)```', response, re.DOTALL)
        if match:
            text = match.group(1).strip()
        else:
            text = response.strip()

        # E1/E4: Defensively strip "(max ~N chars)", "(table cell, ...)", and
        # "(caption, ...)" hints if Gemini echoed them back. The prompt asks
        # the model not to, but occasionally it does.
        text = re.sub(
            r"\s*\((?:table cell, |caption, )?max\s*~\s*\d+\s*chars?\)\s*",
            " ",
            text,
        )

        # Strip chatbot artifacts and prompt leakage from ALL extracted text
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
            # Detect prompt leakage
            if re.match(
                r'^(===\s*(QUY TẮC|NỘI DUNG CẦN DỊCH|VÍ DỤ)|Dịch các đoạn văn bản sau sang tiếng Việt)',
                s,
            ):
                break
            clean.append(line)
        while clean and not clean[-1].strip():
            clean.pop()
        return "\n".join(clean)

    @staticmethod
    def _is_response_truncated(original: str, translated: str) -> bool:
        """Detect if Gemini response was truncated."""
        if len(original) < 200:
            return False
        if not translated:
            return True
        ratio = len(translated) / len(original)
        return ratio < 0.3

    # ── Glossary ─────────────────────────────────────────────

    async def _extract_initial_glossary(
        self, page, chunks: list, total_chunks: int
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Extract terminology glossary from the first few chunks.

        Uses ~3 chunks (abstract/introduction) to build the initial glossary.
        Returns ``(glossary, fields)`` where `fields` maps en→lĩnh vực (only the
        terms Gemini classified). Returns empty dicts on failure — pipeline
        continues without glossary.
        """
        # Gather sample text from first 3 chunks
        sample_chunks = min(3, total_chunks)
        sample_parts = []
        for i in range(sample_chunks):
            sample_parts.append(chunk_to_text(chunks[i]))
        sample_text = "\n\n".join(sample_parts)

        # Trim to ~4000 chars to keep prompt reasonable
        if len(sample_text) > 4000:
            sample_text = sample_text[:4000]

        print(f"[PdfPipeline] Extracting glossary from first "
              f"{sample_chunks} chunks ({len(sample_text)} chars)...")

        try:
            prompt = build_extraction_prompt(sample_text)
            raw = await self.translator._send_prompt_and_get_response(page, prompt)
            glossary = parse_extraction_response(raw)
            fields = parse_extraction_fields(raw)
            print(f"[PdfPipeline] Glossary extracted: {len(glossary)} terms "
                  f"({len(fields)} có lĩnh vực)")
            if glossary:
                for en, vi in list(glossary.items())[:5]:
                    suffix = f" [{fields[en]}]" if en in fields else ""
                    print(f"  {en} → {vi}{suffix}")
                if len(glossary) > 5:
                    print(f"  ... and {len(glossary) - 5} more")
            return glossary, fields
        except Exception as e:
            print(f"[PdfPipeline] Glossary extraction failed: {e}")
            print("[PdfPipeline] Continuing without glossary")
            return {}, {}

    GLOSSARY_REFRESH_INTERVAL = 10  # Re-extract terms every N chunks

    async def _refresh_glossary(
        self, page, original: str, translated: str, existing: dict[str, str]
    ) -> dict[str, str]:
        """Extract new term pairs from a translated chunk via Gemini.

        Returns only NEW terms (not already in existing glossary).
        """
        try:
            prompt = extract_new_terms_prompt(original, translated)
            raw = await self.translator._send_prompt_and_get_response(page, prompt)
            new_terms = parse_new_terms(raw)
            # Filter out terms already in glossary
            truly_new = {
                k: v for k, v in new_terms.items() if k not in existing
            }
            return truly_new
        except Exception as e:
            print(f"[PdfPipeline] Term extraction failed: {e}")
            return {}

    # ── Quality auto-fix ────────────────────────────────────────

    MAX_FIX_ROUNDS = 2   # Max quality-fix iterations
    MAX_FIX_BLOCKS = 30  # Max blocks to retranslate per round

    async def _fix_quality_issues(
        self, _page, all_blocks: list, glossary: dict, glossary_enabled: bool,
        progress: dict, job_id: str,
    ) -> int:
        """Critic → Refiner feedback loop.

        Thay vì chỉ retranslate blindly, giờ:
        1. Critic phân tích từng block xấu → sinh error list cụ thể
        2. Refiner nhận error list đó trong prompt → sửa đúng chỗ
        3. Lặp tối đa MAX_FIX_ROUNDS vòng

        Dùng LLMCritic nếu Ollama available (phát hiện sâu hơn),
        fallback về HeuristicCritic nếu không.
        """
        from app.pdf.llm_judge import is_available as ollama_available

        # Kiểm tra Ollama 1 lần — nếu có thì dùng LLMCritic cho toàn bộ job
        use_llm = ollama_available()
        llm_model = progress.get("judge_model", "qwen2.5:7b")
        if use_llm:
            print(f"[PdfPipeline] Critic: using LLM ({llm_model}) + heuristic")
        else:
            print("[PdfPipeline] Critic: using heuristic only (Ollama not available)")

        total_fixed = 0
        active_glossary = glossary if glossary_enabled else {}

        for fix_round in range(self.MAX_FIX_ROUNDS):
            fixable = find_fixable_blocks(all_blocks, active_glossary or None)
            if not fixable:
                print(f"[PdfPipeline] Fix round {fix_round + 1}: no fixable blocks")
                break

            fixable = fixable[:self.MAX_FIX_BLOCKS]
            print(f"[PdfPipeline] Fix round {fix_round + 1}/{self.MAX_FIX_ROUNDS}: "
                  f"critiquing {len(fixable)} blocks...")

            # ── Step 1: Critic — sinh error list cho từng block ──────────────
            critiques = critique_blocks(
                fixable,
                glossary=active_glossary or None,
                use_llm=use_llm,
                llm_model=llm_model,
            )
            print(f"[PdfPipeline] Critic found errors in "
                  f"{len(critiques)}/{len(fixable)} blocks")

            progress["status"] = (
                f"critic+refine ({len(critiques)} errors, "
                f"round {fix_round + 1}/{self.MAX_FIX_ROUNDS})"
            )
            self._save_progress(progress)

            # Fresh session for this fix round
            fix_page = await self._ensure_page()
            await self.translator.start_new_chat(fix_page)
            fixed_this_round = 0

            # ── Step 2: Refiner — group → build refine prompt → retranslate ─
            mini_chunks = self._group_blocks_into_mini_chunks(fixable)

            for ci, mini_chunk in enumerate(mini_chunks):
                if self._cancelled:
                    break

                original_text = self._blocks_to_numbered_text(mini_chunk)
                bad_translation = self._blocks_to_numbered_translation(mini_chunk)

                # Lấy critique của từng block trong mini_chunk
                # block_id trong critiques là index trong fixable list
                chunk_start = sum(len(mini_chunks[j]) for j in range(ci))
                chunk_critiques = {
                    (k - chunk_start): v
                    for k, v in critiques.items()
                    if chunk_start <= k < chunk_start + len(mini_chunk)
                }
                critique_text = format_critique_for_prompt(chunk_critiques)

                glossary_text = ""
                if active_glossary:
                    filtered = filter_glossary_for_chunk(active_glossary, original_text)
                    glossary_text = format_glossary_for_prompt(filtered)

                # Nếu có error list từ Critic → dùng refine prompt
                # Nếu không (critic thấy OK nhưng heuristic vẫn đưa vào fixable) → dùng prompt gốc
                if critique_text:
                    prompt = self._build_refine_prompt(
                        original_text, bad_translation, critique_text, glossary_text
                    )
                    print(f"[PdfPipeline] Refine chunk {ci + 1}/{len(mini_chunks)} "
                          f"with critic feedback ({len(chunk_critiques)} blocks with errors)...")
                else:
                    prompt = self._build_prompt(original_text, glossary_text)
                    print(f"[PdfPipeline] Refine chunk {ci + 1}/{len(mini_chunks)} "
                          f"(no critic errors — retranslate fresh)...")

                live_page = await self._ensure_page()
                raw = await self.translator._send_prompt_and_get_response(live_page, prompt)
                translated_text = self._extract_text_from_response(raw)

                if translated_text:
                    fixed = self._apply_fix_translations(translated_text, mini_chunk)
                    fixed_this_round += fixed

                delay = self.settings["delay_between_chunks"]
                if delay > 0 and ci < len(mini_chunks) - 1:
                    await asyncio.sleep(delay)

            total_fixed += fixed_this_round
            print(f"[PdfPipeline] Round {fix_round + 1}: "
                  f"refined {fixed_this_round}/{len(fixable)} blocks")

            if fixed_this_round == 0:
                break

        return total_fixed

    @staticmethod
    def _group_blocks_into_mini_chunks(
        blocks: list, max_chars: int | None = None
    ) -> list[list]:
        """Group blocks into chunks of ~max_chars for retranslation."""
        if max_chars is None:
            from .processor import _DEFAULT_CHUNK_TARGET_SIZE
            max_chars = _DEFAULT_CHUNK_TARGET_SIZE
        chunks = []
        current = []
        current_len = 0

        for b in blocks:
            text_len = len(b.text or "")
            if current and current_len + text_len > max_chars:
                chunks.append(current)
                current = []
                current_len = 0
            current.append(b)
            current_len += text_len

        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _blocks_to_numbered_text(blocks: list) -> str:
        """Convert blocks to numbered text format for translation prompt."""
        parts = []
        for i, b in enumerate(blocks):
            parts.append(f"[{i + 1}] {b.text}")
        return "\n\n".join(parts)

    @staticmethod
    def _blocks_to_numbered_translation(blocks: list) -> str:
        """Convert blocks' existing translations to numbered text for Refiner prompt."""
        parts = []
        for i, b in enumerate(blocks):
            t = (b.translated_text or b.text or "").strip()
            parts.append(f"[{i + 1}] {t}")
        return "\n\n".join(parts)

    @staticmethod
    def _apply_fix_translations(translated_text: str, blocks: list) -> int:
        """Parse fix response and apply to blocks. Returns count of fixed blocks."""
        import re as _re
        # Parse [N] ... patterns
        pattern = _re.compile(r'\[(\d+)\]\s*(.*?)(?=\n\[|\Z)', _re.DOTALL)
        matches = pattern.findall(translated_text)

        fixed = 0
        for num_str, text in matches:
            idx = int(num_str) - 1  # 1-based to 0-based
            if 0 <= idx < len(blocks):
                new_text = text.strip()
                old_text = (blocks[idx].translated_text or "").strip()
                # Only accept if the new translation looks better
                if new_text and new_text != old_text:
                    from app.pdf.quality import _has_vietnamese, _is_likely_untranslated
                    # New text must contain Vietnamese (actual translation)
                    if _has_vietnamese(new_text):
                        blocks[idx].translated_text = new_text
                        fixed += 1

        return fixed

    # ── Retry logic ────────────────────────────────────────────

    def _bump_attempt(self, label: str = "") -> None:
        """Increment in-flight attempt counter and persist it.

        Called each time a prompt is actually sent to Gemini so the status
        endpoint can surface real progress during a long retry / truncation
        loop (otherwise the displayed chunk counter freezes at the last
        *completed* chunk while many prompts fly under the hood).
        """
        if getattr(self, "_progress_ref", None) is None:
            return
        self._current_chunk_attempt = getattr(self, "_current_chunk_attempt", 0) + 1
        self._progress_ref["current_chunk_attempt"] = self._current_chunk_attempt
        if label:
            self._progress_ref["current_chunk_attempt_label"] = label
        try:
            self._save_progress(self._progress_ref)
        except Exception:
            pass

    async def _translate_chunk_with_retry(
        self, prompt: str, original_text: str, chunk_idx: int, total: int
    ) -> str:
        """Translate a chunk with exponential backoff retries.

        Automatically relaunches the browser if it was closed.
        Returns the translated text, or empty string if all retries fail.
        """
        from playwright._impl._errors import TargetClosedError, Error as PlaywrightError

        max_retries = self.settings["max_retries"]
        base_backoff = self.settings["base_backoff"]

        for attempt in range(max_retries + 1):
            try:
                page = await self._ensure_page()
                self._bump_attempt(label=f"attempt {attempt + 1}")
                raw_response = await self.translator._send_prompt_and_get_response(
                    page, prompt
                )
                translated_text = self._extract_text_from_response(raw_response)

                # Truncation detection — retry with new session
                if self._is_response_truncated(original_text, translated_text):
                    print(f"[PdfPipeline] Chunk {chunk_idx + 1}/{total} "
                          f"truncated (attempt {attempt + 1}), rotating session...")
                    page = await self._ensure_page()
                    await self.translator.start_new_chat(page)
                    self._page = page
                    self._bump_attempt(label="retry after truncation")

                    raw_response = await self.translator._send_prompt_and_get_response(
                        page, prompt
                    )
                    translated_text = self._extract_text_from_response(raw_response)

                    if self._is_response_truncated(original_text, translated_text):
                        raise RuntimeError("Response still truncated after session rotation")

                return translated_text

            except (TargetClosedError, PlaywrightError) as e:
                # Browser was closed — mark page as dead and relaunch on next attempt
                print(f"[PdfPipeline] Browser closed during chunk {chunk_idx + 1}: {e}")
                self._page = None
                self._context = None
                if attempt < max_retries:
                    print(f"[PdfPipeline] Relaunching browser in 5s...")
                    await asyncio.sleep(5)
                else:
                    print(f"[PdfPipeline] Chunk {chunk_idx + 1}/{total} failed after browser relaunch attempts")
                    return ""

            except TimeoutError as e:
                # Gemini hung — kill current page entirely, open fresh one before retry
                print(f"[PdfPipeline] Gemini timeout during chunk {chunk_idx + 1}: {e}")
                self._page = None
                if attempt < max_retries:
                    print(f"[PdfPipeline] Opening fresh browser session in 5s...")
                    await asyncio.sleep(5)
                    try:
                        # Force a completely fresh page (not just reload)
                        if self._context:
                            new_page = await self._context.new_page()
                            await self.translator.start_new_chat(new_page)
                            self._page = new_page
                    except Exception:
                        self._page = None
                        self._context = None
                else:
                    print(f"[PdfPipeline] Chunk {chunk_idx + 1}/{total} failed "
                          f"after {max_retries + 1} timeout retries")
                    return ""

            except Exception as e:
                if attempt < max_retries:
                    wait = base_backoff * (2 ** attempt)
                    print(f"[PdfPipeline] Chunk {chunk_idx + 1}/{total} failed "
                          f"(attempt {attempt + 1}/{max_retries + 1}): {e}")
                    print(f"[PdfPipeline] Retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    # Rotate session before retry
                    try:
                        page = await self._ensure_page()
                        await self.translator.start_new_chat(page)
                    except Exception:
                        self._page = None
                else:
                    print(f"[PdfPipeline] Chunk {chunk_idx + 1}/{total} failed "
                          f"after {max_retries + 1} attempts: {e}")
                    return ""

        return ""

    # ── Main pipeline ───────────────────────────────────────────

    async def run(self, pdf_path: str, job_id: str) -> str:
        """Run the full PDF translation pipeline.

        Args:
            pdf_path: Path to the original PDF file.
            job_id: Unique job identifier.

        Returns:
            Path to the translated PDF.
        """
        self._cancelled = False
        job_dir = self._get_job_dir(job_id)
        progress = self._load_progress(job_id)
        # Legacy progress.json files (written by older pipeline versions or
        # interrupted before the first chunk save) may lack these keys.
        # Ensure they exist before any code path mutates them.
        progress.setdefault("translated_chunks", {})
        progress.setdefault("failed_chunks", [])
        # Share progress dict with _translate_chunk_with_retry so it can
        # persist current_chunk_attempt on every prompt send.
        self._progress_ref = progress
        self._current_chunk_attempt = 0

        # ── Audit init ──────────────────────────────────────────
        # Mở audit logger + env snapshot trước mọi thứ khác để bắt
        # mọi event kể cả lỗi sớm.
        self._audit = AuditLogger.open(job_id, job_dir)
        self._audit_token = set_current(self._audit)
        self.translator.audit = self._audit
        self._job_started_at = time.time()
        try:
            write_env_snapshot(job_id, job_dir, extra={
                "pdf_path": pdf_path,
                "mode": self.mode,
                "ai_backend": self.translator._backend_name,
            })
        except Exception:
            pass

        # Reset trackers for this run so error_detail reflects the *current*
        # attempt, not a previous one. A successful completion clears
        # error_detail on save (we drop it below before returning).
        self._current_chunk_idx = -1
        self._current_phase = PHASE_INIT
        progress.pop("error_detail", None)
        return await self._run_inner(pdf_path, job_id, job_dir, progress)

    async def _run_inner(self, pdf_path: str, job_id: str,
                          job_dir: str, progress: dict) -> str:
        """Inner body of run(). Wrapped by run() so a top-level except can
        capture structured error_detail without indenting the entire pipeline.
        """
        try:
            return await self._run_pipeline_body(pdf_path, job_id, job_dir, progress)
        except BaseException as e:
            # Persist structured error so the UI / retry wrapper can show
            # exactly where (phase, chunk_idx) and why (type, traceback) it died.
            self._record_error(progress, e)
            raise

    async def _run_pipeline_body(self, pdf_path: str, job_id: str,
                                  job_dir: str, progress: dict) -> str:

        resuming = bool(progress.get("translated_chunks"))
        self._audit.log(
            "job.started",
            pdf_path=pdf_path,
            mode=self.mode,
            ai_backend=self.translator._backend_name,
            source_type="pdf_only",
            resuming=resuming,
            previous_translated_chunks=len(progress.get("translated_chunks", {})),
            chunks_per_session=self.settings["chunks_per_session"],
            max_retries=self.settings["max_retries"],
        )
        self._enter_phase(PHASE_INIT)

        # Context Memory — RAG store cho translation consistency xuyên session.
        # Style profile load từ progress.json; decisions rebuild từ chunk files
        # trên disk (full text) để TF-IDF retrieval chính xác hơn.
        memory = ContextMemory()
        memory.load_from_progress(progress)
        if progress.get("translated_chunks"):
            self._rebuild_memory_from_disk(memory, job_dir, progress)

        print(f"[PdfPipeline] Starting job {job_id}")
        print(f"[PdfPipeline] PDF: {pdf_path}")

        # ── 1. Extract text blocks ──────────────────────────────
        progress["status"] = "extracting"
        self._save_progress(progress)
        self._enter_phase(PHASE_EXTRACTION)

        print("[PdfPipeline] Extracting text blocks...")
        t0 = time.time()
        all_blocks = extract_text_blocks(pdf_path)
        translatable_count = sum(1 for b in all_blocks if b.is_translatable)
        math_count = sum(1 for b in all_blocks
                          if getattr(b, "block_type", "") == "math"
                          or (not b.is_translatable and not getattr(b, "is_header_footer", False)))
        header_footer_count = sum(1 for b in all_blocks
                                    if getattr(b, "is_header_footer", False))
        print(f"[PdfPipeline] Found {len(all_blocks)} blocks, "
              f"{translatable_count} translatable")

        # Save PDF metadata for duplicate detection
        if "title" not in progress or "page_count" not in progress:
            info = get_pdf_info(pdf_path)
            progress["title"] = info.get("title", "")
            progress["page_count"] = info.get("page_count", 0)
            progress["total_chars"] = info.get("total_chars", 0)
            self._save_progress(progress)

        # Audit: extraction kết quả + page metadata
        self._audit.log(
            "pdf.extraction_done",
            duration_seconds=round(time.time() - t0, 3),
            total_blocks=len(all_blocks),
            translatable_blocks=translatable_count,
            math_blocks=math_count,
            header_footer_blocks=header_footer_count,
            title=progress.get("title", ""),
            page_count=progress.get("page_count", 0),
            total_chars=progress.get("total_chars", 0),
        )

        if translatable_count == 0:
            progress["status"] = "error: No translatable text found in PDF"
            self._save_progress(progress)
            self._audit.log("error.unexpected", reason="no_translatable_text",
                            message="PDF has no translatable text blocks")
            self._finalize_audit(status="error", error="no_translatable_text")
            raise RuntimeError("No translatable text found in PDF")

        # ── 2. Chunk for translation ────────────────────────────
        self._enter_phase(PHASE_CHUNKING)
        from .processor import _DEFAULT_CHUNK_TARGET_SIZE as _chunk_target_size
        chunks = split_blocks_into_chunks(all_blocks)
        total_chunks = len(chunks)
        print(f"[PdfPipeline] Split into {total_chunks} chunks "
              f"(target_size={_chunk_target_size} chars)")

        # Audit: chunk boundaries (1 event tóm tắt + per-chunk decisions)
        if total_chunks > 0:
            chunk_sizes = [sum(len(b.text or "") for b in c) for c in chunks]
            self._audit.log(
                "chunks.split_done",
                total_chunks=total_chunks,
                total_chars=sum(chunk_sizes),
                avg_chunk_chars=round(sum(chunk_sizes) / len(chunk_sizes), 1),
                min_chunk_chars=min(chunk_sizes),
                max_chunk_chars=max(chunk_sizes),
                target_size=_chunk_target_size,
            )
            for ci, chunk in enumerate(chunks):
                self._audit.log(
                    "decision.chunk_boundary",
                    chunk_idx=ci,
                    block_count=len(chunk),
                    char_count=chunk_sizes[ci],
                    first_block_idx=int(getattr(chunk[0], "block_idx", 0)) if chunk else -1,
                    last_block_idx=int(getattr(chunk[-1], "block_idx", 0)) if chunk else -1,
                    reason=f"char_budget_{_chunk_target_size}",
                )

        # Persist a chunk → block bbox map so the frontend overlay can deep-link
        # from a clicked PDF region back to its chunk in HistoryEditor. Saved
        # only on first build; resume reuses the existing map.
        if "chunk_block_map" not in progress:
            try:
                page_sizes = self._collect_page_sizes(pdf_path)
                progress["chunk_block_map"] = self._build_chunk_block_map(chunks, page_sizes)
                self._save_progress(progress)
            except Exception as e:
                print(f"[PdfPipeline] chunk_block_map build failed (non-fatal): {e}")

        # ── 3. Launch browser & translate ───────────────────────
        already_translated = len(progress.get("translated_chunks", {}))
        progress["status"] = f"translating {already_translated}/{total_chunks}"
        progress["mode"] = self.mode
        progress["ai_backend"] = self.translator._backend_name
        progress["failed_chunks"] = progress.get("failed_chunks", [])
        self._save_progress(progress)

        chunks_per_session = self.settings["chunks_per_session"]
        delay_between = self.settings["delay_between_chunks"]

        print(f"[PdfPipeline] Mode: {self.mode} "
              f"(session rotation every {chunks_per_session} chunks, "
              f"{delay_between}s delay between chunks)")
        print(f"[PdfPipeline] NOTE: Browser window will open — do NOT close it while translating.")

        try:
            # ── 3a. Glossary extraction (only for new jobs, skip on resume) ──
            self._enter_phase(PHASE_GLOSSARY)
            glossary = progress.get("glossary", {}).get("terms", {})
            glossary_enabled = progress.get("glossary", {}).get("enabled", True)
            locked_terms = progress.get("glossary", {}).get("locked", [])

            if not glossary and already_translated == 0:
                # ── Pre-seed glossary from global store (cross-document) ──
                try:
                    from app.database import get_global_glossary
                    global_glossary = get_global_glossary(min_confidence=0.6, min_frequency=2)
                    if global_glossary:
                        glossary = dict(global_glossary)
                        print(f"[PdfPipeline] Seeded {len(glossary)} terms from global glossary")
                        self._audit.log(
                            "glossary.merged_global",
                            terms_added=len(glossary),
                            min_confidence=0.6,
                            min_frequency=2,
                        )
                except Exception as e:
                    print(f"[PdfPipeline] Global glossary seed failed (non-fatal): {e}")
                    self._audit.log("glossary.global_seed_failed",
                                    error=str(e)[:200])
                    glossary = {}

                progress["status"] = "extracting glossary (phase 1/3)..."
                self._save_progress(progress)
                print("[PdfPipeline] Phase 1/3: Extracting glossary...")
                self._audit.log("glossary.extraction_started",
                                sample_chunks=min(3, total_chunks),
                                seeded_from_global=len(glossary))
                t_gloss = time.time()
                page = await self._ensure_page()
                doc_glossary, doc_fields = await self._extract_initial_glossary(
                    page, chunks, total_chunks
                )
                self._audit.log(
                    "glossary.extraction_done",
                    duration_seconds=round(time.time() - t_gloss, 3),
                    new_terms=len(doc_glossary),
                    fields_classified=len(doc_fields),
                    sample_terms=dict(list(doc_glossary.items())[:5]),
                )
                if doc_glossary:
                    # Merge doc-specific terms on top of global seed
                    # (doc terms take precedence for this job)
                    glossary = merge_glossary(glossary, doc_glossary)
                    progress["glossary"] = {"terms": glossary, "enabled": True,
                                            "locked": locked_terms, "fields": doc_fields}
                    self._save_progress(progress)
                    # Fresh session before translation
                    await self.translator.start_new_chat(self._page)
                elif glossary:
                    # Only global seed, no doc-specific extraction
                    progress["glossary"] = {"terms": glossary, "enabled": True, "locked": locked_terms}
                    self._save_progress(progress)
            elif glossary:
                print(f"[PdfPipeline] Loaded glossary from progress: {len(glossary)} terms")
            else:
                print("[PdfPipeline] Resuming — skipping glossary extraction")

            # ── 3b. HITL gate: pause for human glossary review ──
            # User reviews/edits/locks terms via GlossaryEditor, then POSTs
            # /approve-glossary which sets approved=True and restarts this
            # subprocess. On the second run, glossary is non-empty and
            # already_translated stays 0, so we skip the gate via the
            # `approved` flag and fall through to translation.
            gloss_state = progress.get("glossary", {}) or {}
            if (already_translated == 0
                    and gloss_state.get("terms")
                    and not gloss_state.get("approved", False)):
                gloss_state["awaiting_review"] = True
                progress["glossary"] = gloss_state
                progress["status"] = "awaiting_glossary_review"
                self._save_progress(progress)
                print(f"[PdfPipeline] Awaiting glossary review — "
                      f"{len(gloss_state['terms'])} terms ready. "
                      f"Pipeline will resume when user approves.")
                self._audit.log(
                    "job.paused_for_review",
                    reason="awaiting_glossary_review",
                    glossary_terms=len(gloss_state["terms"]),
                )
                try:
                    await self.translator.cleanup()
                except Exception as e:
                    print(f"[PdfPipeline] Browser cleanup at pause failed (non-fatal): {e}")
                self._page = None
                self._finalize_audit(status="paused_for_review",
                                     glossary_terms=len(gloss_state["terms"]))
                return ""

            chunks_since_new_chat = 0
            progress["status"] = f"translating {already_translated}/{total_chunks}"
            self._save_progress(progress)
            print(f"[PdfPipeline] Phase 2/3: Translating {total_chunks} chunks...")
            self._enter_phase(PHASE_TRANSLATING)
            self._audit.log(
                "translation.loop_started",
                total_chunks=total_chunks,
                already_translated=already_translated,
                chunks_per_session=chunks_per_session,
                delay_between_chunks=delay_between,
            )

            for chunk_idx, chunk in enumerate(chunks):
                # Record position for error reporting + precise resume.
                # If we crash mid-chunk, _record_error will report
                # chunk_idx_at_error = chunk_idx (1-based in the human msg).
                self._current_chunk_idx = chunk_idx
                progress["last_attempted_chunk_idx"] = chunk_idx
                # Persist immediately so the status poller can show the
                # in-flight chunk — without this write, the displayed
                # counter would freeze at the last *completed* chunk during
                # a long retry / truncation loop that may send many prompts.
                self._current_chunk_attempt = 0
                progress["current_chunk_attempt"] = 0
                self._save_progress(progress)
                if self._cancelled:
                    progress["status"] = "cancelled"
                    self._save_progress(progress)
                    print("[PdfPipeline] Cancelled by user")
                    self._audit.log("job.cancelled", at_chunk=chunk_idx,
                                    total_chunks=total_chunks)
                    return ""

                chunk_key = str(chunk_idx)

                # Skip already translated chunks (resume support)
                if chunk_key in progress.get("translated_chunks", {}):
                    print(f"[PdfPipeline] Chunk {chunk_idx + 1}/{total_chunks} "
                          f"— already done, skipping")
                    saved = progress["translated_chunks"][chunk_key]
                    parse_translated_chunk(saved, chunk)
                    self._audit.log("chunk.skipped_resume", chunk_idx=chunk_idx)
                    continue

                # MIX mode: swap backend at document midpoint (no-op otherwise)
                swapped_before = self._mix_swapped
                await self._maybe_swap_backend(chunk_idx, total_chunks, progress)
                if self._mix_swapped and not swapped_before:
                    chunks_since_new_chat = 0

                # Session rotation
                if chunks_since_new_chat >= chunks_per_session:
                    print("[PdfPipeline] Rotating Gemini session...")
                    self._audit.log(
                        "session.rotated",
                        reason="chunks_per_session_limit",
                        chunks_in_session=chunks_since_new_chat,
                        at_chunk=chunk_idx,
                    )
                    try:
                        page = await self._ensure_page()
                        await self.translator.start_new_chat(page)
                    except Exception as e:
                        print(f"[PdfPipeline] Session rotation failed: {e}")
                        self._audit.log("session.rotation_failed",
                                        at_chunk=chunk_idx,
                                        error=str(e)[:200])
                        self._page = None  # Force relaunch on next _ensure_page
                    chunks_since_new_chat = 0

                # Protect math across the chunk with a shared placeholder
                # counter; restore source text in `finally` so retries don't
                # see corrupted placeholders.
                originals_text, math_protector = protect_chunk_math(chunk)
                protected_count = math_protector.protected_count
                try:
                    # E1: prompt-side text carries per-block length budgets
                    # so Gemini can compress proactively. Restored sources
                    # (no budget prefix) are used for glossary/memory match
                    # and for any downstream code that expects clean text.
                    original_text = chunk_to_text(chunk)
                    budgeted_text = chunk_to_text_with_budget(chunk)
                    # Use unprotected text for glossary/memory matching —
                    # placeholders break case-insensitive substring search.
                    unprotected_for_match = "\n\n".join(
                        f"[{i + 1}] {orig}" for i, orig in enumerate(originals_text)
                    )

                    glossary_text = ""
                    num_terms = 0
                    filtered_terms: dict[str, str] = {}
                    if glossary and glossary_enabled:
                        filtered_terms = filter_glossary_for_chunk(
                            glossary, unprotected_for_match, locked=locked_terms
                        )
                        glossary_text = format_glossary_for_prompt(filtered_terms, locked=locked_terms)
                        num_terms = len(filtered_terms)

                    # Retrieve translation memory context
                    context_text = memory.retrieve_context(unprotected_for_match)
                    context_size = memory.size

                    prompt = self._build_prompt(
                        budgeted_text, glossary_text, context_text,
                        math_protected=protected_count > 0,
                        has_length_budget=True,
                    )

                    ctx_info = f" (memory: {context_size})" if context_size else ""
                    gloss_info = f" (glossary: {num_terms} terms)" if num_terms else ""
                    math_info = f" (math: {protected_count})" if protected_count else ""
                    print(f"[PdfPipeline] Translating chunk "
                          f"{chunk_idx + 1}/{total_chunks}"
                          f"{gloss_info}{ctx_info}{math_info}...")

                    # Audit: chunk.sent — bằng chứng quan trọng nhất
                    prompt_path = self._audit.save_raw_prompt(chunk_idx, 1, prompt)
                    t_chunk = time.time()
                    self._audit.log(
                        "chunk.sent",
                        chunk_idx=chunk_idx,
                        total=total_chunks,
                        session_chunk_idx=chunks_since_new_chat,
                        original_chars=len(original_text),
                        prompt_chars=len(prompt),
                        glossary_terms=num_terms,
                        context_memory_size=context_size,
                        math_protected=protected_count,
                        prompt_path=prompt_path,
                    )
                    if protected_count > 0:
                        self._audit.log(
                            "decision.math_placeholder",
                            chunk_idx=chunk_idx,
                            placeholder_count=protected_count,
                        )

                    translated_text = await self._translate_chunk_with_retry(
                        prompt, original_text, chunk_idx, total_chunks
                    )

                    # Audit: chunk.received
                    raw_path = self._audit.save_raw_response(
                        chunk_idx, 1, translated_text or ""
                    )
                    latency_ms = round((time.time() - t_chunk) * 1000)
                    self._audit.log(
                        "chunk.received",
                        chunk_idx=chunk_idx,
                        success=bool(translated_text),
                        translated_chars=len(translated_text or ""),
                        original_chars=len(original_text),
                        length_ratio=round(
                            len(translated_text or "") / max(1, len(original_text)), 3
                        ),
                        latency_ms=latency_ms,
                        raw_response_path=raw_path,
                    )
                finally:
                    # Always restore source block.text
                    for i, orig in enumerate(originals_text):
                        if i < len(chunk):
                            chunk[i].text = orig

                # Restore math placeholders in the LLM response
                if translated_text and protected_count > 0:
                    translated_text = math_protector.restore(translated_text)

                # Recompute original_text from restored sources for downstream
                # use (memory.add, glossary refresh, progress save).
                original_text = chunk_to_text(chunk)

                if not translated_text:
                    # Track failed chunks but continue
                    if chunk_idx not in progress["failed_chunks"]:
                        progress["failed_chunks"].append(chunk_idx)
                    print(f"[PdfPipeline] Chunk {chunk_idx + 1}/{total_chunks} "
                          f"FAILED — skipping")
                    self._audit.log(
                        "chunk.failed",
                        chunk_idx=chunk_idx,
                        original_chars=len(original_text),
                        reason="all_retries_exhausted",
                    )

                # Parse and assign translations to blocks
                parse_translated_chunk(translated_text, chunk)

                # Compression safety check — flag blocks where the
                # translation is suspiciously short relative to source.
                # The prompt explicitly tells Gemini to drop hư từ but
                # protect technical content; a ratio < 0.6 on a
                # substantial block suggests the model went too far.
                if translated_text:
                    suspect_blocks = []
                    for b in chunk:
                        en_len = len(b.text or "")
                        vi_len = len(b.translated_text or "")
                        if en_len > 40 and vi_len > 0:
                            ratio = vi_len / en_len
                            if ratio < 0.6:
                                suspect_blocks.append({
                                    "block_idx": getattr(b, "block_idx", -1),
                                    "page": getattr(b, "page_num", -1),
                                    "en_len": en_len,
                                    "vi_len": vi_len,
                                    "ratio": round(ratio, 2),
                                    "en_preview": (b.text or "")[:120],
                                    "vi_preview": (b.translated_text or "")[:120],
                                })
                    if suspect_blocks:
                        self._audit.log(
                            "chunk.compression_suspect",
                            chunk_idx=chunk_idx,
                            suspect_count=len(suspect_blocks),
                            total_blocks=len(chunk),
                            suspects=suspect_blocks[:10],
                        )
                        print(f"[PdfPipeline] Chunk {chunk_idx + 1}: "
                              f"{len(suspect_blocks)} block(s) có ratio VI/EN < 0.6 "
                              f"— có thể bị nén quá mức")

                # Periodically extract new glossary terms via Gemini
                if (translated_text and glossary_enabled
                        and chunk_idx > 0
                        and chunk_idx % self.GLOSSARY_REFRESH_INTERVAL == 0):
                    print(f"[PdfPipeline] Refreshing glossary at chunk {chunk_idx + 1}...")
                    try:
                        page = await self._ensure_page()
                        new_terms = await self._refresh_glossary(
                            page, original_text, translated_text, glossary
                        )
                        if new_terms:
                            glossary = merge_glossary(glossary, new_terms)
                            progress["glossary"]["terms"] = glossary
                            print(f"[PdfPipeline] Glossary: +{len(new_terms)} terms "
                                  f"(total: {len(glossary)})")
                            self._audit.log(
                                "glossary.refresh",
                                at_chunk=chunk_idx,
                                new_terms=len(new_terms),
                                total_terms=len(glossary),
                                sample_new=dict(list(new_terms.items())[:3]),
                            )
                            await self.translator.start_new_chat(self._page)
                            chunks_since_new_chat = 0
                            self._audit.log(
                                "session.rotated",
                                reason="post_glossary_refresh",
                                chunks_in_session=0,
                                at_chunk=chunk_idx,
                            )
                    except Exception as e:
                        print(f"[PdfPipeline] Glossary refresh failed (non-fatal): {e}")
                        self._audit.log("glossary.refresh_failed",
                                        at_chunk=chunk_idx,
                                        error=str(e)[:200])

                # Add to context memory (chỉ khi dịch thành công)
                if translated_text:
                    try:
                        memory.add(
                            chunk_index=chunk_idx,
                            original=original_text,
                            translated=translated_text,
                            key_terms=filtered_terms,
                        )
                    except Exception as e:
                        print(f"[PdfPipeline] ContextMemory.add failed (non-fatal): {e}")

                # Save progress
                self._save_chunk(job_id, chunk_idx, original_text, translated_text)
                progress["translated_chunks"][chunk_key] = translated_text
                progress["status"] = f"translating {chunk_idx + 1}/{total_chunks}"
                # Clear in-flight attempt counter so the UI label drops back
                # to "completed N/total" until the next chunk starts.
                progress["current_chunk_attempt"] = 0
                progress.pop("current_chunk_attempt_label", None)
                self._current_chunk_attempt = 0
                memory.save_to_progress(progress)
                self._save_progress(progress)

                chunks_since_new_chat += 1

                # Inter-chunk delay (avoid rate limiting on long docs)
                if chunk_idx < total_chunks - 1 and delay_between > 0:
                    await asyncio.sleep(delay_between)

            # ── 3b. Quality auto-fix ──────────────────────────
            if not self._cancelled:
                print("[PdfPipeline] Phase 3/3: Quality auto-fix check...")
                self._enter_phase(PHASE_QUALITY_FIX)
                pre_report = check_translation_quality(all_blocks)
                fixable_count = len(find_fixable_blocks(all_blocks))
                print(f"[PdfPipeline] Pre-check score: {pre_report.score:.1f}/100, "
                      f"{fixable_count} fixable blocks")
                self._audit.log(
                    "quality.pre_check",
                    score=round(pre_report.score, 2),
                    issue_count=len(pre_report.issues),
                    fixable_count=fixable_count,
                )

                # Close the browser eagerly when there's nothing left to fix —
                # otherwise Chrome stays open while we rebuild the PDF, which
                # leaves the user staring at a "done" progress bar with a live
                # Chromium still on screen. The `finally` cleanup below is a
                # safety net so the idempotent close here is harmless.
                if fixable_count == 0:
                    try:
                        await self.translator.cleanup()
                        self._page = None
                        self._context = None
                        print("[PdfPipeline] Browser closed (no quality fix needed)")
                    except Exception as e:
                        print(f"[PdfPipeline] Early browser cleanup failed "
                              f"(non-fatal): {e}")

                if fixable_count > 0:
                    page = await self._ensure_page()
                    fixed = await self._fix_quality_issues(
                        page, all_blocks, glossary, glossary_enabled,
                        progress, job_id,
                    )
                    self._audit.log("quality.fix_done",
                                    fixable_count=fixable_count,
                                    fixed_count=fixed)
                    if fixed > 0:
                        print(f"[PdfPipeline] Quality fix: improved {fixed} blocks")
                        for chunk_idx, chunk in enumerate(chunks):
                            chunk_key = str(chunk_idx)
                            parts = []
                            for i, b in enumerate(chunk):
                                if b.translated_text:
                                    parts.append(f"[{i + 1}] {b.translated_text}")
                                elif b.text:
                                    parts.append(f"[{i + 1}] {b.text}")
                            progress["translated_chunks"][chunk_key] = "\n\n".join(parts)
                        self._save_progress(progress)

        finally:
            await self.translator.cleanup()
            self._page = None
            self._context = None

        if self._cancelled:
            self._finalize_audit(status="cancelled")
            return ""

        # ── 4. Rebuild PDF ──────────────────────────────────────
        print("[PdfPipeline] Rebuilding PDF...")
        progress["status"] = "compiling"
        self._save_progress(progress)
        self._enter_phase(PHASE_REBUILDING)

        output_dir = os.path.join(job_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "translated.pdf")

        # Stamp provenance: PDF metadata (Tier 1) + footer on every page (Tier 2).
        try:
            acct_info = self.translator.get_account_info()
        except Exception as e:
            print(f"[PdfPipeline] get_account_info() failed (non-fatal): {e}")
            acct_info = {"backend": self.translator._backend_name, "account_email": ""}
        translation_meta = build_meta(
            job_id=job_id,
            source_kind="pdf_upload",
            source_label=os.path.basename(pdf_path),
            source_url="",
            translator_backend=acct_info.get("backend", self.translator._backend_name),
            account_email=acct_info.get("account_email", ""),
            title=progress.get("title", ""),
        )
        progress["translation_meta"] = translation_meta
        self._save_progress(progress)

        t_rebuild = time.time()
        self._audit.log(
            "pdf.rebuild_started",
            output_path=output_path,
            total_blocks=len(all_blocks),
            translatable_blocks=sum(1 for b in all_blocks if b.is_translatable),
            translation_meta_backend=translation_meta["translator_backend"],
            translation_meta_account=translation_meta["account_email"] or "default",
        )
        try:
            engine = os.environ.get("PDF_REBUILD_ENGINE", "inplace").lower()
            if engine == "typst":
                from app.pdf.typst_pipeline import rebuild_pdf_typst
                rebuild_pdf_typst(pdf_path, all_blocks, output_path, translation_meta=translation_meta)
            else:
                rebuild_pdf_inplace(pdf_path, all_blocks, output_path, translation_meta=translation_meta)
            rebuild_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            self._audit.log(
                "pdf.rebuild_done",
                duration_seconds=round(time.time() - t_rebuild, 3),
                output_size_bytes=rebuild_size,
                output_exists=os.path.exists(output_path),
            )
        except Exception as e:
            self._audit.log(
                "pdf.rebuild_failed",
                duration_seconds=round(time.time() - t_rebuild, 3),
                error=str(e)[:500],
                error_type=type(e).__name__,
            )
            self._finalize_audit(status="error", error=str(e)[:200])
            raise

        # ── 5. Quality check ────────────────────────────────────
        print("[PdfPipeline] Running quality check...")
        self._enter_phase(PHASE_QUALITY)
        glossary_terms = progress.get("glossary", {}).get("terms", {})
        t_q = time.time()
        quality = check_translation_quality(all_blocks, glossary_terms or None)
        progress["quality"] = quality.to_dict()
        print(f"[PdfPipeline] Quality score: {quality.score:.1f}/100 "
              f"({len(quality.issues)} issues)")
        self._audit.log(
            "quality.heuristic_run",
            duration_seconds=round(time.time() - t_q, 3),
            score=round(quality.score, 2),
            untranslated_blocks=quality.untranslated_blocks,
            issue_count=len(quality.issues),
            issue_severities={
                "error": sum(1 for i in quality.issues if getattr(i, "severity", "") == "error"),
                "warning": sum(1 for i in quality.issues if getattr(i, "severity", "") == "warning"),
                "info": sum(1 for i in quality.issues if getattr(i, "severity", "") == "info"),
            },
            glossary_terms=len(glossary_terms or {}),
        )

        # ── 5c. Auto-diagnostics ─────────────────────────────────
        print("[PdfPipeline] Running auto-diagnostics...")
        t_d = time.time()
        try:
            diag_report = run_diagnostics(job_id, job_dir, progress)
            progress["diagnostics"] = diag_report.to_dict()
            if diag_report.primary_cause:
                print(f"[PdfPipeline] Primary cause: {diag_report.primary_cause} "
                      f"(severity={diag_report.overall_severity}, "
                      f"findings={len(diag_report.findings)})")
            self._audit.log(
                "quality.diagnostics_run",
                duration_seconds=round(time.time() - t_d, 3),
                primary_cause=diag_report.primary_cause or "",
                overall_severity=diag_report.overall_severity or "",
                findings_count=len(diag_report.findings),
                summary=(diag_report.summary or "")[:500],
            )
        except Exception as e:
            print(f"[PdfPipeline] Diagnostics failed (non-critical): {e}")
            progress["diagnostics"] = {"error": str(e)}
            self._audit.log("quality.diagnostics_failed",
                            duration_seconds=round(time.time() - t_d, 3),
                            error=str(e)[:200])

        # ── 5c-bis. Multi-agent agreement (Contribution 3) ───────
        # Opt-in via ENABLE_MULTI_AGENT=1 because each run hits Ollama
        # (5-15s/chunk × max_chunks). Skipped silently if disabled or if
        # Ollama isn't reachable — never blocks pipeline completion.
        if os.getenv("ENABLE_MULTI_AGENT", "0") == "1":
            print("[PdfPipeline] Running multi-agent agreement check...")
            t_ma = time.time()
            try:
                from app.pdf.multi_agent import (
                    run_multi_agent_evaluation, is_available as multi_agent_available,
                )
                model = os.getenv("MULTI_AGENT_MODEL", "qwen2.5:7b")
                max_chunks = int(os.getenv("MULTI_AGENT_MAX_CHUNKS", "10"))
                self._audit.log(
                    "quality.multi_agent_started",
                    arbiter_model=model,
                    max_chunks=max_chunks,
                )
                if multi_agent_available(model):
                    ma_report = run_multi_agent_evaluation(
                        job_dir,
                        arbiter_model=model,
                        max_chunks=max_chunks,
                        run_synthesis=False,   # auto-run: comparison only, no rewrite
                    )
                    progress["multi_agent"] = ma_report.to_dict()
                    print(f"[PdfPipeline] Multi-agent: {ma_report.num_segments} segments, "
                          f"mean agreement={ma_report.mean_agreement:.1f}, "
                          f"{ma_report.high_agreement_count} consensus")
                    self._audit.log(
                        "quality.multi_agent_run",
                        duration_seconds=round(time.time() - t_ma, 3),
                        arbiter_model=model,
                        num_segments=ma_report.num_segments,
                        mean_agreement=round(ma_report.mean_agreement, 2),
                        high_agreement_count=ma_report.high_agreement_count,
                    )
                else:
                    progress["multi_agent"] = {
                        "available": False,
                        "error": f"Ollama/{model} not available",
                    }
                    print(f"[PdfPipeline] Multi-agent skipped: {model} not available")
                    self._audit.log("quality.multi_agent_skipped",
                                    arbiter_model=model,
                                    reason="ollama_or_model_unavailable")
            except Exception as e:
                print(f"[PdfPipeline] Multi-agent failed (non-critical): {e}")
                progress["multi_agent"] = {"error": str(e)}
                self._audit.log("quality.multi_agent_failed",
                                duration_seconds=round(time.time() - t_ma, 3),
                                error=str(e)[:200])

        # ── 5d. Merge job glossary into global store ─────────────
        final_glossary = progress.get("glossary", {}).get("terms", {})
        final_fields = progress.get("glossary", {}).get("fields", {})
        if final_glossary:
            try:
                from app.database import merge_job_glossary_to_global
                merge_job_glossary_to_global(job_id, final_glossary, fields=final_fields)
                print(f"[PdfPipeline] Merged {len(final_glossary)} terms into global glossary")
                self._audit.log("glossary.merged_to_global",
                                terms_count=len(final_glossary),
                                fields_count=len(final_fields or {}))
            except Exception as e:
                print(f"[PdfPipeline] Global glossary merge failed (non-critical): {e}")
                self._audit.log("glossary.merge_to_global_failed",
                                error=str(e)[:200])

        # ── 6. Validate ─────────────────────────────────────────
        self._enter_phase(PHASE_VALIDATION)
        validation = self._validate_output(pdf_path, output_path)
        failed = progress.get("failed_chunks", [])
        if failed:
            validation["status"] = "warning"
            validation["warnings"].append(
                f"{len(failed)} chunk(s) failed to translate: {failed}"
            )
        if quality.score < 70:
            validation["status"] = "warning"
            validation["warnings"].append(
                f"Quality score low: {quality.score:.1f}/100 "
                f"({quality.untranslated_blocks} untranslated blocks)"
            )
        progress["validation"] = validation
        self._audit.log(
            "pdf.validation",
            status=validation["status"],
            original_pages=validation.get("original_pages", 0),
            translated_pages=validation.get("translated_pages", 0),
            warnings_count=len(validation.get("warnings", [])),
            warnings=validation.get("warnings", [])[:10],
            failed_chunks_count=len(failed),
        )

        if validation["status"] == "warning":
            progress["status"] = "done_with_warnings"
        else:
            progress["status"] = "done"

        # Clear any stale error_detail from a previous failed attempt now
        # that we've reached completion. Keeps the UI's diagnostics surface
        # honest — no ghost errors lingering on a successful re-run.
        progress.pop("error_detail", None)
        self._save_progress(progress)
        print(f"[PdfPipeline] Done! Output: {output_path}")

        self._finalize_audit(
            status=progress["status"],
            output_path=output_path,
            quality_score=round(quality.score, 2),
            validation_status=validation["status"],
            failed_chunks=len(failed),
        )
        return output_path

    @staticmethod
    def _validate_output(original_pdf: str, translated_pdf: str) -> dict:
        """Compare original and translated PDFs."""
        orig_info = get_pdf_info(original_pdf)
        trans_info = get_pdf_info(translated_pdf)

        result = {
            "status": "ok",
            "original_pages": orig_info["page_count"],
            "translated_pages": trans_info["page_count"],
            "warnings": [],
        }

        # Page counts should match exactly for PDF overlay
        if trans_info["page_count"] != orig_info["page_count"]:
            result["status"] = "warning"
            result["warnings"].append(
                f"Page count mismatch: original {orig_info['page_count']}, "
                f"translated {trans_info['page_count']}"
            )

        orig_size = os.path.getsize(original_pdf)
        trans_size = os.path.getsize(translated_pdf)
        if trans_size < orig_size * 0.3:
            result["status"] = "warning"
            result["warnings"].append(
                f"Translated PDF is much smaller than original "
                f"({trans_size} vs {orig_size} bytes)"
            )

        return result
