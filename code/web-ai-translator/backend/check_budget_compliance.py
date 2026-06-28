"""Check whether Gemini respected the (max ~N chars) budget hints in prompts.

For each prompt + matching translation:
  1. Parse `[K] (max ~M chars) ...` from the prompt's "NỘI DUNG CẦN DỊCH" section.
  2. Parse `[K] translated text` from the translation file.
  3. Compare vi_len vs budget. Compute over/under rate and excess distribution.
"""
import os
import re
import sys
from collections import defaultdict

JOB_DIR = sys.argv[1] if len(sys.argv) > 1 else (
    "workspace/users/trucnb/jobs/pdf_BABOK_Guide_v3_Member"
)
AUDIT = os.path.join(JOB_DIR, "audit_responses")

PROMPT_BLOCK_RE = re.compile(
    r"^\[(\d+)\]\s*\((?:(table cell, |caption, ))?max\s*~\s*(\d+)\s*chars?\)\s*(.*?)$",
    re.MULTILINE,
)
TRANSL_BLOCK_RE = re.compile(
    r"^\[(\d+)\]\s*(.*?)(?=^\[\d+\]|\Z)",
    re.MULTILINE | re.DOTALL,
)

def parse_prompt_budgets(text: str):
    """Return {block_id: (budget, en_len, kind)} where kind in {plain, table, caption}."""
    m = re.search(r"=== NỘI DUNG CẦN DỊCH ===(.+)", text, re.DOTALL)
    if not m:
        return {}
    body = m.group(1)
    out = {}
    for mm in PROMPT_BLOCK_RE.finditer(body):
        k = int(mm.group(1))
        tag = (mm.group(2) or "").strip().rstrip(",")
        budget = int(mm.group(3))
        rest = mm.group(4).strip()
        kind = "table" if tag == "table cell" else "caption" if tag == "caption" else "plain"
        out[k] = (budget, len(rest), kind)
    return out


def parse_translation(text: str):
    out = {}
    for mm in TRANSL_BLOCK_RE.finditer(text):
        k = int(mm.group(1))
        body = mm.group(2).strip()
        out[k] = body
    return out


def bucket_budget(b: int) -> str:
    if b <= 20: return "  1-20 (label)"
    if b <= 50: return " 21-50 (short)"
    if b <= 150: return "51-150 (line)"
    if b <= 400: return "151-400 (para)"
    return "  >400 (long para)"


def main():
    if not os.path.isdir(AUDIT):
        raise SystemExit(f"audit dir not found: {AUDIT}")
    prompts = sorted(f for f in os.listdir(AUDIT) if f.endswith("_prompt.txt"))

    per_kind = defaultdict(lambda: {"under": 0, "over": 0, "total_vi": 0, "total_budget": 0})
    per_budget_bucket = defaultdict(lambda: {"under": 0, "over": 0})
    excess_buckets = defaultdict(int)
    over_budget = under_budget = 0
    worst = []
    total = 0

    for pf in prompts:
        m = re.match(r"chunk_(\d+)_attempt_(\d+)_prompt", pf)
        if not m:
            continue
        chunk_idx, attempt = m.group(1), m.group(2)
        tf = pf.replace("_prompt.txt", "_translation.txt")
        ppath = os.path.join(AUDIT, pf)
        tpath = os.path.join(AUDIT, tf)
        if not os.path.isfile(tpath):
            continue
        with open(ppath, "r", encoding="utf-8") as f:
            prompt_text = f.read()
        with open(tpath, "r", encoding="utf-8") as f:
            tr_text = f.read()

        budgets = parse_prompt_budgets(prompt_text)
        transl = parse_translation(tr_text)
        for k, (budget, en_len, kind) in budgets.items():
            vi = transl.get(k, "")
            vi_len = len(vi)
            if vi_len == 0:
                continue
            total += 1
            per_kind[kind]["total_vi"] += vi_len
            per_kind[kind]["total_budget"] += budget
            bkt = bucket_budget(budget)
            if vi_len > budget:
                over_budget += 1
                per_kind[kind]["over"] += 1
                per_budget_bucket[bkt]["over"] += 1
                excess = (vi_len - budget) / budget
                excess_buckets[
                    "0-25%" if excess < 0.25 else
                    "25-50%" if excess < 0.5 else
                    "50-100%" if excess < 1.0 else
                    ">100%"
                ] += 1
                worst.append((excess, chunk_idx, k, budget, vi_len, vi[:80]))
            else:
                under_budget += 1
                per_kind[kind]["under"] += 1
                per_budget_bucket[bkt]["under"] += 1

    print(f"=== OVERALL ({total} blocks with budget hint, non-empty VI) ===")
    print(f"  Under/equal budget : {under_budget}  ({under_budget*100/max(1,total):5.1f}%)")
    print(f"  Over budget        : {over_budget}  ({over_budget*100/max(1,total):5.1f}%)")
    print()
    print("Excess distribution on over-budget blocks:")
    for k in ("0-25%", "25-50%", "50-100%", ">100%"):
        v = excess_buckets.get(k, 0)
        print(f"  {k:8}  {v:5}  ({v*100/max(1,over_budget):5.1f}% of over-budget)")
    print()
    print("=== BY BLOCK KIND ===")
    for kind, s in per_kind.items():
        tot = s["under"] + s["over"]
        comply = s["under"] * 100 / max(1, tot)
        avg_vi = s["total_vi"] / max(1, tot)
        avg_b = s["total_budget"] / max(1, tot)
        print(f"  {kind:8}  total={tot:5}  under={s['under']:5}  over={s['over']:5}  "
              f"compliance={comply:5.1f}%  avg_vi={avg_vi:5.1f}  avg_budget={avg_b:5.1f}")
    print()
    print("=== BY BUDGET RANGE ===")
    for bkt in ("  1-20 (label)", " 21-50 (short)", "51-150 (line)", "151-400 (para)", "  >400 (long para)"):
        s = per_budget_bucket.get(bkt, {"under": 0, "over": 0})
        tot = s["under"] + s["over"]
        comply = s["under"] * 100 / max(1, tot)
        print(f"  {bkt:25}  total={tot:5}  under={s['under']:5}  over={s['over']:5}  "
              f"compliance={comply:5.1f}%")
    print()
    print("=== 10 WORST OVER-BUDGET (excess%, chunk, block, budget, vi_len, preview) ===")
    worst.sort(reverse=True)
    for excess, ci, k, b, v, prev in worst[:10]:
        print(f"  +{excess*100:6.0f}%  chunk={ci}  [#{k}]  budget={b:4}  vi_len={v:4}  {prev!r}")


if __name__ == "__main__":
    main()
