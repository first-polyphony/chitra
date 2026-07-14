"""Tests for the deterministic goal-state store and command interface."""

from __future__ import annotations

import json
import multiprocessing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from chitra.artifacts import ARTIFACT_URL_PREFIX, ArtifactRecord, upsert_artifact
from chitra.goals import (
    GoalNotFoundError,
    GoalRecord,
    GoalRedirectRequiredError,
    GoalStatus,
    GoalValidationError,
    add_ask,
    check_specification,
    close_goal,
    due_goals,
    get_goal,
    hold_goal,
    list_goals,
    load_goals,
    main,
    redirect_goal,
    resolve_ask,
    resume_goal,
    session_host,
    session_name,
    update_now,
    upsert_goal,
    validate_goal,
)


def _mp_upsert_new_lane(root_str: str, session_ref: str) -> None:
    """Module-level so it is a valid multiprocessing target (fork-safe)."""
    upsert_goal(
        Path(root_str),
        GoalRecord(
            session_ref=session_ref,
            goal="Ship the tested deterministic goals store safely under load.",
            done_when="The full suite and static checks pass.",
            source="task-file:/tmp/goal-store.md",
            status="working",
        ),
    )


def _mp_add_ask(root_str: str, session_ref: str, ask: str) -> None:
    add_ask(Path(root_str), session_ref, ask)


