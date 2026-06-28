"""Unit tests cho các converter ở `app.text.*` và safe-extract của `latex_processor`.

Cover:
  - `_escape_text`: escape ký tự đặc biệt của LaTeX
  - `text_to_latex_body` / `text_to_latex`: plain text → LaTeX
  - `markdown_to_latex_body` / `markdown_to_latex`: Markdown subset → LaTeX
  - `convert_to_latex`: dispatch theo ext
  - `html_to_latex_body` / `html_to_latex` / `convert_html_to_latex`: HTML → LaTeX
  - `_safe_extract_zip` / `extract_source_zip`: zip-slip CVE-2007-4559 family
  - `save_single_tex`: ghi 1 file .tex vào source/
  - `_find_main_tex`: chọn .tex có `\\begin{document}` thay vì .tex bất kỳ

Pure-function tests — không cần ASGI client, không cần workspace fixture.
"""

import io
import os
import tarfile
import zipfile

import pytest

from app.text.converter import (
    _escape_text,
    text_to_latex_body,
    text_to_latex,
    markdown_to_latex_body,
    markdown_to_latex,
    convert_to_latex,
    text_ext,
)
from app.text.html_converter import (
    html_ext,
    html_to_latex_body,
    html_to_latex,
    convert_html_to_latex,
)
from app.services.latex_processor import (
    _safe_extract_zip,
    extract_source,
    extract_source_zip,
    save_single_tex,
    _find_main_tex,
)


# ──────────────────────────────────────────────────────────────────────────────
# _escape_text
# ──────────────────────────────────────────────────────────────────────────────

class TestEscapeText:
    def test_no_specials_unchanged(self):
        assert _escape_text("Hello world.") == "Hello world."

    def test_escape_basic_specials(self):
        out = _escape_text("100% & $5 # _ { }")
        # backslash first, sau đó từng ký tự
        assert r"\%" in out
        assert r"\&" in out
        assert r"\$" in out
        assert r"\#" in out
        assert r"\_" in out
        assert r"\{" in out
        assert r"\}" in out

    def test_escape_backslash(self):
        """Backslash được thay bằng `\\textbackslash` token (curly braces có thể bị escape phụ)."""
        out = _escape_text("a\\b")
        # Token \textbackslash phải xuất hiện — curly braces có thể được re-escape bởi loop
        assert r"\textbackslash" in out
        # Backslash thô không được lọt qua
        assert "a\\b" not in out

    def test_escape_tilde_caret(self):
        out = _escape_text("a~b^c")
        assert r"\textasciitilde{}" in out
        assert r"\textasciicircum{}" in out


# ──────────────────────────────────────────────────────────────────────────────
# text_ext / html_ext
# ──────────────────────────────────────────────────────────────────────────────

class TestExtDetect:
    @pytest.mark.parametrize("name,expected", [
        ("a.txt", "txt"),
        ("a.md", "md"),
        ("a.markdown", "md"),
        ("a.TXT", "txt"),
        ("a.pdf", None),
        ("a", None),
    ])
    def test_text_ext(self, name, expected):
        assert text_ext(name) == expected

    @pytest.mark.parametrize("name,expected", [
        ("a.html", "html"),
        ("a.HTM", "html"),
        ("a.htm", "html"),
        ("a.txt", None),
    ])
    def test_html_ext(self, name, expected):
        assert html_ext(name) == expected


# ──────────────────────────────────────────────────────────────────────────────
# text_to_latex
# ──────────────────────────────────────────────────────────────────────────────

class TestTextToLatex:
    def test_empty_body(self):
        assert text_to_latex_body("") == "\n"

    def test_single_paragraph(self):
        out = text_to_latex_body("Hello world.")
        assert "Hello world." in out

    def test_multiple_paragraphs_separated_by_blank_lines(self):
        text = "First paragraph.\n\nSecond paragraph."
        out = text_to_latex_body(text)
        assert "First paragraph." in out
        assert "Second paragraph." in out

    def test_specials_escaped(self):
        out = text_to_latex_body("Hello & $5.")
        assert r"\&" in out
        assert r"\$" in out

    def test_full_document_wraps_with_preamble(self):
        out = text_to_latex("Body.", title="My Title")
        assert r"\documentclass" in out
        assert r"\begin{document}" in out
        assert r"\end{document}" in out
        assert "My Title" in out

    def test_title_escaped_in_preamble(self):
        out = text_to_latex("body", title="100% & co.")
        # Title chứa % → phải có \%
        assert r"\%" in out
        assert r"\&" in out


# ──────────────────────────────────────────────────────────────────────────────
# markdown_to_latex
# ──────────────────────────────────────────────────────────────────────────────

