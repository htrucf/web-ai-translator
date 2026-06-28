"""Office (.docx) translation pipeline.

In-place replacement of paragraph text via Playwright Gemini. Source format is
preserved 1:1 — the original ZIP is copied byte-for-byte and only translated
<w:t> text nodes are rewritten. Optional PDF preview via LibreOffice headless.
"""
