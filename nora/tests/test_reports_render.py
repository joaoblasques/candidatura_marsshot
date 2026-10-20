"""Report file-format rendering (PR.W / PR.X / PR.PPT)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.reports.render import SUPPORTED_REPORT_FORMATS, render_report


def _is_ooxml_zip(data: bytes) -> bool:
    # All OOXML files (.docx/.xlsx/.pptx) are zip containers starting with "PK".
    return data[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(data))


def test_docx_render_returns_valid_attachment():
    att = render_report("Q3 review\n\nRevenue up 20%.", "W")
    assert att.filename.endswith(".docx")
    assert att.content_type.endswith("wordprocessingml.document")
    assert _is_ooxml_zip(att.content)


def test_xlsx_render_returns_valid_attachment():
    att = render_report("row one\nrow two", "X")
    assert att.filename.endswith(".xlsx")
    assert _is_ooxml_zip(att.content)


def test_pptx_render_returns_valid_attachment():
    att = render_report("Title line\n\nA point\n\nAnother point", "PPT")
    assert att.filename.endswith(".pptx")
    assert _is_ooxml_zip(att.content)


def test_unknown_format_raises():
    assert "E" not in SUPPORTED_REPORT_FORMATS
    with pytest.raises(ValueError):
        render_report("x", "E")