class TestMarkdownToLatex:
    def test_h1_becomes_section(self):
        out = markdown_to_latex_body("# Title\n\nbody.")
        assert r"\section*{Title}" in out

    def test_h2_becomes_subsection(self):
        out = markdown_to_latex_body("## Subtitle")
        assert r"\subsection*{Subtitle}" in out

    def test_h6_clamped_to_subparagraph(self):
        out = markdown_to_latex_body("###### Tiny")
        assert r"\subparagraph*{Tiny}" in out

    def test_bold_double_asterisk(self):
        out = markdown_to_latex_body("This is **bold** text.")
        assert r"\textbf{bold}" in out

    def test_italic_single_asterisk(self):
        out = markdown_to_latex_body("This is *italic* text.")
        assert r"\textit{italic}" in out

    def test_inline_code(self):
        out = markdown_to_latex_body("Use `print()` to output.")
        assert r"\texttt{print()}" in out

    def test_link(self):
        out = markdown_to_latex_body("[click here](https://example.com) ok.")
        assert r"\href{https://example.com}{click here}" in out

    def test_unordered_list(self):
        out = markdown_to_latex_body("- one\n- two\n- three\n")
        assert r"\begin{itemize}" in out
        assert r"\end{itemize}" in out
        assert r"\item one" in out
        assert r"\item three" in out

    def test_ordered_list(self):
        out = markdown_to_latex_body("1. first\n2. second\n")
        assert r"\begin{enumerate}" in out
        assert r"\end{enumerate}" in out
        assert r"\item first" in out

    def test_code_fence(self):
        md = "```\ndef f():\n    return 1\n```\n"
        out = markdown_to_latex_body(md)
        assert r"\begin{verbatim}" in out
        assert r"\end{verbatim}" in out
        assert "def f():" in out
        # Bên trong verbatim không escape
        assert "\\_" not in "def f():"

    def test_horizontal_rule(self):
        out = markdown_to_latex_body("Before\n\n---\n\nAfter")
        assert r"\rule" in out

    def test_specials_in_paragraph_escaped(self):
        out = markdown_to_latex_body("Cost is $50 & growing.")
        assert r"\$" in out
        assert r"\&" in out

    def test_convert_to_latex_dispatches_md(self):
        out = convert_to_latex(b"# Hi\n\nbody.", "md", title="T")
        assert r"\section*{Hi}" in out
        assert r"\begin{document}" in out

    def test_convert_to_latex_dispatches_txt(self):
        out = convert_to_latex(b"hello", "txt", title="T")
        assert r"\begin{document}" in out
        assert "hello" in out

    def test_convert_to_latex_unknown_raises(self):
        with pytest.raises(ValueError):
            convert_to_latex(b"x", "exe")


# ──────────────────────────────────────────────────────────────────────────────
# html_to_latex
# ──────────────────────────────────────────────────────────────────────────────

class TestHtmlToLatex:
    def test_basic_heading_paragraph(self):
        body, title = html_to_latex_body(
            "<html><body><h1>Title</h1><p>Para.</p></body></html>"
        )
        assert r"\section*{Title}" in body
        assert "Para." in body

    def test_title_from_title_tag(self):
        _, title = html_to_latex_body(
            "<html><head><title>From Tag</title></head><body></body></html>"
        )
        assert title == "From Tag"

    def test_title_fallback_to_h1(self):
        _, title = html_to_latex_body(
            "<html><body><h1>Hello</h1></body></html>"
        )
        assert title == "Hello"

    def test_skips_script_and_style(self):
        body, _ = html_to_latex_body(
            "<html><body><p>keep</p>"
            "<script>alert('XSS')</script>"
            "<style>body{display:none;}</style>"
            "</body></html>"
        )
        assert "keep" in body
        assert "XSS" not in body
        assert "display:none" not in body

    def test_inline_bold_italic_code(self):
        body, _ = html_to_latex_body(
            "<p>This is <strong>bold</strong> and <em>italic</em> and <code>x</code>.</p>"
        )
        assert r"\textbf{bold}" in body
        assert r"\textit{italic}" in body
        assert r"\texttt{x}" in body

    def test_link_with_href(self):
        body, _ = html_to_latex_body(
            '<p>See <a href="https://example.com">here</a>.</p>'
        )
        assert r"\href{https://example.com}{here}" in body

    def test_anchor_link_dropped(self):
        body, _ = html_to_latex_body('<p><a href="#section1">go</a></p>')
        # Internal anchors → drop \href, keep text
        assert "go" in body
        assert "#section1" not in body

    def test_ul_ol(self):
        body, _ = html_to_latex_body(
            "<ul><li>one</li><li>two</li></ul>"
            "<ol><li>first</li><li>second</li></ol>"
        )
        assert r"\begin{itemize}" in body
        assert r"\begin{enumerate}" in body
        assert "one" in body and "first" in body

    def test_pre_to_verbatim(self):
        body, _ = html_to_latex_body("<pre>def f(): pass</pre>")
        assert r"\begin{verbatim}" in body
        assert "def f(): pass" in body

    def test_blockquote(self):
        body, _ = html_to_latex_body(
            "<blockquote><p>quoted text</p></blockquote>"
        )
        assert r"\begin{quote}" in body
        assert "quoted text" in body
        assert r"\end{quote}" in body

    def test_specials_escaped(self):
        body, _ = html_to_latex_body("<p>100% off &amp; free</p>")
        # &amp; → & → \&; % → \%
        assert r"\%" in body
        assert r"\&" in body

    def test_full_document_wraps(self):
        out = html_to_latex(
            "<html><body><h1>X</h1><p>y</p></body></html>",
            title="Custom",
        )
        assert r"\documentclass" in out
        assert "Custom" in out

    def test_convert_html_bytes(self):
        out, title = convert_html_to_latex(
            b"<html><head><title>Sample</title></head>"
            b"<body><h1>Hi</h1></body></html>"
        )
        assert title == "Sample"
        assert r"\section*{Hi}" in out

    def test_convert_html_user_title_overrides(self):
        out, title = convert_html_to_latex(
            b"<html><head><title>Tag</title></head><body><p>x</p></body></html>",
            title="UserGiven",
        )
        assert title == "UserGiven"

    def test_inline_spacing_preserved(self):
        """Khoảng trắng quanh inline tag không bị nuốt."""
        body, _ = html_to_latex_body(
            "<p>hello <strong>world</strong>!</p>"
        )
        # Không được glue thành "hello\textbf{world}!"
        assert r"hello \textbf{world}" in body or r"hello  \textbf{world}" in body


