# -*- coding: utf-8 -*-
"""A/B Test: Glossary vs No-Glossary — Glossary Compliance Measurement.

Usage (từ thư mục backend/):

  # Bước 1: Dịch baseline (không glossary) — cần Gemini đang mở
  python abtest_glossary.py --translate-baseline

  # Bước 2: Phân tích kết quả (sau khi có đủ dữ liệu)
  python abtest_glossary.py --analyze

  # Chạy cả hai luôn:
  python abtest_glossary.py --full

Paper: "Evaluation of Explainable Artificial Intelligence: SHAP, LIME, and CAM"
Job dir: workspace/jobs/pdf_Evaluation_of_Explainable_Artificial_Intelligence_/
"""

import asyncio
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import NamedTuple

# Force UTF-8 stdout on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Config ───────────────────────────────────────────────────────────────────

JOB_DIR = Path("workspace/jobs/pdf_Evaluation_of_Explainable_Artificial_Intelligence_")
CHUNKS_DIR = JOB_DIR / "chunks"
BASELINE_DIR = JOB_DIR / "chunks_no_glossary"   # tạo mới khi chạy baseline
ABTEST_REPORT = JOB_DIR / "abtest_report.json"
ABTEST_TXT    = JOB_DIR / "abtest_report.txt"

# Số chunks dùng cho A/B test (chọn chunks chứa nhiều thuật ngữ nhất)
N_TEST_CHUNKS = 12

# ── Reference Glossary — thuật ngữ XAI chuẩn ─────────────────────────────────
# format: EN term (lowercase) -> Vietnamese expected translation

REFERENCE_GLOSSARY: dict[str, str] = {
    # Core XAI terms
    "explainable artificial intelligence": "trí tuệ nhân tạo có thể giải thích được",
    "xai": "XAI",
    "explainability": "khả năng giải thích",
    "interpretability": "khả năng diễn giải",
    "interpretable": "có thể diễn giải",
    "transparency": "tính minh bạch",
    "algorithmic transparency": "tính minh bạch của thuật toán",
    "black box": "hộp đen",
    "black-box": "hộp đen",
    "post-hoc": "hậu kỳ",
    "intrinsic": "nội tại",
    "global interpretability": "khả năng diễn giải toàn cục",
    "local interpretability": "khả năng diễn giải cục bộ",

    # Methods
    "shap": "SHAP",
    "shapley": "Shapley",
    "shapley values": "giá trị Shapley",
    "lime": "LIME",
    "cam": "CAM",
    "class activation mapping": "bản đồ kích hoạt lớp",
    "class activation map": "bản đồ kích hoạt lớp",
    "grad-cam": "Grad-CAM",
    "gradient": "gradient",
    "global average pooling": "gộp trung bình toàn cục",
    "gap": "GAP",

    # ML general
    "machine learning": "học máy",
    "deep learning": "học sâu",
    "neural network": "mạng nơ-ron",
    "convolutional neural network": "mạng nơ-ron tích chập",
    "cnn": "CNN",
    "feature importance": "tầm quan trọng đặc trưng",
    "feature attribution": "quy gán đặc trưng",
    "prediction": "dự đoán",
    "classifier": "bộ phân loại",
    "classification": "phân loại",
    "model agnostic": "không phụ thuộc mô hình",
    "model-agnostic": "không phụ thuộc mô hình",
    "overfitting": "quá khớp",
    "perturbation": "nhiễu loạn",
    "surrogate model": "mô hình thay thế",
    "saliency map": "bản đồ nổi bật",
    "activation map": "bản đồ kích hoạt",
    "image classification": "phân loại hình ảnh",
    "object detection": "phát hiện đối tượng",
    "bounding box": "hộp giới hạn",
    "localization": "định vị",
    "trust": "sự tin cậy",
    "decision": "quyết định",
    "reasoning": "lập luận",
}

