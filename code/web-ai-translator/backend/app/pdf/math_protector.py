"""Protect mathematical expressions during translation.

Replaces math expressions with unique placeholders (<<MATH_1>>, <<MATH_2>>...)
before sending text to the translator, then restores them afterward.

This prevents LLMs from modifying, translating, or hallucinating math content.

Works for:
- LaTeX inline math: $...$, \(...\)
- LaTeX display math: $$...$$, \[...\]
- LaTeX commands in text: \alpha, \beta, \textbf{...}
- Common math patterns: equations like "x = 3y + 2", "E = mc^2"
- Standalone symbols and numbers in math context
"""

import re
from dataclasses import dataclass, field


@dataclass
class MathProtector:
    """Protect and restore math expressions using placeholders.

    Usage:
        protector = MathProtector()
        safe_text = protector.protect(original_text)
        # ... translate safe_text ...
        final_text = protector.restore(translated_text)
    """

    _store: dict = field(default_factory=dict)   # placeholder → original
    _counter: int = 0
    placeholder_prefix: str = "<<MATH_"
    placeholder_suffix: str = ">>"

    def _next_placeholder(self) -> str:
        self._counter += 1
        return f"{self.placeholder_prefix}{self._counter}{self.placeholder_suffix}"

    def _replace(self, match: re.Match) -> str:
        original = match.group(0)
        # Don't re-protect already-protected content
        if original.startswith(self.placeholder_prefix):
            return original
        ph = self._next_placeholder()
        self._store[ph] = original
        return ph

    def reset(self):
        """Clear all stored placeholders (call between documents)."""
        self._store.clear()
        self._counter = 0

    def protect(self, text: str) -> str:
        """Replace math expressions with placeholders.

        Order matters: longer/more specific patterns first to avoid
        partial matches.
        """
        if not text:
            return text

        result = text

        # 1. LaTeX display math: $$...$$ (greedy within reason)
        result = re.sub(
            r'\$\$(.+?)\$\$',
            self._replace,
            result,
            flags=re.DOTALL,
        )

        # 2. LaTeX display math: \[...\]
        result = re.sub(
            r'\\\[.+?\\\]',
            self._replace,
            result,
            flags=re.DOTALL,
        )

        # 3. LaTeX inline math: \(...\)
        result = re.sub(
            r'\\\(.+?\\\)',
            self._replace,
            result,
            flags=re.DOTALL,
        )

        # 4. LaTeX inline math: $...$ (not currency like "$5")
        #    Match $...$ where content has at least one LaTeX command or math symbol
        result = re.sub(
            r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)',
            self._maybe_replace_inline_math,
            result,
        )

        # 5. LaTeX environments in text: \begin{equation}...\end{equation}
        result = re.sub(
            r'\\begin\{[^}]+\}.*?\\end\{[^}]+\}',
            self._replace,
            result,
            flags=re.DOTALL,
        )

        # 6. LaTeX commands with arguments: \frac{...}{...}, \sqrt{...}, etc.
        result = re.sub(
            r'\\(?:frac|sqrt|sum|prod|int|lim|max|min|sup|inf|log|ln|exp|sin|cos|tan'
            r'|text|textbf|textit|mathrm|mathbf|mathcal|mathbb|vec|hat|bar|tilde'
            r'|overline|underline|overbrace|underbrace)'
            r'(?:\{[^}]*\})+',
            self._replace,
            result,
        )

        # 7. Standalone LaTeX Greek/math commands: \alpha, \beta, \infty, etc.
        result = re.sub(
            r'\\(?:alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa'
            r'|lambda|mu|nu|xi|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega'
            r'|Alpha|Beta|Gamma|Delta|Epsilon|Zeta|Eta|Theta|Iota|Kappa'
            r'|Lambda|Mu|Nu|Xi|Pi|Rho|Sigma|Tau|Upsilon|Phi|Chi|Psi|Omega'
            r'|infty|partial|nabla|forall|exists|in|notin|subset|supset'
            r'|cup|cap|vee|wedge|neg|cdot|times|div|pm|mp|leq|geq|neq'
            r'|approx|equiv|sim|propto|rightarrow|leftarrow|Rightarrow'
            r'|Leftarrow|leftrightarrow|uparrow|downarrow)\b',
            self._replace,
            result,
        )

        # 8. Common equation patterns in plain text:
        #    "x = 3y + 2", "E = mc²", "f(x) = ...", "P(A|B)"
        #    Only match if it looks like a formula (has = and variables)
        result = re.sub(
            r'(?<![a-zA-Z])[A-Za-z]\s*\([^)]{1,30}\)\s*=\s*[^,.\n]{3,60}',
            self._replace,
            result,
        )

        # 9. Superscript/subscript patterns: x², x₁, R², O(n²)
        result = re.sub(
            r'[A-Za-z]\s*[²³¹⁰⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉]+',
            self._replace,
            result,
        )

        # 10. (D3) ASCII variable with caret/underscore exponent or index:
        #     x^2, x^{2n}, x_i, x_{ij}, n^k, R^n
        #     Single-char base + ^ or _ + (braced expression OR token).
        result = re.sub(
            r'(?<![A-Za-z])[A-Za-z]\s*[\^_]\s*(?:\{[^}]{1,30}\}|[A-Za-z0-9+\-]{1,6})',
            self._replace,
            result,
        )

        # 11. (D3) Standalone Unicode math operators / relations.
        #     Example: "≤", "≈", "→", "∈". Captured 1-by-1 so they don't
        #     leak into translated prose.
        result = re.sub(
            r'[≤≥≠≈≡≅∝∞∑∏∫∂∇∈∉⊂⊃⊆⊇∪∩∧∨¬±×÷⇒⇐⇔→←↔↑↓⊕⊗√∀∃]',
            self._replace,
            result,
        )

        # 12. (D3) Function-call notation that looks formulaic — single-char
        #     identifier followed by parenthesised args containing math.
        #     Example: "f(x)", "g(x, y)", "P(A | B)". We deliberately
        #     scope the args to ≤30 chars so we don't grab English clauses.
        result = re.sub(
            r'(?<![A-Za-z])[A-Za-z]\([A-Za-z0-9,\s+\-*/=<>≤≥≠|]{1,30}\)',
            self._replace,
            result,
        )

        # 13. (D3) Inline equations without `$` delimiters:
        #     "x = 3y + 2", "α + β = γ", "n ≥ 5".
        #     Heuristic: identifier + relational operator + RHS, no period
        #     in middle (avoids matching real sentences).
        result = re.sub(
            r'(?<![A-Za-z])[A-Za-zα-ωΑ-Ω][A-Za-z0-9]{0,4}'
            r'\s*[=<>≤≥≠]\s*'
            r'[A-Za-z0-9α-ωΑ-Ω+\-*/().\s]{1,40}'
            r'(?=[\s,;.]|$)',
            self._maybe_replace_equation,
            result,
        )

        # 14. (D3) Standalone Greek letters (without backslash) appearing
        #     in academic prose: "α-particle", "β decay", "γ-ray".
        #     Only match when isolated — not inside Vietnamese accented words.
        result = re.sub(
            r'(?<![A-Za-zÀ-ỹ])[α-ωΑ-Ω](?![A-Za-zÀ-ỹ])',
            self._replace,
            result,
        )

        return result

    def _maybe_replace_equation(self, match: re.Match) -> str:
        """Guard for pattern #13: only protect if it really looks like math.

        Reject when the captured RHS is mostly English words (heuristic:
        contains a stop word or 3+ alphabetic words) — those are
        comparison sentences ("X is greater than the threshold"), not
        equations.
        """
        captured = match.group(0)
        rhs = re.split(r"[=<>≤≥≠]", captured, maxsplit=1)
        rhs_text = rhs[1].strip() if len(rhs) > 1 else ""
        # Count alphabetic words in RHS
        words = re.findall(r"[A-Za-z]{2,}", rhs_text)
        if len(words) >= 3:
            return captured  # likely prose, leave it
        # Reject common English word RHS: "than", "to", "the", "if"
        if any(w.lower() in {"than", "the", "and", "or", "if",
                              "with", "without", "is", "are"}
               for w in words):
            return captured
        return self._replace(match)

    def _maybe_replace_inline_math(self, match: re.Match) -> str:
        """Only replace $...$ if content looks like actual math, not currency."""
        content = match.group(1)
        # If content has LaTeX commands, math operators, or looks formulaic
        if (re.search(r'[\\^_{}|]', content)
                or re.search(r'[+\-*/=<>≤≥≠≈∞∑∏∫]', content)
                or re.search(r'[α-ωΑ-Ω]', content)
                or re.search(r'\\[a-zA-Z]+', content)):
            return self._replace(match)
        # Pure number like "$5" — probably currency, don't protect
        if re.match(r'^\d+(?:\.\d+)?$', content.strip()):
            return match.group(0)
        # Default: protect it (safer)
        return self._replace(match)

    def restore(self, text: str) -> str:
        """Restore all placeholders with original math expressions.

        Handles cases where the translator might have:
        - Added spaces around placeholders
        - Changed placeholder case
        - Wrapped placeholders in quotes or backticks
        """
        if not text or not self._store:
            return text

        result = text

        # Sort by placeholder number descending to avoid partial matches
        # (<<MATH_10>> before <<MATH_1>>)
        for ph in sorted(self._store.keys(),
                         key=lambda x: int(re.search(r'\d+', x).group()),
                         reverse=True):
            original = self._store[ph]
            # Try exact match first
            if ph in result:
                result = result.replace(ph, original)
                continue
            # Try with surrounding whitespace/quotes stripped
            escaped = re.escape(ph)
            pattern = rf'["`\']*\s*{escaped}\s*["`\']*'
            result = re.sub(pattern, original, result)

        return result

    @property
    def protected_count(self) -> int:
        """Number of expressions currently protected."""
        return len(self._store)

    def get_store(self) -> dict:
        """Get a copy of the placeholder store (for debugging/logging)."""
        return dict(self._store)


