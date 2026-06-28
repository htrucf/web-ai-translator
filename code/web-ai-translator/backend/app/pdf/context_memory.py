"""Context Memory — Bộ nhớ ngữ cảnh xuyên phiên dịch (RAG nội bộ).

Vấn đề: Pipeline chia paper thành nhiều session nhỏ (do giới hạn context
của Web UI). Session thứ 10 không biết gì về "quyết định dịch thuật" từ
session đầu tiên → mất nhất quán văn phong, thuật ngữ.

Giải pháp: Mỗi chunk đã dịch xong → lưu cặp (original, translated) vào
vector store. Khi dịch chunk mới, truy xuất top-K chunks tương tự nhất
→ inject vào prompt làm "translation memory" để AI dịch nhất quán.

Kiến trúc:
  ┌──────────┐   dịch xong    ┌──────────────┐
  │ Chunk N  │ ──────────────→│ Vector Store  │
  └──────────┘                │  (TF-IDF +   │
                              │   cosine sim) │
  ┌──────────┐   truy xuất    │              │
  │ Chunk N+1│ ──────────────→│ top-K giống  │
  └──────────┘                └──────┬───────┘
                                     │
                              ┌──────▼───────┐
                              │ Context text │
                              │ inject vào   │
                              │   prompt     │
                              └──────────────┘

Cân bằng context vs token limit:
  - Tối đa 3 chunks tham chiếu (~300-500 chars mỗi chunk)
  - Tổng context section ≤ 1500 chars
  - Chỉ lấy chunks có similarity > 0.15 (tránh noise)
  - Ưu tiên chunks GẦN nhất nếu similarity ngang nhau

Không yêu cầu dependencies ngoài (dùng sklearn TfidfVectorizer nếu có,
fallback sang numpy TF-IDF tự cài nếu không có sklearn).
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ── Configuration ─────────────────────────────────────────────────────────────

MAX_CONTEXT_CHUNKS = 3       # Tối đa chunks tham chiếu inject vào prompt
MAX_CONTEXT_CHARS = 1500     # Tổng chars của context section
MIN_SIMILARITY = 0.15        # Ngưỡng tối thiểu để coi là "tương tự"
SUMMARY_MAX_CHARS = 200      # Tối đa chars cho mỗi chunk summary
RECENCY_BONUS = 0.05         # Bonus cho chunks gần nhất (mỗi bước gần thêm)


# ── Translation Decision (đơn vị lưu trữ) ────────────────────────────────────

@dataclass
class TranslationDecision:
    """Một "quyết định dịch thuật" — lưu cặp original/translated + metadata."""
    chunk_index: int
    original: str              # Text gốc tiếng Anh
    translated: str            # Bản dịch tiếng Việt
    key_terms: dict[str, str] = field(default_factory=dict)  # Terms dùng trong chunk này
    style_notes: str = ""      # Ghi chú văn phong (vd: "dùng 'chúng tôi' thay 'chúng ta'")

    def summary(self) -> str:
        """Tóm tắt ngắn gọn để inject vào prompt."""
        # Lấy 2-3 câu đầu của bản dịch
        sentences = re.split(r'[.!?]\s+', self.translated)
        summary = '. '.join(sentences[:2])
        if len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[:SUMMARY_MAX_CHARS].rsplit(' ', 1)[0] + '...'
        return summary


# ── TF-IDF Vectorizer (lightweight, không cần sklearn) ────────────────────────

class _TfidfVectorizer:
    """TF-IDF vectorizer đơn giản dùng numpy — không cần sklearn.

    Đủ tốt cho vài trăm documents ngắn (chunks trong 1 paper).
    """

    def __init__(self):
        self._vocab: dict[str, int] = {}
        self._idf: Optional[np.ndarray] = None
        self._fitted = False

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Tách từ đơn giản — lowercase, chỉ giữ alphanumeric."""
        return re.findall(r'[a-z0-9]+', text.lower())

    def fit(self, documents: list[str]):
        """Build vocabulary + IDF từ tập documents."""
        # Build vocab
        vocab = {}
        for doc in documents:
            for token in set(self._tokenize(doc)):
                if token not in vocab:
                    vocab[token] = len(vocab)
        self._vocab = vocab
        V = len(vocab)
        if V == 0:
            self._idf = np.array([])
            self._fitted = True
            return

        # Compute IDF
        N = len(documents)
        df = np.zeros(V)
        for doc in documents:
            tokens = set(self._tokenize(doc))
            for token in tokens:
                if token in self._vocab:
                    df[self._vocab[token]] += 1

        # IDF = log((N+1) / (df+1)) + 1  (smooth IDF)
        self._idf = np.log((N + 1) / (df + 1)) + 1
        self._fitted = True

    def transform(self, text: str) -> np.ndarray:
        """Chuyển text thành TF-IDF vector."""
        V = len(self._vocab)
        if V == 0 or not self._fitted:
            return np.zeros(1)

        tokens = self._tokenize(text)
        if not tokens:
            return np.zeros(V)

        # TF (term frequency)
        tf = np.zeros(V)
        for token in tokens:
            idx = self._vocab.get(token)
            if idx is not None:
                tf[idx] += 1
        # Normalize TF
        tf = tf / len(tokens) if tokens else tf

        # TF-IDF
        tfidf = tf * self._idf

        # L2 normalize
        norm = np.linalg.norm(tfidf)
        if norm > 0:
            tfidf = tfidf / norm

        return tfidf


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity giữa 2 vectors (đã normalize thì = dot product)."""
    if a.shape != b.shape or np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
        return 0.0
    return float(np.dot(a, b))


# ── Context Memory Store ──────────────────────────────────────────────────────

class ContextMemory:
    """Vector store cho translation decisions — per-job, in-memory.

    Usage trong pipeline:
        memory = ContextMemory()

        # Sau mỗi chunk dịch xong:
        memory.add(chunk_idx, original_text, translated_text, terms_used)

        # Trước khi dịch chunk mới:
        context = memory.retrieve_context(new_chunk_text)
        prompt = _build_prompt(text, glossary, context_text=context)
    """

    def __init__(self):
        self._decisions: list[TranslationDecision] = []
        self._vectors: list[np.ndarray] = []
        self._vectorizer = _TfidfVectorizer()
        self._style_profile: dict[str, str] = {}  # accumulated style choices

    @property
    def size(self) -> int:
        return len(self._decisions)

    def add(
        self,
        chunk_index: int,
        original: str,
        translated: str,
        key_terms: dict[str, str] | None = None,
    ):
        """Lưu một chunk đã dịch vào memory.

        Gọi sau mỗi chunk dịch xong (trước khi sang chunk tiếp).
        """
        decision = TranslationDecision(
            chunk_index=chunk_index,
            original=original,
            translated=translated,
            key_terms=key_terms or {},
        )
        self._decisions.append(decision)

        # Detect style patterns
        self._update_style_profile(translated)

        # Rebuild vectorizer mỗi khi thêm document mới
        all_originals = [d.original for d in self._decisions]
        self._vectorizer.fit(all_originals)
        self._vectors = [self._vectorizer.transform(d.original) for d in self._decisions]

    def _update_style_profile(self, translated: str):
        """Phát hiện và lưu patterns văn phong từ bản dịch."""
        text_lower = translated.lower()

        # Detect xưng hô
        if 'chúng tôi' in text_lower:
            self._style_profile['pronoun_we'] = 'chúng tôi'
        elif 'chúng ta' in text_lower:
            self._style_profile['pronoun_we'] = 'chúng ta'

        # Detect cách dịch "paper/article"
        if 'bài báo' in text_lower:
            self._style_profile['paper'] = 'bài báo'
        elif 'bài viết' in text_lower:
            self._style_profile['paper'] = 'bài viết'
        elif 'nghiên cứu' in text_lower:
            self._style_profile['paper'] = 'nghiên cứu'

        # Detect cách dịch "method/approach"
        if 'phương pháp' in text_lower:
            self._style_profile['method'] = 'phương pháp'
        elif 'cách tiếp cận' in text_lower:
            self._style_profile['method'] = 'cách tiếp cận'

        # Detect cách dịch "result/performance"
        if 'kết quả' in text_lower:
            self._style_profile['result'] = 'kết quả'
        elif 'hiệu suất' in text_lower:
            self._style_profile['result'] = 'hiệu suất'

    def retrieve(
        self,
        query_text: str,
        top_k: int = MAX_CONTEXT_CHUNKS,
        min_similarity: float = MIN_SIMILARITY,
    ) -> list[tuple[TranslationDecision, float]]:
        """Truy xuất top-K chunks tương tự nhất với query text.

        Returns: list of (decision, similarity_score), sorted by relevance.
        """
        if not self._decisions or not self._vectors:
            return []

        query_vec = self._vectorizer.transform(query_text)

        scored = []
        total = len(self._decisions)
        for i, (decision, doc_vec) in enumerate(zip(self._decisions, self._vectors)):
            sim = _cosine_similarity(query_vec, doc_vec)

            # Recency bonus: chunks gần nhất được ưu tiên nhẹ
            recency = (i / total) * RECENCY_BONUS if total > 0 else 0
            final_score = sim + recency

            if final_score >= min_similarity:
                scored.append((decision, final_score))

        # Sort by score descending, take top K
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def retrieve_context(
        self,
        query_text: str,
        max_chars: int = MAX_CONTEXT_CHARS,
    ) -> str:
        """Truy xuất và format context text sẵn sàng inject vào prompt.

        Returns chuỗi context (có thể rỗng nếu memory trống hoặc không tìm thấy
        chunks tương tự).
        """
        results = self.retrieve(query_text)
        if not results:
            return ""

        parts = []
        total_chars = 0

        for decision, score in results:
            # Tóm tắt chunk tham chiếu
            orig_snippet = decision.original[:100].replace('\n', ' ')
            trans_snippet = decision.summary()
            entry = f"  EN: {orig_snippet}...\n  VI: {trans_snippet}"

            if total_chars + len(entry) > max_chars:
                break
            parts.append(entry)
            total_chars += len(entry)

        if not parts:
            return ""

        # Thêm style notes nếu có
        style_text = self._format_style_notes()

        header = "=== NGỮ CẢNH DỊCH THUẬT (tham khảo từ các đoạn trước) ===\n"
        body = "\n---\n".join(parts)
        footer = (
            "\nHãy dịch nhất quán với văn phong và thuật ngữ đã dùng ở trên.\n\n"
        )

        return header + style_text + body + footer

    def _format_style_notes(self) -> str:
        """Format style profile thành text hướng dẫn."""
        if not self._style_profile:
            return ""

        notes = []
        mapping = {
            'pronoun_we': 'Đại từ "we"',
            'paper': 'Từ "paper"',
            'method': 'Từ "method"',
            'result': 'Từ "result"',
        }
        for key, label in mapping.items():
            if key in self._style_profile:
                notes.append(f'  {label} → dịch là "{self._style_profile[key]}"')

        if not notes:
            return ""
        return "Văn phong đã chọn:\n" + "\n".join(notes) + "\n\n"

    def get_style_profile(self) -> dict[str, str]:
        """Trả về style profile hiện tại."""
        return dict(self._style_profile)

    def save_to_progress(self, progress: dict):
        """Lưu metadata context memory vào progress.json (để resume).

        Chỉ lưu metadata (chunk indexes, style_profile, key_terms) — KHÔNG lưu
        full text (vốn đã có trong chunks/ trên disk). Pipeline sẽ rebuild
        decisions từ chunk files khi resume.
        """
        progress["context_memory"] = {
            "chunk_indexes": [d.chunk_index for d in self._decisions],
            "key_terms_by_chunk": {
                str(d.chunk_index): d.key_terms for d in self._decisions if d.key_terms
            },
            "style_profile": self._style_profile,
            "size": len(self._decisions),
        }

    def load_from_progress(self, progress: dict):
        """Khôi phục style_profile từ progress.json.

        Decisions/vectors KHÔNG được khôi phục ở đây — pipeline sẽ rebuild
        từ chunk files trên disk sau khi load (xem _rebuild_memory_from_disk).
        """
        cm_data = progress.get("context_memory")
        if not cm_data:
            return

        # Restore style profile (lightweight, không cần disk)
        saved_style = cm_data.get("style_profile", {})
        self._style_profile.update(saved_style)

        if saved_style:
            print(f"[ContextMemory] Restored style_profile: {saved_style}")

    def clear(self):
        """Reset toàn bộ memory."""
        self._decisions.clear()
        self._vectors.clear()
        self._style_profile.clear()
        self._vectorizer = _TfidfVectorizer()
