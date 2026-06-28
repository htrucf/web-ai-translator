"""Tests cho EvalCodec — adapter định dạng của vòng khép kín (PDF codec)."""

from types import SimpleNamespace


def blk(text, translated, translatable=True, page=0, idx=0):
    return SimpleNamespace(
        text=text, translated_text=translated,
        is_translatable=translatable, page_num=page, block_idx=idx,
    )


def test_pdf_codec_source_text_numbered():
    from app.pdf.eval_codec import PdfEvalCodec
    chunk = [blk("Hello world.", ""), blk("Second block.", "")]
    src = PdfEvalCodec().to_source_text(chunk)
    assert "[1] Hello world." in src and "[2] Second block." in src


def test_pdf_codec_evaluate_flags_untranslated():
    from app.pdf.eval_codec import PdfEvalCodec
    eng = ("This is a clearly long English sentence that was not translated "
           "into Vietnamese at all here.")
    score, errors = PdfEvalCodec().evaluate([blk(eng, eng)], None)
    assert score < 100
    assert errors


def test_pdf_codec_apply_roundtrip():
    from app.pdf.eval_codec import PdfEvalCodec
    chunk = [blk("This is the original.", "This is the original.")]
    PdfEvalCodec().apply("[1] Đây là bản dịch.", chunk)
    assert chunk[0].translated_text == "Đây là bản dịch."


def test_pdf_codec_satisfies_protocol():
    from app.pdf.eval_codec import EvalCodec, PdfEvalCodec
    assert isinstance(PdfEvalCodec(), EvalCodec)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
