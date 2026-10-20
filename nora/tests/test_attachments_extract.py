"""Attachment content extraction (models + local text extraction)."""

from __future__ import annotations

from app.models import Attachment, ExtractedAttachment, ParsedThread


def test_attachment_carries_optional_content_and_size():
    a = Attachment(filename="x.pdf", content_type="application/pdf", size=12, content=b"abc")
    assert a.content == b"abc" and a.size == 12
    bare = Attachment(filename="y.txt")
    assert bare.content is None and bare.size is None


def test_extracted_attachment_defaults_and_thread_field():
    e = ExtractedAttachment(filename="x.pdf", status="read", text="hello")
    assert e.reason is None and e.truncated is False
    assert ParsedThread().attachment_extracts == []


import io  # noqa: E402
import zipfile  # noqa: E402

from app.attachments.extract import PER_FILE_BYTES, extract_attachments  # noqa: E402


def _txt(name, body):
    data = body.encode()
    return Attachment(filename=name, size=len(data), content=data)


def test_plain_text_is_read():
    [r] = extract_attachments([_txt("notes.txt", "Quarterly plan: grow 20%")])
    assert r.status == "read" and "Quarterly plan" in r.text


def test_unsupported_type_is_flagged():
    [r] = extract_attachments([Attachment(filename="a.zip", size=3, content=b"PK\x03")])
    assert r.status == "unreadable" and r.reason == "unsupported_type"


def test_missing_content_over_cap_is_too_large():
    [r] = extract_attachments([Attachment(filename="big.pdf", size=PER_FILE_BYTES + 1, content=None)])
    assert r.status == "unreadable" and r.reason == "too_large"


def test_empty_extraction_is_no_text_found():
    [r] = extract_attachments([_txt("blank.txt", "   ")])
    assert r.status == "unreadable" and r.reason == "no_text_found"


def test_corrupt_docx_degrades_not_raises():
    [r] = extract_attachments([Attachment(filename="bad.docx", size=4, content=b"not a zip")])
    assert r.status == "unreadable" and r.reason == "extract_failed"


def test_zip_bomb_docx_is_guarded():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"A" * 200_000_000)  # ~200MB uncompressed
    data = buf.getvalue()
    [r] = extract_attachments([Attachment(filename="bomb.docx", size=len(data), content=data)])
    assert r.status == "unreadable" and r.reason == "extract_failed"


def test_per_file_text_is_truncated():
    [r] = extract_attachments([_txt("long.txt", "x" * 30_000)])
    assert r.status == "read" and r.truncated is True and len(r.text) <= 20_000


def test_total_budget_exhaustion_surfaces_later_files_as_not_read():
    # Three full files exhaust the 50k total budget; the 4th must be flagged
    # unreadable (named in the manifest), not a silent empty "read".
    big = "x" * 20_000
    atts = [_txt(f"f{i}.txt", big) for i in range(3)] + [_txt("last.txt", "Deal closes Friday.")]
    results = extract_attachments(atts)
    assert results[-1].filename == "last.txt"
    assert results[-1].status == "unreadable"
    assert results[-1].reason == "budget_exhausted"
    assert results[-1].text == ""