# Aliases: nhiều cách viết khác nhau của cùng concept
ALIASES: dict[str, str] = {
    "black boxes": "black-box",
    "predictions": "prediction",
    "classifiers": "classifier",
    "neural networks": "neural network",
    "convolutional neural networks": "convolutional neural network",
    "cnns": "cnn",
    "gradients": "gradient",
    "decisions": "decision",
    "class activation maps": "class activation map",
}

# Expected Vietnamese translations — cả từ đồng nghĩa đều được chấp nhận
ACCEPTED_VI: dict[str, list[str]] = {
    "explainable artificial intelligence": [
        "trí tuệ nhân tạo có thể giải thích được",
        "trí tuệ nhân tạo giải thích được",
        "xai",
    ],
    "machine learning": ["học máy"],
    "deep learning": ["học sâu"],
    "interpretability": ["khả năng diễn giải", "tính có thể diễn giải", "khả năng giải thích"],
    "explainability": ["khả năng giải thích", "tính giải thích được"],
    "black box": ["hộp đen"],
    "black-box": ["hộp đen"],
    "black boxes": ["hộp đen"],
    "shap": ["shap"],
    "lime": ["lime"],
    "cam": ["cam", "bản đồ kích hoạt lớp"],
    "class activation mapping": ["bản đồ kích hoạt lớp", "bản đồ kích hoạt class"],
    "class activation map": ["bản đồ kích hoạt lớp", "bản đồ kích hoạt class"],
    "neural network": ["mạng nơ-ron", "mạng thần kinh", "mạng neural"],
    "convolutional neural network": ["mạng nơ-ron tích chập", "mạng tích chập"],
    "feature importance": ["tầm quan trọng đặc trưng", "mức độ quan trọng của đặc trưng"],
    "prediction": ["dự đoán", "dự báo"],
    "predictions": ["dự đoán", "dự báo", "các dự đoán"],
    "classifier": ["bộ phân loại", "phân loại"],
    "classification": ["phân loại"],
    "global average pooling": ["gộp trung bình toàn cục", "tổng hợp trung bình toàn cục"],
    "transparency": ["tính minh bạch", "sự minh bạch"],
    "post-hoc": ["hậu kỳ", "post-hoc"],
    "trust": ["sự tin cậy", "niềm tin", "tin cậy"],
    "perturbation": ["nhiễu loạn", "nhiễu", "perturbation"],
    "surrogate model": ["mô hình thay thế", "mô hình ủy nhiệm"],
    "saliency map": ["bản đồ nổi bật", "bản đồ hiển thị"],
}


# ── Data structures ───────────────────────────────────────────────────────────

class TermStats(NamedTuple):
    term_en: str
    term_vi_expected: str
    count_in_originals: int       # số lần xuất hiện EN trong originals
    correct_with_glossary: int    # số lần VI đúng trong with-glossary translations
    correct_no_glossary: int      # số lần VI đúng trong no-glossary translations


# ── Chunk utilities ───────────────────────────────────────────────────────────

def read_chunks(chunks_dir: Path) -> list[tuple[str, str, str]]:
    """Returns list of (chunk_id, original_text, translated_text)."""
    results = []
    for f in sorted(chunks_dir.iterdir()):
        if not f.name.endswith("_original.txt"):
            continue
        chunk_id = f.name.replace("_original.txt", "")
        orig = f.read_text(encoding="utf-8")
        trans_file = chunks_dir / f"{chunk_id}_translated.txt"
        trans = trans_file.read_text(encoding="utf-8") if trans_file.exists() else ""
        results.append((chunk_id, orig, trans))
    return results


def count_term_in_text(term: str, text: str) -> int:
    """Count case-insensitive occurrences of term in text."""
    return len(re.findall(re.escape(term), text, re.IGNORECASE))


def check_vi_translation(term_en: str, translated_text: str) -> bool:
    """Check if any accepted VI translation of term_en appears in translated_text."""
    accepted = ACCEPTED_VI.get(term_en, [REFERENCE_GLOSSARY.get(term_en, "").lower()])
    trans_lower = translated_text.lower()
    for vi in accepted:
        if vi.lower() in trans_lower:
            return True
    return False