def protect_blocks_math(blocks: list) -> dict:
    """Protect math in a list of TextBlock objects.

    Creates one MathProtector per block to avoid cross-block interference.
    Returns a dict mapping block index → MathProtector instance,
    so each block's math can be restored independently after translation.

    Usage:
        protectors = protect_blocks_math(blocks)
        # blocks[i].text is now protected
        # ... translate ...
        for i, p in protectors.items():
            blocks[i].translated_text = p.restore(blocks[i].translated_text)
    """
    protectors = {}
    for i, block in enumerate(blocks):
        if not block.is_translatable or not block.text:
            continue
        p = MathProtector()
        protected = p.protect(block.text)
        if p.protected_count > 0:
            block.text = protected
            protectors[i] = p
    return protectors


def protect_text_math(text: str) -> tuple[str, MathProtector]:
    """Protect math expressions in a single text string.

    Returns (protected_text, protector).
    Call protector.restore(translated_text) after translation.
    """
    p = MathProtector()
    protected = p.protect(text)
    return protected, p


def protect_chunk_math(chunk: list) -> tuple[list[str], "MathProtector"]:
    """Protect math across an entire chunk using a SHARED MathProtector.

    Unlike `protect_blocks_math` (per-block protectors with colliding
    placeholder numbers), this uses one protector for the whole chunk so
    every placeholder is unique across blocks. This matters when the
    LLM response is a single concatenated string with [N] markers — we
    can run `protector.restore(response)` once and recover all math
    correctly regardless of which block it came from.

    Mutates `block.text` in place with placeholders for translatable
    blocks. Returns:
        (originals, protector)
    where `originals[i]` is the pre-protection text for chunk[i].
    Caller MUST restore `chunk[i].text = originals[i]` after translation
    so subsequent chunks/runs don't see corrupted source text.
    """
    p = MathProtector()
    originals: list[str] = []
    for block in chunk:
        originals.append(block.text)
        if getattr(block, "is_translatable", True) and block.text:
            block.text = p.protect(block.text)
    return originals, p
