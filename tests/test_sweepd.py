"""Tests for the deterministic, delta-only Chitra sweep digest daemon."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from chitra.account_registry import update_registry
from chitra.goals import GoalRecord, GoalStatus, close_goal, upsert_goal
from chitra.rate_limit_state import LoadHostState, Transaction, upsert_load_state, upsert_transaction
from chitra.sweepd import SweepSnapshot, build_snapshot, compute_delta, load_snapshot, resolve_config, run_once
from chitra.usage import AccountedVerdict

NOW = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)


def _goal(
    session_ref: str,
    *,
    status: GoalStatus = "working",
    intent: str = "Build a deterministic sensing daemon for compact fleet-state deltas.",
    scope: str = "Daemon module tests and deployment unit only.",
    open_asks: tuple[str, ...] = (),
    needs: str = "",
    hold_reason: str = "",
    resume_at: str = "",
) -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the deterministic fleet digest daemon safely today.",
        done_when="The daemon writes a compact verified delta digest.",
        source="task-file:DocsHome/Projects_Dev/sweep-digest.md",
        status=status,
        intent=intent,
        scope=scope,
        now="",
        last_verified="",
        created_at="",
        updated_at="",
        open_asks=open_asks,
        needs=needs,
        hold_reason=hold_reason,
        resume_at=resume_at,
    )


def _register_account(root: Path, tmux_session: str) -> None:
    update_registry(
        root,
        [
            AccountedVerdict(
                session_id=f"session-{tmux_session}",
                tmux_session=tmux_session,
                kind="claude",
                account="monitor@example.test",
                level="pause",
                binding_window="7d",
                resume_at_epoch=0,
                self_fresh=True,
                account_attributed=False,
            )
        ],
        now=NOW,
    )


def test_compute_delta_surfaces_changed_new_spec_pending_and_disappeared_lanes(tmp_path: Path) -> None:
    stable = upsert_goal(
        tmp_path,
        _goal(
            "trailhead:stable:0.0",
            status="held",
            hold_reason="rate-limit:7d",
            resume_at="2026-07-13T17:00:00+00:00",
        ),
    )
    pending = upsert_goal(
        tmp_path,
        _goal("trailhead:pending:0.0", open_asks=("Approve the irreversible production deploy?",)),
    )
    bad_spec = upsert_goal(tmp_path, _goal("trailhead:bad-spec:0.0", intent="", scope=""))
    upsert_transaction(
        tmp_path,
        Transaction(
            session_ref=stable.session_ref,
            phase="held",
            hold_reason="rate-limit:7d",
            resume_at="2026-07-13T17:00:00+00:00",
            attempts=2,
        ),
    )
    upsert_load_state(
        tmp_path,
        LoadHostState(host="trailhead", load_level=2, shed_lanes=(stable.session_ref,), updated_at=NOW.isoformat()),
    )
    _register_account(tmp_path, "stable")
    flags_path = tmp_path / "flags.log"
    flags_path.write_text(
        "CRIT 2026-07-13T17:55:00Z trailhead:stable:0.0 rate_limit: usage at 95 percent\n",
        encoding="utf-8",
    )

    baseline = build_snapshot(tmp_path, flags_path=flags_path, now=NOW)
    first = compute_delta(SweepSnapshot(), baseline, now=NOW)
    first_lanes = {change.lane.session_ref: change for change in first.changed_lanes}

    assert first_lanes[stable.session_ref].change == "new"
    assert first_lanes[stable.session_ref].lane.due is True
    assert first_lanes[stable.session_ref].lane.rate_limit_phase == "held"
    assert first_lanes[stable.session_ref].lane.load_level == 2
    assert first_lanes[stable.session_ref].lane.account == "monitor@example.test"
    assert first_lanes[pending.session_ref].lane.pending_decisions == ("Approve the irreversible production deploy?",)
    assert first_lanes[bad_spec.session_ref].lane.specification_failures
    assert first.changed_flags[0].flag.rule == "rate_limit"
    assert first.load_level == {"trailhead": 2}
    assert first.shed_lanes == (stable.session_ref,)

    unchanged = compute_delta(baseline, baseline, now=NOW)
    assert unchanged.changed_lanes == ()
    assert unchanged.disappeared_lanes == ()
    assert unchanged.unchanged_lane_count == len(baseline.lanes)
    assert unchanged.changed_flags == ()
    assert unchanged.unchanged_flag_count == len(baseline.flags)

    upsert_goal(tmp_path, replace(stable, status="blocked", now="waiting for quota reset confirmation"))
    new_lane = upsert_goal(tmp_path, _goal("trailhead:new:0.0"))
    close_goal(tmp_path, pending.session_ref, delivered_items=("compact verified delta digest",))
    after = build_snapshot(tmp_path, flags_path=flags_path, now=NOW)
    delta = compute_delta(baseline, after, now=NOW)
    changed = {change.lane.session_ref: change.change for change in delta.changed_lanes}

    assert changed[stable.session_ref] == "changed"
    assert changed[new_lane.session_ref] == "new"
    assert [item.session_ref for item in delta.disappeared_lanes] == [pending.session_ref]


def test_digest_persistence_round_trip_collapses_unchanged_lanes(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _goal("trailhead:one:0.0"))
    flags_path = tmp_path / "flags.log"
    flags_path.write_text("CRIT 2026-07-13T17:55:00Z lane-1 blocked: needs operator input\n", encoding="utf-8")
    config = resolve_config(
        state_dir=tmp_path,
        digest_path=tmp_path / "sweep-digest.json",
        snapshot_path=tmp_path / "sweep-digest-state.json",
        flags_path=flags_path,
        poll_seconds=30,
    )

    first = run_once(config, now=NOW)
    second = run_once(config, now=NOW)

    assert len(first.changed_lanes) == 1
    assert second.changed_lanes == ()
    assert second.unchanged_lane_count == 1
    assert second.changed_flags == ()
    assert second.unchanged_flag_count == 1
    persisted = load_snapshot(config.snapshot_path)
    assert persisted == build_snapshot(tmp_path, flags_path=flags_path, now=NOW)
    digest_payload = json.loads(config.digest_path.read_text(encoding="utf-8"))
    assert digest_payload["unchanged_lane_count"] == 1
    assert digest_payload["changed_lanes"] == []