def select_test_chunks(all_chunks: list[tuple[str, str, str]], n: int) -> list[tuple[str, str, str]]:
    """Select top-N chunks containing the most glossary terms."""
    scored = []
    for chunk_id, orig, trans in all_chunks:
        score = sum(
            count_term_in_text(term, orig)
            for term in REFERENCE_GLOSSARY
        )
        scored.append((score, chunk_id, orig, trans))
    scored.sort(reverse=True)
    return [(cid, orig, trans) for _, cid, orig, trans in scored[:n]]


# ── Glossary prompt builder ───────────────────────────────────────────────────

def build_translation_prompt_with_glossary(chunk_text: str) -> str:
    glossary_section = (
        "=== BẢNG THUẬT NGỮ (BẮT BUỘC dùng đúng bản dịch này) ===\n"
        + "\n".join(f'  "{en}" → "{vi}"' for en, vi in sorted(REFERENCE_GLOSSARY.items()))
        + "\n\n"
    )
    return (
        f"{glossary_section}"
        "Dịch đoạn văn bản học thuật sau sang tiếng Việt.\n"
        "Yêu cầu:\n"
        "- Dùng ĐÚNG bản dịch thuật ngữ theo bảng trên\n"
        "- Giữ nguyên [BLOCK_XXX] markers\n"
        "- Giữ nguyên các viết tắt (SHAP, LIME, CAM, CNN, XAI, GAP...)\n"
        "- Giữ nguyên các số, công thức, citations\n"
        "- KHÔNG dịch tên riêng, tên tác giả\n\n"
        f"=== VĂN BẢN ===\n{chunk_text}"
    )


def build_translation_prompt_no_glossary(chunk_text: str) -> str:
    return (
        "Dịch đoạn văn bản học thuật sau sang tiếng Việt.\n"
        "Yêu cầu:\n"
        "- Giữ nguyên [BLOCK_XXX] markers\n"
        "- Giữ nguyên các viết tắt (SHAP, LIME, CAM, CNN, XAI, GAP...)\n"
        "- Giữ nguyên các số, công thức, citations\n"
        "- KHÔNG dịch tên riêng, tên tác giả\n\n"
        f"=== VĂN BẢN ===\n{chunk_text}"
    )


# ── Compliance analysis ───────────────────────────────────────────────────────

def analyze_compliance(
    test_chunks: list[tuple[str, str, str]],
    baseline_chunks: dict[str, str],   # chunk_id -> no-glossary translated text
) -> list[TermStats]:
    """Compute per-term compliance stats for with-glossary vs no-glossary."""
    results = []

    # Normalize aliases
    def canonical(term: str) -> str:
        return ALIASES.get(term, term)

    # Terms to evaluate (union of reference glossary and aliases)
    all_terms = list(REFERENCE_GLOSSARY.keys())

    for term_en in all_terms:
        canon = canonical(term_en)
        vi_expected = REFERENCE_GLOSSARY.get(canon, REFERENCE_GLOSSARY.get(term_en, ""))

        total_appearances = 0
        correct_with = 0
        correct_no = 0

        for chunk_id, orig, trans_with in test_chunks:
            appearances = count_term_in_text(term_en, orig)
            if appearances == 0:
                continue
            total_appearances += appearances

            # WITH glossary: use existing translated files
            if check_vi_translation(term_en, trans_with):
                correct_with += appearances  # count all occurrences as correct

            # NO glossary: use baseline translations
            trans_no = baseline_chunks.get(chunk_id, "")
            if trans_no and check_vi_translation(term_en, trans_no):
                correct_no += appearances

        if total_appearances > 0:
            results.append(TermStats(
                term_en=term_en,
                term_vi_expected=vi_expected,
                count_in_originals=total_appearances,
                correct_with_glossary=correct_with,
                correct_no_glossary=correct_no,
            ))

    # Sort by total appearances descending
    results.sort(key=lambda x: x.count_in_originals, reverse=True)
    return results


