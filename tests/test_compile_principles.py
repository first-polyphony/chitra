from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.compile_principles import compile_manifest

REPO_ROOT = Path(__file__).parents[1]


def test_checked_in_lock_matches_compiler_output() -> None:
    compiled = compile_manifest(REPO_ROOT / "src/chitra/principles.source.yaml")
    checked_in = json.loads((REPO_ROOT / "src/chitra/principles.lock.json").read_text(encoding="utf-8"))

    assert compiled == checked_in
    assert compiled["reproducible"] is True
    assert compiled["corpus_id"].startswith("sha256:")


def test_compiler_rejects_duplicate_principle_ids(tmp_path: Path) -> None:
    source = (REPO_ROOT / "src/chitra/principles.source.yaml").read_text(encoding="utf-8")
    first = source.index("  - principle_id: G01")
    second = source.index("  - principle_id: G02")
    duplicated = source[:second] + source[first:second] + source[second:]
    path = tmp_path / "duplicate.yaml"
    path.write_text(duplicated, encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        compile_manifest(path)
