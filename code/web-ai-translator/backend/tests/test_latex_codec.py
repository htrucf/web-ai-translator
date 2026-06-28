"""Tests cho LatexEvalCodec — adapter eval-loop cho chunk LaTeX (chuỗi)."""


def test_latex_unit_holds_translation():
    from app.services.latex_eval_codec import LatexUnit
    u = LatexUnit("\\section{Intro}")
    assert u.source == "\\section{Intro}" and u.translated_text == ""


def test_latex_codec_apply_strips_fence():
    from app.services.latex_eval_codec import LatexEvalCodec, LatexUnit
    codec = LatexEvalCodec()
    u = LatexUnit("This is the original English text here.")
    codec.apply("```\n\\section{Giới thiệu} Đã dịch.\n```", u)
    assert u.translated_text == "\\section{Giới thiệu} Đã dịch."
    assert codec.to_translation_text(u) == "\\section{Giới thiệu} Đã dịch."


def test_latex_codec_evaluate_scores():
    from app.services.latex_eval_codec import LatexEvalCodec, LatexUnit
    u = LatexUnit("A long English paragraph that was clearly not translated into Vietnamese here.")
    score, errors = LatexEvalCodec().evaluate(u, None)   # chưa dịch → điểm thấp
    assert score < 100
    u.translated_text = "Một đoạn văn tiếng Việt đầy đủ đã được dịch hoàn chỉnh ở đây."
    score2, _ = LatexEvalCodec().evaluate(u, None)
    assert score2 >= score


def test_latex_codec_translate_prompt_keeps_source():
    from app.services.latex_eval_codec import LatexEvalCodec
    p = LatexEvalCodec().translate_prompt("\\textbf{Hello}")
    assert "\\textbf{Hello}" in p and "LaTeX" in p


def test_latex_codec_satisfies_protocol():
    from app.pdf.eval_codec import EvalCodec
    from app.services.latex_eval_codec import LatexEvalCodec
    assert isinstance(LatexEvalCodec(), EvalCodec)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