# ── Baseline translation (Playwright) ────────────────────────────────────────

async def run_baseline_translations(test_chunks: list[tuple[str, str, str]]):
    """Re-translate test chunks WITHOUT glossary using Playwright + Gemini."""
    sys.path.insert(0, str(Path(__file__).parent))
    from app.services.translator import WebAITranslator

    BASELINE_DIR.mkdir(exist_ok=True)
    translator = WebAITranslator(user_data_dir="./browser_data")

    print(f"\n[BASELINE] Dịch {len(test_chunks)} chunks KHÔNG có glossary...")
    print("[BASELINE] Đảm bảo Gemini đã đăng nhập trong browser_data/\n")

    async with translator:
        for i, (chunk_id, orig, _) in enumerate(test_chunks):
            out_file = BASELINE_DIR / f"{chunk_id}_translated.txt"
            if out_file.exists():
                print(f"  [{i+1}/{len(test_chunks)}] {chunk_id}: đã có baseline, bỏ qua")
                continue

            print(f"  [{i+1}/{len(test_chunks)}] Đang dịch {chunk_id} (không glossary)...")
            prompt = build_translation_prompt_no_glossary(orig)
            try:
                translated = await translator.translate_chunk(prompt)
                out_file.write_text(translated, encoding="utf-8")
                print(f"    ✓ Saved ({len(translated)} chars)")
            except Exception as e:
                print(f"    ✗ Lỗi: {e}")
                out_file.write_text(f"[ERROR: {e}]", encoding="utf-8")

            if i < len(test_chunks) - 1:
                time.sleep(3)

    print(f"\n[BASELINE] Hoàn thành. Saved to: {BASELINE_DIR}")


# ── Report generation ─────────────────────────────────────────────────────────

def compute_aggregate(stats: list[TermStats]) -> dict:
    """Compute aggregate metrics."""
    total_appearances = sum(s.count_in_originals for s in stats)
    total_correct_with = sum(s.correct_with_glossary for s in stats)
    total_correct_no = sum(s.correct_no_glossary for s in stats)

    rate_with = total_correct_with / total_appearances if total_appearances else 0
    rate_no = total_correct_no / total_appearances if total_appearances else 0

    return {
        "total_term_appearances": total_appearances,
        "total_correct_with_glossary": total_correct_with,
        "total_correct_no_glossary": total_correct_no,
        "compliance_rate_with_glossary": round(rate_with * 100, 1),
        "compliance_rate_no_glossary": round(rate_no * 100, 1),
        "improvement_pp": round((rate_with - rate_no) * 100, 1),
        "terms_evaluated": len(stats),
    }


