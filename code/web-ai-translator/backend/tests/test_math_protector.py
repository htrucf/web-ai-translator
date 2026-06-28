"""Tests for app/pdf/math_protector.py.

Covers:
  MathProtector.protect / restore round-trip
  protect_chunk_math() — single shared protector across blocks → unique ids
  protect_blocks_math() — per-block protector (legacy helper)
  Currency $5 is NOT protected (heuristic)
  LaTeX placeholders survive a translator-style mangling
"""

import re
from types import SimpleNamespace

from app.pdf.math_protector import (
    MathProtector,
    protect_blocks_math,
    protect_chunk_math,
    protect_text_math,
)


# ── round-trip ────────────────────────────────────────────────────────────────

def test_protect_then_restore_is_identity_for_inline_math():
    text = r"The energy is given by $E = mc^2$ in special relativity."
    p = MathProtector()
    protected = p.protect(text)
    assert "$E = mc^2$" not in protected, "math not replaced"
    assert "<<MATH_" in protected
    restored = p.restore(protected)
    assert restored == text


def test_protect_display_math_dollar_dollar():
    text = r"Equation: $$\sum_{i=1}^n x_i$$ end."
    p = MathProtector()
    out = p.protect(text)
    # $$...$$ should be captured as a single placeholder
    assert out.count("<<MATH_") == 1
    assert p.restore(out) == text


def test_protect_latex_command_with_args():
    text = r"Use \frac{a}{b} for fractions and \sqrt{x} for roots."
    p = MathProtector()
    out = p.protect(text)
    assert r"\frac{a}{b}" not in out
    assert r"\sqrt{x}" not in out
    assert p.restore(out) == text


def test_protect_greek_letters():
    text = r"Let \alpha and \beta be parameters with \gamma > 0."
    p = MathProtector()
    out = p.protect(text)
    assert r"\alpha" not in out
    assert r"\beta" not in out
    assert r"\gamma" not in out
    assert p.restore(out) == text


def test_isolated_currency_is_not_protected():
    """A single `$5.` (no second $) does not match the inline-math regex."""
    text = "The book costs $5. End."
    p = MathProtector()
    out = p.protect(text)
    assert out == text


def test_two_currency_amounts_get_protected_as_safer_default():
    """`$5 ... $10` matches `$...$` so the heuristic protects it.

    This is a documented false positive — the protector errs on the side of
    "wrap mixed content to keep the translator from mangling it".  We assert
    the behaviour so a future change is intentional.
    """
    text = "The book costs $5 and the pen costs $10."
    p = MathProtector()
    out = p.protect(text)
    # Round-trip must still be identity
    assert p.restore(out) == text


def test_unicode_math_superscript_protected():
    text = "The complexity is O(n²) in the worst case."
    p = MathProtector()
    out = p.protect(text)
    # n² should be protected as a math token
    assert "n²" not in out
    restored = p.restore(out)
    assert "n²" in restored


# ── translator mangling survives restore ────────────────────────────────────

def test_restore_handles_quoted_placeholders():
    """A translator may wrap placeholders in quotes — restore should still work."""
    text = r"We have $E = mc^2$ exactly."
    p = MathProtector()
    out = p.protect(text)
    # Pretend translator wrapped the placeholder in backticks
    placeholder = re.search(r"<<MATH_\d+>>", out).group(0)
    mangled = out.replace(placeholder, f'"{placeholder}"')
    restored = p.restore(mangled)
    assert "$E = mc^2$" in restored


def test_restore_with_extra_whitespace():
    text = r"Inline $\alpha + \beta$ here."
    p = MathProtector()
    out = p.protect(text)
    placeholder = re.search(r"<<MATH_\d+>>", out).group(0)
    # Translator added stray spaces around the placeholder
    mangled = out.replace(placeholder, f"  {placeholder}  ")
    restored = p.restore(mangled)
    assert r"\alpha + \beta" in restored


# ── protect_text_math wrapper ────────────────────────────────────────────────

def test_protect_text_math_returns_protector():
    text = r"Formula: $f(x) = x^2 + 1$"
    protected, p = protect_text_math(text)
    assert isinstance(p, MathProtector)
    assert p.protected_count >= 1
    assert p.restore(protected) == text


