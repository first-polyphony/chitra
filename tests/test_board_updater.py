"""Tests for chitra.board_updater: backup + validate + rollback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chitra.board_updater import validate_facts, write_facts


def _valid_facts() -> dict[str, Any]:
    return {
        "snapshot_owner": "example-owner",
        "selfcheck": {"solid": "ok", "weak": "", "unsure": ""},
        "sessions": [{"id": "row-1", "host": "example-host", "state": {"cls": "st-work", "detail": "building"}}],
        "log": [{"chip_target": "row-1"}, {"chip_target": None}],
    }


def test_validate_facts_accepts_a_well_formed_document() -> None:
    result = validate_facts(_valid_facts())
    assert result.ok is True
    assert result.errors == []


def test_validate_facts_skips_owner_check_when_not_supplied() -> None:
    facts = _valid_facts()
    facts["snapshot_owner"] = "anything-at-all"
    result = validate_facts(facts)
    assert result.ok is True


def test_validate_facts_rejects_wrong_snapshot_owner_when_expected_owner_given() -> None:
    facts = _valid_facts()
    facts["snapshot_owner"] = "not-the-expected-owner"
    result = validate_facts(facts, expected_owner="example-owner")
    assert result.ok is False
    assert any("snapshot_owner" in e for e in result.errors)


def test_validate_facts_rejects_host_not_in_valid_hosts_when_given() -> None:
    facts = _valid_facts()
    facts["sessions"][0]["host"] = "unknown-host"
    result = validate_facts(facts, valid_hosts={"example-host"})
    assert result.ok is False
    assert any("host" in e for e in result.errors)


def test_validate_facts_rejects_bad_state_cls() -> None:
    facts = _valid_facts()
    facts["sessions"][0]["state"]["cls"] = "st-bogus"
    result = validate_facts(facts)
    assert result.ok is False
    assert any("state.cls" in e for e in result.errors)


def test_validate_facts_rejects_bad_state_cls_unless_overridden_via_valid_state_cls() -> None:
    facts = _valid_facts()
    facts["sessions"][0]["state"]["cls"] = "custom-deployment-state"
    default_result = validate_facts(facts)
    assert default_result.ok is False
    assert any("state.cls" in e for e in default_result.errors)

    overridden_result = validate_facts(facts, valid_state_cls={"custom-deployment-state"})
    assert overridden_result.ok is True


def test_validate_facts_rejects_dangling_chip_target() -> None:
    facts = _valid_facts()
    facts["log"].append({"chip_target": "row-does-not-exist"})
    result = validate_facts(facts)
    assert result.ok is False
    assert any("chip_target" in e for e in result.errors)


def test_write_facts_backs_up_existing_file_before_overwrite(tmp_path: Path) -> None:
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    existing = board_dir / "facts.json"
    existing.write_text(json.dumps({"snapshot_owner": "example-owner", "note": "old"}), encoding="utf-8")

    result = write_facts(_valid_facts(), board_dir=board_dir)

    assert result["ok"] is True
    assert result["backup"] is not None
    backup_path = Path(result["backup"])
    assert backup_path.exists()
    assert json.loads(backup_path.read_text(encoding="utf-8"))["note"] == "old"
    written = json.loads(existing.read_text(encoding="utf-8"))
    assert written["snapshot_owner"] == "example-owner"


def test_write_facts_rolls_back_on_invalid_input(tmp_path: Path) -> None:
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    existing = board_dir / "facts.json"
    existing.write_text(json.dumps({"snapshot_owner": "example-owner", "note": "keep-me"}), encoding="utf-8")

    bad_facts = _valid_facts()
    bad_facts["snapshot_owner"] = "wrong"
    result = write_facts(bad_facts, board_dir=board_dir, expected_owner="example-owner")

    assert result["ok"] is False
    # The pre-existing file must be untouched — no rollback needed because
    # nothing was ever written over it.
    assert json.loads(existing.read_text(encoding="utf-8"))["note"] == "keep-me"


def test_write_facts_creates_board_dir_if_missing(tmp_path: Path) -> None:
    board_dir = tmp_path / "nested" / "board"
    result = write_facts(_valid_facts(), board_dir=board_dir)
    assert result["ok"] is True
    assert (board_dir / "facts.json").exists()
    assert result["backup"] is None