def print_report(stats: list[TermStats], agg: dict, has_baseline: bool):
    """Print formatted report to stdout and save to file."""
    lines = []

    lines.append("=" * 78)
    lines.append("A/B TEST REPORT: GLOSSARY vs NO-GLOSSARY COMPLIANCE")
    lines.append("Paper: Evaluation of Explainable Artificial Intelligence (SHAP, LIME, CAM)")
    lines.append("=" * 78)

    if not has_baseline:
        lines.append("")
        lines.append("⚠  BASELINE CHƯA CÓ — chỉ hiển thị WITH-GLOSSARY compliance.")
        lines.append("   Chạy:  python abtest_glossary.py --translate-baseline")
        lines.append("   để có so sánh A/B đầy đủ.")

    lines.append("")
    lines.append("TỔNG QUAN")
    lines.append("-" * 40)
    lines.append(f"  Số thuật ngữ đánh giá       : {agg['terms_evaluated']}")
    lines.append(f"  Tổng lượt xuất hiện (EN)    : {agg['total_term_appearances']}")
    lines.append(f"  Compliance CÓ glossary      : {agg['compliance_rate_with_glossary']}%"
                 f"  ({agg['total_correct_with_glossary']}/{agg['total_term_appearances']})")
    if has_baseline:
        lines.append(f"  Compliance KHÔNG glossary   : {agg['compliance_rate_no_glossary']}%"
                     f"  ({agg['total_correct_no_glossary']}/{agg['total_term_appearances']})")
        lines.append(f"  Cải thiện (percentage pts)  : +{agg['improvement_pp']} pp")
    lines.append("")

    # Per-term table
    col_w = [30, 28, 6, 12, 12, 10]
    header = (
        f"{'English Term':<{col_w[0]}}"
        f"{'Vietnamese (Expected)':<{col_w[1]}}"
        f"{'Cnt':>{col_w[2]}}"
        f"{'w/ Gloss':>{col_w[3]}}"
    )
    if has_baseline:
        header += f"{'No Gloss':>{col_w[4]}}{'Δ':>{col_w[5]}}"

    lines.append("PER-TERM BREAKDOWN")
    lines.append("-" * (78 if has_baseline else 60))
    lines.append(header)
    lines.append("-" * (78 if has_baseline else 60))

    for s in stats:
        rate_with = s.correct_with_glossary / s.count_in_originals * 100
        line = (
            f"{s.term_en[:col_w[0]-1]:<{col_w[0]}}"
            f"{s.term_vi_expected[:col_w[1]-1]:<{col_w[1]}}"
            f"{s.count_in_originals:>{col_w[2]}}"
            f"  {s.correct_with_glossary}/{s.count_in_originals} ({rate_with:.0f}%)"
        )
        if has_baseline:
            rate_no = s.correct_no_glossary / s.count_in_originals * 100
            delta = rate_with - rate_no
            delta_str = f"+{delta:.0f}%" if delta >= 0 else f"{delta:.0f}%"
            line += (
                f"  {s.correct_no_glossary}/{s.count_in_originals} ({rate_no:.0f}%)"
                f"  {delta_str:>8}"
            )
        lines.append(line)

    lines.append("-" * (78 if has_baseline else 60))

    if has_baseline:
        # Top gainers
        gainers = sorted(
            [(s.correct_with_glossary / s.count_in_originals
              - s.correct_no_glossary / s.count_in_originals, s)
             for s in stats if s.count_in_originals >= 2],
            reverse=True,
        )[:5]

        lines.append("")
        lines.append("TOP 5 TERMS — IMPROVEMENT NHẤT KHI CÓ GLOSSARY")
        lines.append("-" * 60)
        for delta, s in gainers:
            rate_with = s.correct_with_glossary / s.count_in_originals * 100
            rate_no = s.correct_no_glossary / s.count_in_originals * 100
            lines.append(
                f"  {s.term_en:<35} {rate_no:.0f}% → {rate_with:.0f}%  (+{delta*100:.0f} pp)"
            )

        # Example: most impactful term for paper narrative
        if gainers:
            best_delta, best_s = gainers[0]
            lines.append("")
            lines.append("VÍ DỤ CỤ THỂ ĐỂ TRÍCH DẪN TRONG BÁO CÁO:")
            lines.append(
                f'  "{best_s.term_en}" → "{best_s.term_vi_expected}"'
            )
            lines.append(
                f"  Xuất hiện {best_s.count_in_originals} lần trong bài."
            )
            lines.append(
                f"  Với glossary: {best_s.correct_with_glossary}/{best_s.count_in_originals}"
                f" ({best_s.correct_with_glossary/best_s.count_in_originals*100:.0f}%)"
            )
            lines.append(
                f"  Không glossary: {best_s.correct_no_glossary}/{best_s.count_in_originals}"
                f" ({best_s.correct_no_glossary/best_s.count_in_originals*100:.0f}%)"
            )

    lines.append("")
    lines.append("=" * 78)

    report_text = "\n".join(lines)
    print(report_text)
    ABTEST_TXT.write_text(report_text, encoding="utf-8")
    print(f"\n[SAVED] {ABTEST_TXT}")
    return report_text


