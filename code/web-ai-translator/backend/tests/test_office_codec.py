"""Tests cho OfficeEvalCodec — adapter eval-loop cho .docx (block office)."""

from types import SimpleNamespace


def office_blk(text, translated=""):
    # Block office duck-typed: chỉ .text + .translated_text
    return SimpleNamespace(text=text, translated_text=translated)


def test_office_codec_source_numbered():
    from app.office.eval_codec import OfficeEvalCodec
    src = OfficeEvalCodec().to_source_text([office_blk("Hello world."), office_blk("Second.")])
    assert "[1] Hello world." in src and "[2] Second." in src


def test_office_codec_apply_overwrites():
    from app.office.eval_codec import OfficeEvalCodec
    chunk = [office_blk("A", "old"), office_blk("B")]
    OfficeEvalCodec().apply("[1] Mới\n\n[2] Hai", chunk)
    assert chunk[0].translated_text == "Mới"   # GHI ĐÈ (khác parse_numbered_response)
    assert chunk[1].translated_text == "Hai"


def test_office_codec_evaluate_flags_untranslated():
    from app.office.eval_codec import OfficeEvalCodec
    eng = ("This is a clearly long English sentence that was not translated "
           "into Vietnamese at all here.")
    score, errors = OfficeEvalCodec().evaluate([office_blk(eng, eng)], None)
    assert score < 100
    assert errors


def test_office_codec_translate_prompt_and_extract():
    from app.office.eval_codec import OfficeEvalCodec
    codec = OfficeEvalCodec()
    assert "[1] hi" in codec.translate_prompt("[1] hi")
    assert codec.extract("```\nfoo\n```") == "foo"


def test_office_codec_satisfies_protocol():
    from app.pdf.eval_codec import EvalCodec
    from app.office.eval_codec import OfficeEvalCodec
    assert isinstance(OfficeEvalCodec(), EvalCodec)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
