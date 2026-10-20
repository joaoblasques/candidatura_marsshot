"""Showcase Drive seed corpus is well-formed and demo-ready."""

from __future__ import annotations

import io
import zipfile

from scripts.seed_showcase_drive import build_seed_corpus


def test_corpus_builds_with_names_and_bytes():
    corpus = build_seed_corpus()
    assert len(corpus) >= 20  # a realistic "many files" Drive
    names = [n for n, _, _ in corpus]
    assert len(set(names)) == len(names)  # no duplicate names
    assert all(content for _, content, _ in corpus)  # every file has bytes


def test_corpus_has_four_quarterly_earnings_for_disambiguation():
    names = [n for n, _, _ in build_seed_corpus()]
    earnings = [n for n in names if "earnings" in n.lower() and n[:2].upper().startswith("Q")]
    assert len(earnings) == 4  # Q1..Q4 -> "earnings" is ambiguous, "Q4 earnings" resolves


def test_office_files_are_valid_ooxml():
    for name, content, _ in build_seed_corpus():
        if name.endswith((".docx", ".xlsx", ".pptx")):
            assert content[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(content)), name