def save_json_report(stats: list[TermStats], agg: dict):
    data = {
        "aggregate": agg,
        "per_term": [
            {
                "term_en": s.term_en,
                "term_vi_expected": s.term_vi_expected,
                "count_in_originals": s.count_in_originals,
                "correct_with_glossary": s.correct_with_glossary,
                "rate_with_glossary": round(s.correct_with_glossary / s.count_in_originals * 100, 1)
                    if s.count_in_originals else 0,
                "correct_no_glossary": s.correct_no_glossary,
                "rate_no_glossary": round(s.correct_no_glossary / s.count_in_originals * 100, 1)
                    if s.count_in_originals else 0,
                "improvement_pp": round(
                    (s.correct_with_glossary - s.correct_no_glossary)
                    / s.count_in_originals * 100, 1
                ) if s.count_in_originals else 0,
            }
            for s in stats
        ],
    }
    ABTEST_REPORT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SAVED] {ABTEST_REPORT}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_analysis():
    print(f"[ANALYZE] Đọc chunks từ {CHUNKS_DIR} ...")
    all_chunks = read_chunks(CHUNKS_DIR)
    print(f"[ANALYZE] Tổng cộng {len(all_chunks)} chunks")

    test_chunks = select_test_chunks(all_chunks, N_TEST_CHUNKS)
    print(f"[ANALYZE] Chọn {len(test_chunks)} chunks có nhiều thuật ngữ nhất:")
    for cid, orig, _ in test_chunks:
        score = sum(count_term_in_text(t, orig) for t in REFERENCE_GLOSSARY)
        print(f"  {cid}: {score} term appearances")

    # Load baseline translations if available
    baseline_chunks: dict[str, str] = {}
    has_baseline = BASELINE_DIR.exists()
    if has_baseline:
        for f in BASELINE_DIR.iterdir():
            if f.name.endswith("_translated.txt"):
                cid = f.name.replace("_translated.txt", "")
                baseline_chunks[cid] = f.read_text(encoding="utf-8")
        # Check if we have baseline for all test chunks
        test_ids = {cid for cid, _, _ in test_chunks}
        missing = test_ids - set(baseline_chunks.keys())
        if missing:
            print(f"\n⚠  Baseline thiếu {len(missing)} chunks: {missing}")
            has_baseline = len(missing) == 0
    else:
        print("\n⚠  Chưa có baseline directory. Chạy --translate-baseline trước.")

    print("\n[ANALYZE] Tính compliance...")
    stats = analyze_compliance(test_chunks, baseline_chunks)
    stats = [s for s in stats if s.count_in_originals > 0]

    agg = compute_aggregate(stats)
    print_report(stats, agg, has_baseline)
    save_json_report(stats, agg)


async def run_baseline_then_analyze():
    all_chunks = read_chunks(CHUNKS_DIR)
    test_chunks = select_test_chunks(all_chunks, N_TEST_CHUNKS)
    await run_baseline_translations(test_chunks)
    run_analysis()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Glossary A/B Test for XAI paper")
    parser.add_argument("--analyze", action="store_true",
                        help="Chỉ phân tích — không cần Playwright")
    parser.add_argument("--translate-baseline", action="store_true",
                        help="Dịch baseline không glossary (cần Playwright + Gemini login)")
    parser.add_argument("--full", action="store_true",
                        help="Chạy baseline translation rồi analyze luôn")
    args = parser.parse_args()

    async def _baseline_only():
        all_chunks = read_chunks(CHUNKS_DIR)
        test_chunks = select_test_chunks(all_chunks, N_TEST_CHUNKS)
        await run_baseline_translations(test_chunks)

    if args.translate_baseline:
        asyncio.run(_baseline_only())
    elif args.full:
        asyncio.run(run_baseline_then_analyze())
    else:
        run_analysis()
