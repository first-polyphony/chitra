"""Tests for the deterministic goal-state store and command interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    GoalStatus,
    GoalValidationError,
    close_goal,
    get_goal,
    list_goals,
    load_goals,
    main,
    session_host,
    session_name,
    upsert_goal,
    validate_goal,
)


def _record(**changes: str) -> GoalRecord:
    values: dict[str, str] = {
        "session_ref": "tophand:f2-77:0.0",
        "goal": "Ship the tested deterministic goals store safely.",
        "done_when": "The full suite and static checks pass.",
        "source": "feat/goal-store-roster",
        "status": "working",
        "now": "writing tests",
        "last_verified": "",
    }
    values.update(changes)
    return GoalRecord(
        session_ref=values["session_ref"],
        goal=values["goal"],
        done_when=values["done_when"],
        source=values["source"],
        status=cast(GoalStatus, values["status"]),
        now=values["now"],
        last_verified=values["last_verified"],
    )


def test_store_round_trip_and_atomic_write(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())

    assert stored.created_at
    assert stored.updated_at
    assert get_goal(tmp_path, stored.session_ref) == stored
    assert list_goals(tmp_path) == [stored]
    assert load_goals(tmp_path) == [stored]
    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads((tmp_path / "goals.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "chitra.goals.v1"


def test_upsert_preserves_created_timestamp_and_recomputes_updated(tmp_path: Path) -> None:
    first = upsert_goal(tmp_path, _record())
    second = upsert_goal(tmp_path, _record(now="running checks", updated_at="not-kept"))

    assert second.created_at == first.created_at
    assert second.updated_at != "not-kept"
    assert get_goal(tmp_path, first.session_ref) == second


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_record(goal=""), "goal must be non-empty"),
        (_record(goal="Too short"), "at least six words"),
        (_record(done_when=""), "done_when must be non-empty"),
    ],
)
def test_upsert_rejects_invalid_records(tmp_path: Path, record: GoalRecord, message: str) -> None:
    with pytest.raises(GoalValidationError, match=message):
        upsert_goal(tmp_path, record)
    assert not (tmp_path / "goals.json").exists()


def test_close_removes_record_and_raises_for_absent_goal(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())

    assert close_goal(tmp_path, stored.session_ref) == stored
    assert list_goals(tmp_path) == []
    with pytest.raises(GoalNotFoundError):
        close_goal(tmp_path, stored.session_ref)


@pytest.mark.parametrize(
    ("session_ref", "expected_host", "expected_name"),
    [
        ("tophand:f2-77:0.0", "tophand", "f2-77"),
        ("tophand:f2-77", "tophand", "f2-77"),
        ("lane-token", "lane-token", "lane-token"),
        ("tophand:", "tophand", "tophand"),
    ],
)
def test_session_ref_helpers_degrade_gracefully(session_ref: str, expected_host: str, expected_name: str) -> None:
    assert session_host(session_ref) == expected_host
    assert session_name(session_ref) == expected_name


def test_validate_goal_reports_each_doctrine_violation() -> None:
    assert validate_goal(_record()) == []
    issues = validate_goal(_record(goal="brief", done_when="", source="", status="not-a-status"))

    assert any("six words" in issue for issue in issues)
    assert "done_when must be non-empty" in issues
    assert "source must be non-empty" in issues
    assert any(issue.startswith("status must be") for issue in issues)


def test_roster_command_works_with_an_empty_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["roster", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "no lanes recorded"