# ── protect_blocks_math (legacy per-block) ───────────────────────────────────

def _make_block(text: str, is_translatable: bool = True):
    """Tiny stand-in for TextBlock so we can test math helpers without fitz."""
    return SimpleNamespace(text=text, is_translatable=is_translatable)


def test_protect_blocks_math_skips_non_translatable():
    blocks = [
        _make_block(r"First $x = 1$ block.", is_translatable=True),
        _make_block(r"Skipped $y = 2$ block.", is_translatable=False),
    ]
    protectors = protect_blocks_math(blocks)
    assert 0 in protectors
    assert 1 not in protectors  # non-translatable was skipped
    # Block 0 was mutated, block 1 untouched
    assert "<<MATH_" in blocks[0].text
    assert "<<MATH_" not in blocks[1].text


def test_protect_blocks_math_per_block_protectors_collide():
    """Per-block protectors reuse counters → placeholder NUMBERS clash.

    This is the documented limitation that motivates protect_chunk_math().
    We assert the collision exists so we know the fixture is correct.
    """
    blocks = [
        _make_block(r"Block one with $a + b$ math."),
        _make_block(r"Block two with $c + d$ math."),
    ]
    protect_blocks_math(blocks)
    # Both blocks contain `<<MATH_1>>` — the placeholder ids overlap
    assert "<<MATH_1>>" in blocks[0].text
    assert "<<MATH_1>>" in blocks[1].text


# ── protect_chunk_math (shared protector — main API) ─────────────────────────

def test_protect_chunk_math_unique_ids_across_blocks():
    blocks = [
        _make_block(r"Block one with $a + b$ math."),
        _make_block(r"Block two with $c + d$ math."),
    ]
    originals, p = protect_chunk_math(blocks)

    # Numbers grow across blocks — no collisions
    assert "<<MATH_1>>" in blocks[0].text
    assert "<<MATH_2>>" in blocks[1].text
    assert p.protected_count == 2

    # originals contain the pre-protect text
    assert "$a + b$" in originals[0]
    assert "$c + d$" in originals[1]


def test_protect_chunk_math_restore_after_concat():
    """Single shared protector can restore math from a concatenated response.

    This is the scenario the translator hits: it sends `[1] ... [2] ...` and
    receives a single string back. One restore() call must recover all math.
    """
    blocks = [
        _make_block(r"$\alpha$ first"),
        _make_block(r"$\beta$ second"),
    ]
    originals, p = protect_chunk_math(blocks)

    fake_response = (
        f"[1] Translated: {blocks[0].text}\n\n"
        f"[2] Translated: {blocks[1].text}"
    )
    restored = p.restore(fake_response)

    assert r"$\alpha$" in restored
    assert r"$\beta$" in restored
    assert "<<MATH_" not in restored


def test_protect_chunk_math_skips_empty_and_non_translatable():
    blocks = [
        _make_block(r"Has $x = 1$ here."),
        _make_block("", is_translatable=True),  # empty
        _make_block(r"Skip $y = 2$.", is_translatable=False),  # not translatable
    ]
    originals, p = protect_chunk_math(blocks)

    assert "<<MATH_" in blocks[0].text
    assert blocks[1].text == ""  # empty stays empty
    assert "<<MATH_" not in blocks[2].text  # non-translatable untouched
    # only block 0 contributed a placeholder
    assert p.protected_count == 1
    # originals still mirrors every input position
    assert len(originals) == 3


def test_protect_chunk_math_originals_round_trip():
    """Caller is expected to restore block.text from originals after translation."""
    blocks = [
        _make_block(r"Has $x = 1$ here."),
        _make_block(r"More math: \alpha and \beta."),
    ]
    originals, _ = protect_chunk_math(blocks)

    # Caller restores
    for i, orig in enumerate(originals):
        blocks[i].text = orig

    # Block texts are back to pre-protection state
    assert blocks[0].text == r"Has $x = 1$ here."
    assert blocks[1].text == r"More math: \alpha and \beta."


def test_no_math_means_no_placeholders():
    """Pure prose should round-trip untouched."""
    text = "This is a sentence with no mathematical content at all."
    p = MathProtector()
    out = p.protect(text)
    assert out == text
    assert p.protected_count == 0