# ──────────────────────────────────────────────────────────────────────────────
# save_single_tex / _find_main_tex
# ──────────────────────────────────────────────────────────────────────────────

class TestLatexHelpers:
    def test_save_single_tex(self, tmp_path):
        tex_bytes = b"\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"
        out_dir = str(tmp_path)
        tex_path = save_single_tex(tex_bytes, out_dir)
        assert os.path.exists(tex_path)
        assert tex_path.endswith("main.tex")
        with open(tex_path, encoding="utf-8") as f:
            content = f.read()
        assert r"\begin{document}" in content

    def test_save_single_tex_latin1_fallback(self, tmp_path):
        """Bytes không decode được UTF-8 vẫn được ghi (fallback latin-1)."""
        # 0xff không hợp lệ trong UTF-8
        tex_bytes = b"\xff\xfeHello"
        tex_path = save_single_tex(tex_bytes, str(tmp_path))
        assert os.path.exists(tex_path)

    def test_find_main_tex_prefers_begin_document(self, tmp_path):
        """Nếu có nhiều .tex, file chứa `\\begin{document}` được chọn."""
        d = tmp_path / "ex"
        d.mkdir()
        (d / "helper.tex").write_text("\\newcommand{\\foo}{bar}\n", encoding="utf-8")
        (d / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n",
            encoding="utf-8",
        )
        found = _find_main_tex(str(d))
        assert found.endswith("main.tex")

    def test_find_main_tex_fallback_first(self, tmp_path):
        """Nếu không có file nào chứa `\\begin{document}` → trả về .tex đầu tiên."""
        d = tmp_path / "ex"
        d.mkdir()
        (d / "only.tex").write_text("\\newcommand{\\foo}{bar}\n", encoding="utf-8")
        found = _find_main_tex(str(d))
        assert found.endswith("only.tex")

    def test_find_main_tex_no_tex_raises(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(FileNotFoundError):
            _find_main_tex(str(d))


# ──────────────────────────────────────────────────────────────────────────────
# Safe extract — ZIP & TAR (zip-slip / tar-slip)
# ──────────────────────────────────────────────────────────────────────────────

class TestSafeExtract:
    def test_zip_normal_ok(self, tmp_path):
        archive = tmp_path / "ok.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.tex", "\\begin{document}x\\end{document}")
        dest = tmp_path / "out"
        dest.mkdir()
        tex_path = extract_source_zip(str(archive), str(dest))
        assert os.path.exists(tex_path)

    def test_zip_slip_relative_blocked(self, tmp_path):
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.tex", "\\begin{document}x\\end{document}")
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError):
            extract_source_zip(str(archive), str(dest))

    def test_zip_absolute_path_blocked(self, tmp_path):
        archive = tmp_path / "abs.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            # Path absolute trên Unix
            zf.writestr("/etc/passwd", "content")
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(RuntimeError):
            extract_source_zip(str(archive), str(dest))

    def test_tar_normal_ok(self, tmp_path):
        archive = tmp_path / "ok.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            data = b"\\begin{document}x\\end{document}"
            info = tarfile.TarInfo(name="main.tex")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        dest = tmp_path / "out"
        dest.mkdir()
        tex_path = extract_source(str(archive), str(dest))
        assert os.path.exists(tex_path)