def _record(**changes: str) -> GoalRecord:
    values: dict[str, str] = {
        "session_ref": "tophand:f2-77:0.0",
        "goal": "Ship the tested deterministic goals store safely.",
        "done_when": "The full suite and static checks pass.",
        "source": "task-file:/tmp/goal-store.md",
        "status": "working",
        "intent": "Safely deliver a deterministic persistent goals store for operators.",
        "scope": "Goal storage, CLI behavior, and tests only.",
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
        intent=values["intent"],
        scope=values["scope"],
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
    assert payload["goals"][0]["goal_version"] == 1
    assert payload["goals"][0]["goal_history"] == []


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


def test_load_old_record_without_optional_fields_is_backward_compatible(tmp_path: Path) -> None:
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

    record = load_goals(tmp_path)[0]
    assert record.open_asks == ()
    assert record.needs == ""
    assert record.hold_reason == ""
    assert record.resume_at == ""
    assert record.intent == ""
    assert record.scope == ""
    assert record.goal_version == 1
    assert record.goal_history == ()
    stored = upsert_goal(tmp_path, record)
    assert load_goals(tmp_path) == [stored]
    assert stored.to_dict()["goal_history"] == []


@pytest.mark.parametrize(
    ("field", "invalid", "message"),
    [
        ("intent", 1, "intent must be a string"),
        ("scope", 1, "scope must be a string"),
        ("goal_version", True, "goal_version must be an integer"),
        ("goal_history", "not-a-list", "goal_history must be a list"),
        (
            "goal_history",
            [{"goal": "g", "done_when": "d", "intent": "i", "scope": "s", "revised_at": "t"}],
            "goal_history entries must contain strategic prior values",
        ),
        (
            "goal_history",
            [{"goal": "g", "done_when": "d", "intent": "i", "scope": "s", "revised_at": "t", "reason": 1}],
            "goal_history entries must contain strings",
        ),
    ],
)
def test_new_optional_goal_fields_are_strictly_validated(tmp_path: Path, field: str, invalid: object, message: str) -> None:
    record_payload = _record().to_dict()
    record_payload[field] = invalid
    payload = {"schema": "chitra.goals.v1", "goals": [record_payload]}
    (tmp_path / "goals.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_goals(tmp_path)


def test_plain_upsert_preserves_strategic_version_and_history(tmp_path: Path) -> None:
    first = upsert_goal(tmp_path, _record())
    redirected = redirect_goal(tmp_path, first.session_ref, reason="operator narrowed delivery", scope="Goal storage and tests only.")

    revised = upsert_goal(
        tmp_path,
        replace(redirected, now="running checks", goal_version=99, goal_history=()),
    )

    assert revised.now == "running checks"
    assert revised.goal_version == 2
    assert revised.goal_history == redirected.goal_history


@pytest.mark.parametrize(
    "change",
    [
        {"goal": "Ship a revised deterministic goals store safely now."},
        {"done_when": "The redirected complete suite and static checks pass."},
        {"intent": "Safely deliver a changed deterministic persistent goals store for operators."},
        {"scope": "Goal storage, CLI behavior, tests, and docs only."},
        {"source": "branch:feat/revised-goal"},
    ],
)
def test_upsert_rejects_any_strategic_change(tmp_path: Path, change: dict[str, str]) -> None:
    first = upsert_goal(tmp_path, _record())

    with pytest.raises(GoalRedirectRequiredError, match="redirect"):
        upsert_goal(tmp_path, replace(first, **change))


def test_redirect_records_prior_strategic_values_and_preserves_tactical_state(tmp_path: Path) -> None:
    first = upsert_goal(tmp_path, _record(open_asks=""))
    asked = add_ask(tmp_path, first.session_ref, "1. Approve the redirect?")
    redirected = redirect_goal(
        tmp_path,
        first.session_ref,
        reason="operator expanded the stated scope",
        goal="Ship a revised deterministic goals store safely now.",
        scope="Goal storage, CLI behavior, tests, and docs only.",
    )

    assert redirected.goal_version == 2
    assert redirected.goal_history == (
        {
            "goal": asked.goal,
            "done_when": asked.done_when,
            "intent": asked.intent,
            "scope": asked.scope,
            "revised_at": redirected.updated_at,
            "reason": "operator expanded the stated scope",
        },
    )
    assert redirected.now == asked.now
    assert redirected.open_asks == asked.open_asks
    assert redirected.needs == asked.needs


def test_redirect_requires_existing_record_reason_and_real_strategic_change(tmp_path: Path) -> None:
    with pytest.raises(GoalNotFoundError):
        redirect_goal(tmp_path, "missing:lane", reason="operator changed direction")
    stored = upsert_goal(tmp_path, _record())
    with pytest.raises(ValueError, match="reason"):
        redirect_goal(tmp_path, stored.session_ref, reason=" ", goal="A revised goal with enough words to validate.")
    with pytest.raises(ValueError, match="must change"):
        redirect_goal(tmp_path, stored.session_ref, reason="operator reviewed it")
    with pytest.raises(GoalValidationError, match="goal must be non-empty"):
        redirect_goal(tmp_path, stored.session_ref, reason="operator removed the goal", goal="")


def test_update_now_is_tactical_only_and_requires_an_existing_record(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())
    updated = update_now(tmp_path, stored.session_ref, now="validating", status="blocked", last_verified="2026-07-11T00:00:00Z")

    assert updated.now == "validating"
    assert updated.status == "blocked"
    assert updated.last_verified == "2026-07-11T00:00:00Z"
    assert updated.goal == stored.goal
    assert updated.intent == stored.intent
    unchanged = update_now(tmp_path, stored.session_ref)
    assert (unchanged.now, unchanged.status, unchanged.last_verified) == (
        updated.now,
        updated.status,
        updated.last_verified,
    )
    with pytest.raises(GoalNotFoundError):
        update_now(tmp_path, "missing:lane", now="nothing")


@pytest.mark.parametrize(
    ("record", "issue"),
    [
        (_record(intent="too short"), "intent must be"),
        (_record(goal="too short"), "goal must contain"),
        (_record(done_when="too short"), "done_when must be"),
        (_record(scope="too short"), "scope must be"),
        (_record(source="screen"), "source must start"),
    ],
)
def test_check_specification_reports_each_independent_criterion(record: GoalRecord, issue: str) -> None:
    assert check_specification(record) == [next(item for item in check_specification(record) if issue in item)]


def test_check_specification_accepts_a_fully_specified_goal() -> None:
    assert check_specification(_record()) == []


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
        main(["scan-asks", "--root", str(tmp_path), "--transcript", str(transcript), "--session-ref", record.session_ref, "--record"]) == 0
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


def test_goal_cli_set_redirect_now_and_check_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        "--intent",
        record.intent,
        "--scope",
        record.scope,
    ]
    assert main(set_args) == 0
    capsys.readouterr()
    assert main([*set_args, "--goal", "Ship a revised deterministic goals store safely now."]) == 1
    assert "chitra-goals redirect --reason" in capsys.readouterr().err

    assert (
        main(
            [
                "redirect",
                "--root",
                str(tmp_path),
                "--session-ref",
                record.session_ref,
                "--reason",
                "operator redirected the strategic objective",
                "--goal",
                "Ship a revised deterministic goals store safely now.",
            ]
        )
        == 0
    )
    assert '"goal_version": 2' in capsys.readouterr().out
    assert main(["now", "--root", str(tmp_path), "--session-ref", record.session_ref, "--now", "running tests"]) == 0
    assert '"now": "running tests"' in capsys.readouterr().out
    assert main(["check", "--root", str(tmp_path), "--session-ref", record.session_ref]) == 0
    assert capsys.readouterr().out.strip() == "well-specified"

    assert (
        main(
            [
                "redirect",
                "--root",
                str(tmp_path),
                "--session-ref",
                record.session_ref,
                "--reason",
                "operator retained a short captured intent",
                "--intent",
                "brief intent",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["check", "--root", str(tmp_path), "--session-ref", record.session_ref]) == 1
    assert "intent must be" in capsys.readouterr().out

    assert main(["check", "--root", str(tmp_path), "--session-ref", "missing:lane"]) == 1
    assert "missing:lane" in capsys.readouterr().err
    assert main(["redirect", "--root", str(tmp_path), "--session-ref", record.session_ref, "--reason", "no revision"]) == 1
    assert "must change" in capsys.readouterr().err
    assert main(["now", "--root", str(tmp_path), "--session-ref", "missing:lane"]) == 1
    assert "missing:lane" in capsys.readouterr().err


def test_goal_cli_guidance_happy_and_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from chitra.policy_config import POLICY_CONFIG_ENV_VAR

    document = tmp_path / "decisions.md"
    document.write_text("# Canonical decisions\n", encoding="utf-8")
    policy = tmp_path / "policy.yaml"
    policy.write_text(f"guidance:\n  canonical_decisions:\n    default: {document}\n", encoding="utf-8")
    monkeypatch.setenv(POLICY_CONFIG_ENV_VAR, str(policy))

    assert main(["guidance", "--cwd", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == str(document)
    assert main(["guidance", "--cwd", str(tmp_path), "--show"]) == 0
    assert capsys.readouterr().out == "# Canonical decisions\n"

    monkeypatch.delenv(POLICY_CONFIG_ENV_VAR)
    assert main(["guidance", "--cwd", str(tmp_path)]) == 1
    assert "no guidance is configured" in capsys.readouterr().err
    policy.write_text("guidance:\n  canonical_decisions:\n    default: /missing/decisions.md\n", encoding="utf-8")
    monkeypatch.setenv(POLICY_CONFIG_ENV_VAR, str(policy))
    assert main(["guidance", "--cwd", str(tmp_path)]) == 1
    assert "configured guidance file is missing" in capsys.readouterr().err


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


def test_roster_command_reads_unreviewed_artifacts_from_the_shared_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    artifact = upsert_artifact(
        tmp_path,
        ArtifactRecord(
            url=f"{ARTIFACT_URL_PREFIX}operator-copyable-link",
            title="Operator artifact",
            kind="page",
            source="tophand:/var/lib/chitra/artifact.html",
            brief=(
                "What was built: An operator-facing artifact roster entry.\n"
                "What it does: It exposes the full copyable artifact link.\n"
                "Does it actually work: Roster probe status=200 with 1 rendered link; /tmp/roster-proof.json."
            ),
        ),
    )
    capsys.readouterr()

    assert main(["roster", "--root", str(tmp_path)]) == 0
    rendered = capsys.readouterr().out

    assert f"{artifact.title} — {artifact.url}" in rendered


# --- concurrent-writer lost-update regression (SOL finding #9) -------------


def test_concurrent_writers_adding_different_lanes_do_not_lose_each_other(tmp_path: Path) -> None:
    """Two-process lost-update regression, matching the review's exact
    scenario: 'Monitor A reads goals [A, B]... A writes [B, A']; B writes
    [A, B']. Whichever os.replace() runs last silently erases the other's
    mutation.' N real OS processes each add their OWN lane concurrently to
    the SAME shared document; every one must survive -- none silently
    clobbered by a losing race on the read-modify-write window."""
    ctx = multiprocessing.get_context("fork")
    session_refs = [f"host:lane-{i}:0.0" for i in range(20)]
    procs = [ctx.Process(target=_mp_upsert_new_lane, args=(str(tmp_path), ref)) for ref in session_refs]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    stored_refs = {record.session_ref for record in load_goals(tmp_path)}
    assert stored_refs == set(session_refs)


def test_concurrent_writers_adding_asks_to_the_same_lane_do_not_lose_each_other(tmp_path: Path) -> None:
    """The same lost-update race, but on ONE lane's own record: several
    processes concurrently add_ask() to the SAME session_ref. Without
    serializing the read-modify-write transaction, each process's
    read-then-append can race another's, and whichever write lands last
    silently drops the other's ask. Every ask must survive."""
    upsert_goal(tmp_path, _record())
    ctx = multiprocessing.get_context("fork")
    asks = [f"{i}. Concurrent ask number {i}." for i in range(15)]
    procs = [ctx.Process(target=_mp_add_ask, args=(str(tmp_path), "tophand:f2-77:0.0", ask)) for ask in asks]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    stored = get_goal(tmp_path, "tophand:f2-77:0.0")
    assert stored is not None
    assert set(stored.open_asks) == set(asks)
