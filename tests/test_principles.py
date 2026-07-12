"""Tests for deterministic compilation and scoped principle lookup."""

from __future__ import annotations

from pathlib import Path

import pytest

from chitra.principles import (
    DEFAULT_INDEX_PATH,
    DEFAULT_SOURCE_PATH,
    PrinciplesError,
    compile_index,
    load_index,
    lookup_principles,
)


def test_checked_in_index_is_current_and_reproducible() -> None:
    checked_in = load_index()
    rebuilt = compile_index()

    assert rebuilt == checked_in
    assert checked_in.corpus_id.startswith("sha256:")
    assert len(checked_in.principles) == 20
    assert all(principle.citation.content_sha256 for principle in checked_in.principles)


def test_lookup_is_deterministic_and_citation_bearing() -> None:
    index = load_index()

    first = lookup_principles(
        index,
        "Should this new service use structlog logging with keyword context?",
        scopes=("engineering",),
        answer_category="architecture",
    )
    second = lookup_principles(
        index,
        "Should this new service use structlog logging with keyword context?",
        scopes=("engineering",),
        answer_category="architecture",
    )

    assert first == second
    assert first[0].principle_id == "A08"
    assert first[0].score >= 0.6
    assert first[0].citation.path.endswith("B-principles-corpus.md")
    assert first[0].citation.line_start == 77


def test_load_rejects_index_when_reviewed_source_changed(tmp_path: Path) -> None:
    source = tmp_path / "principles.source.yaml"
    source.write_bytes(DEFAULT_SOURCE_PATH.read_bytes() + b"\n")

    with pytest.raises(PrinciplesError, match="stale"):
        load_index(DEFAULT_INDEX_PATH, source_path=source)
