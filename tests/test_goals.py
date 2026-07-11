"""Tests for the deterministic goal-state store and command interface."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from chitra.artifacts import ARTIFACT_URL_PREFIX, ArtifactRecord, upsert_artifact
from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    GoalStatus,
    GoalValidationError,
    add_ask,
    close_goal,
    due_goals,
    get_goal,
    hold_goal,
    list_goals,
    load_goals,
    main,
    resolve_ask,
    resume_goal,
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
        "needs": "",
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
        needs=values["needs"],
    )


def test_store_round_trip_and_atomic_write(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record(needs="you: run the interview"))

    assert stored.created_at
    assert stored.updated_at
    assert get_goal(tmp_path, stored.session_ref) == stored
    assert list_goals(tmp_path) == [stored]
    assert load_goals(tmp_path) == [stored]
    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads((tmp_path / "goals.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "chitra.goals.v1"
    assert payload["goals"][0]["needs"] == "you: run the interview"


def test_upsert_preserves_created_timestamp_and_recomputes_updated(tmp_path: Path) -> None:
    first = upsert_goal(tmp_path, _record())
    second = upsert_goal(tmp_path, _record(now="running checks", updated_at="not-kept"))

    assert second.created_at == first.created_at
    assert second.updated_at != "not-kept"
    assert get_goal(tmp_path, first.session_ref) == second


def test_open_asks_round_trip_and_set_preserves_them_until_explicitly_cleared(tmp_path: Path) -> None:
    first = upsert_goal(tmp_path, _record())
    add_ask(tmp_path, first.session_ref, "1. Should we merge the Folio tenancy change?")

    revised = upsert_goal(tmp_path, _record(status="blocked", now="awaiting ruling"))
    assert revised.open_asks == ("1. Should we merge the Folio tenancy change?",)
    assert json.loads((tmp_path / "goals.json").read_text(encoding="utf-8"))["goals"][0]["open_asks"] == list(revised.open_asks)

    cleared = upsert_goal(tmp_path, _record(status="working"), clear_open_asks=True)
    assert cleared.open_asks == ()


def test_add_ask_deduplicates_and_resolve_supports_text_index_and_all(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())
    one = "1. Approve the first irreversible step?"
    two = "2. Choose the tenancy boundary."

    assert add_ask(tmp_path, stored.session_ref, one).open_asks == (one,)
    assert add_ask(tmp_path, stored.session_ref, one).open_asks == (one,)
    assert add_ask(tmp_path, stored.session_ref, two).open_asks == (one, two)
    assert resolve_ask(tmp_path, stored.session_ref, ask=one).open_asks == (two,)
    assert resolve_ask(tmp_path, stored.session_ref, index=0).open_asks == ()

    add_ask(tmp_path, stored.session_ref, one)
    add_ask(tmp_path, stored.session_ref, two)
    assert resolve_ask(tmp_path, stored.session_ref, all=True).open_asks == ()


def test_resolve_ask_rejects_ambiguous_missing_and_out_of_range_selectors(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())
    with pytest.raises(ValueError, match="exactly one"):
        resolve_ask(tmp_path, stored.session_ref)
    with pytest.raises(ValueError, match="out of range"):
        resolve_ask(tmp_path, stored.session_ref, index=0)
    with pytest.raises(GoalNotFoundError):
        add_ask(tmp_path, "missing:lane", "1. Decide?")


def test_load_old_record_without_open_asks_is_backward_compatible(tmp_path: Path) -> None:
    payload = {
        "schema": "chitra.goals.v1",
        "updated_at": "2026-07-10T00:00:00+00:00",
        "goals": [
            {
                "session_ref": "host:lane:0.0",
                "goal": "Keep a fully backwards compatible persistent goal store.",
                "done_when": "The stored old record loads without migration.",
                "source": "main",
                "status": "working",
                "now": "loading",
                "last_verified": "",
                "created_at": "",
                "updated_at": "",
            }
        ],
    }
    (tmp_path / "goals.json").write_text(json.dumps(payload), encoding="utf-8")

    assert load_goals(tmp_path)[0].open_asks == ()
    assert load_goals(tmp_path)[0].needs == ""
    assert load_goals(tmp_path)[0].hold_reason == ""
    assert load_goals(tmp_path)[0].resume_at == ""


def test_hold_resume_due_and_upsert_hold_metadata_preservation(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())
    held = hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at="2026-07-10T12:00:00Z")

    assert held.status == "held"
    assert held.goal == stored.goal
    assert held.hold_reason == "rate-limit:5h"
    assert held.resume_at == "2026-07-10T12:00:00Z"

    revised = upsert_goal(tmp_path, replace(held, now="waiting", hold_reason="", resume_at=""))
    assert revised.hold_reason == "rate-limit:5h"
    assert revised.resume_at == "2026-07-10T12:00:00Z"
    assert due_goals(tmp_path, now=datetime(2026, 7, 10, 11, 59, tzinfo=UTC)) == []
    assert due_goals(tmp_path, now=datetime(2026, 7, 10, 12, tzinfo=UTC)) == [revised]

    resumed = resume_goal(tmp_path, stored.session_ref)
    assert resumed.status == "working"
    assert resumed.hold_reason == ""
    assert resumed.resume_at == ""


def test_hold_resume_due_errors_and_cli_validation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stored = upsert_goal(tmp_path, _record())
    with pytest.raises(GoalNotFoundError):
        hold_goal(tmp_path, "missing:lane", reason="operator")
    with pytest.raises(ValueError, match="not held"):
        resume_goal(tmp_path, stored.session_ref)

    hold_goal(tmp_path, stored.session_ref, reason="operator")
    second = upsert_goal(tmp_path, _record(session_ref="host:second:0.0"))
    hold_goal(tmp_path, second.session_ref, reason="rate-limit:7d", resume_at="2026-07-10T10:00:00+00:00")
    hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at="2026-07-10T09:00:00+00:00")
    assert [record.session_ref for record in due_goals(tmp_path, now=datetime(2026, 7, 10, 11, tzinfo=UTC))] == [
        stored.session_ref,
        second.session_ref,
    ]

    assert main(["hold", "--root", str(tmp_path), "--session-ref", second.session_ref, "--reason", "operator", "--resume-at", "nope"]) == 1
    assert "ISO8601" in capsys.readouterr().err


def test_goal_cli_outputs_open_asks_needs_and_scan_recording_requires_a_lane(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stored = upsert_goal(tmp_path, _record(needs="you: approve release"))
    add_ask(tmp_path, stored.session_ref, "1. Approve release?")

    assert main(["get", "--root", str(tmp_path), "--session-ref", stored.session_ref]) == 0
    output = capsys.readouterr().out
    assert '"open_asks": [' in output
    assert '"needs": "you: approve release"' in output
    assert main(["list", "--root", str(tmp_path), "--json"]) == 0
    output = capsys.readouterr().out
    assert '"open_asks": [' in output
    assert '"needs": "you: approve release"' in output
    assert main(["list", "--root", str(tmp_path)]) == 0
    assert "1. Approve release?" in capsys.readouterr().out
    assert main(["scan-asks", "--transcript", str(tmp_path / "none.jsonl"), "--record"]) == 1
    assert "--record requires --session-ref" in capsys.readouterr().err


def test_goal_cli_seeds_clears_and_scans_open_asks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _record()
    set_args = [
        "set",
        "--root",
        str(tmp_path),
        "--session-ref",
        record.session_ref,
        "--goal",
        record.goal,
        "--done-when",
        record.done_when,
        "--source",
        record.source,
    ]
    assert main([*set_args, "--open-ask", "1. Seed one?", "--open-ask", "2. Seed two."]) == 0
    seeded = get_goal(tmp_path, record.session_ref)
    assert seeded is not None
    assert seeded.open_asks == ("1. Seed one?", "2. Seed two.")

    transcript = tmp_path / "lane.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"content": "Awaiting ruling:\n1. Scan this exact ask?"}}) + "\n",
        encoding="utf-8",
    )
    assert (
        main(
            ["scan-asks", "--root", str(tmp_path), "--transcript", str(transcript), "--session-ref", record.session_ref, "--record"]
        )
        == 0
    )
    assert "1. Scan this exact ask?" in capsys.readouterr().out
    scanned = get_goal(tmp_path, record.session_ref)
    assert scanned is not None
    assert scanned.open_asks[-1] == "1. Scan this exact ask?"

    assert main([*set_args, "--clear-asks"]) == 0
    cleared = get_goal(tmp_path, record.session_ref)
    assert cleared is not None
    assert cleared.open_asks == ()


def test_goal_cli_set_preserves_needs_when_omitted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _record()
    set_args = [
        "set",
        "--root",
        str(tmp_path),
        "--session-ref",
        record.session_ref,
        "--goal",
        record.goal,
        "--done-when",
        record.done_when,
        "--source",
        record.source,
    ]

    assert main([*set_args, "--needs", "you: run the interview"]) == 0
    capsys.readouterr()
    assert main([*set_args, "--status", "blocked"]) == 0
    stored = get_goal(tmp_path, record.session_ref)

    assert stored is not None
    assert stored.needs == "you: run the interview"


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


def test_roster_command_reads_unreviewed_artifacts_from_the_shared_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = upsert_artifact(
        tmp_path,
        ArtifactRecord(
            url=f"{ARTIFACT_URL_PREFIX}operator-copyable-link",
            title="Operator artifact",
            kind="page",
            source="tophand:/var/lib/chitra/artifact.html",
        ),
    )
    capsys.readouterr()

    assert main(["roster", "--root", str(tmp_path)]) == 0
    rendered = capsys.readouterr().out

    assert f"{artifact.title} — {artifact.url}" in rendered
