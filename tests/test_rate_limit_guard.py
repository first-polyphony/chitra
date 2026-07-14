"""Tests for chitra.rate_limit_guard: the durable pause/resume transaction
state machine (see docs/SOL-ADVERSARIAL-REVIEW finding #2)."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import chitra.dispatchd as dispatchd_mod
from chitra.account_registry import RegistryEntry, get_entry
from chitra.dispatch import DispatchOrder, DispatchResult, DispatchStatus, liveness_check
from chitra.goals import LOAD_SHED_HOLD_REASON_PREFIX, RATE_LIMIT_HOLD_REASON_PREFIX, GoalRecord, get_goal, hold_goal, upsert_goal
from chitra.lane_activity import LaneActivity, upsert_lane_activity
from chitra.load_shed import PressureSample
from chitra.policy_config import PausePolicy, PolicyConfig, UsagePolicy
from chitra.rate_limit_guard import (
    CHECKPOINT_NUDGE,
    STOP_NUDGE,
    apply_pause,
    apply_resume,
    plan_pauses,
    plan_resumes,
    sweep,
)
from chitra.rate_limit_state import LoadHostState, Transaction, get_load_state, get_transaction, upsert_load_state, upsert_transaction
from chitra.recovery import load_recovery_records, recovery_records_path
from chitra.usage import AccountedVerdict, UsageSnapshot, UsageWindow

FAST_POLICY = PolicyConfig(
    pause=PausePolicy(
        checkpoint_deadline_seconds=60,
        stop_deadline_seconds=60,
        quiescence_quiet_seconds=30,
        quiescence_timeout_seconds=300,
        resume_deadline_seconds=60,
        max_retry_attempts=3,
    )
)
CLEAR_PRESSURE = PressureSample(80, 0, 0, 0)


def _snapshot(*, session_id: str, tmux_session: str, account: str = "acct@example.com", five_hour_pct: float, ts: str) -> UsageSnapshot:
    return UsageSnapshot(
        kind="claude",
        ts=ts,
        session_id=session_id,
        tmux_session=tmux_session,
        five_hour=UsageWindow(five_hour_pct, int(datetime.fromisoformat(ts).timestamp()) + 3600),
        seven_day=UsageWindow(10, int(datetime.fromisoformat(ts).timestamp()) + 86400),
        account=account,
    )


def _write_snapshot(usage_dir: Path, snapshot: UsageSnapshot) -> None:
    usage_dir.mkdir(parents=True, exist_ok=True)
    (usage_dir / f"{snapshot.session_id}.json").write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")


def _goal(session_ref: str = "tophand:lane1:0.0") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the tested rate-limit guard safely under a real sweep cycle.",
        done_when="Tests pass and the full suite is green.",
        source="task",
        status="working",
    )


def _deliver(queue_dir: Path, order_id: str, *, status: DispatchStatus = DispatchStatus.SENT, transcript_path: str | None = None) -> None:
    """Simulate dispatchd having processed one order (writes its result file
    directly -- the real dispatchd/transcript-grep machinery is exercised
    separately in tests/test_dispatchd.py and tests/test_dispatch.py)."""
    results_dir = queue_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    result = DispatchResult(order_id=order_id, session_ref="irrelevant", status=status, transcript_path=transcript_path)
    (results_dir / f"{order_id}.json").write_text(result.model_dump_json(), encoding="utf-8")


ISO = "2026-07-12T00:00:00+00:00"


def _iso(minutes: float = 0, seconds: float = 0) -> str:
    return (datetime.fromisoformat(ISO) + timedelta(minutes=minutes, seconds=seconds)).isoformat()


def _now(minutes: float = 0, seconds: float = 0) -> datetime:
    return datetime.fromisoformat(_iso(minutes, seconds))


# --- unit-level: planning + single-step apply -------------------------------


def test_plan_pauses_skips_untracked_sessions_and_foreign_holds(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _goal("tophand:tracked:0.0"))
    verdicts = [
        AccountedVerdict(
            session_id="s1",
            tmux_session="untracked",
            kind="claude",
            account="a",
            level="pause",
            binding_window="5h",
            resume_at_epoch=1_700_000_000,
            self_fresh=True,
            account_attributed=False,
        ),
        AccountedVerdict(
            session_id="s2",
            tmux_session="tracked",
            kind="claude",
            account="a",
            level="pause",
            binding_window="5h",
            resume_at_epoch=1_700_000_000,
            self_fresh=True,
            account_attributed=False,
        ),
    ]
    to_pause, skipped = plan_pauses(verdicts, host="tophand", goals_root=tmp_path)
    assert [v.tmux_session for v in to_pause] == ["tracked"]
    assert any("untracked" in reason and "no chitra goal record" in reason for reason in skipped)


def test_plan_pauses_selects_an_attached_non_chitra_session(tmp_path: Path) -> None:
    def attached_runner(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["tmux", "list-clients", "-t"]
        assert timeout == 5
        return subprocess.CompletedProcess(command, 0, stdout="operator-client\n", stderr="")

    session_ref = "trailhead:operator-work:0.0"
    assert liveness_check(session_ref, runner=attached_runner, local_extra={"trailhead"}) is True
    upsert_goal(tmp_path, _goal(session_ref))
    verdict = AccountedVerdict(
        session_id="attached-operator-session",
        tmux_session="operator-work",
        kind="claude",
        account="a",
        level="pause",
        binding_window="5h",
        resume_at_epoch=1_700_000_000,
        self_fresh=True,
        account_attributed=False,
    )

    to_pause, skipped = plan_pauses([verdict], host="trailhead", goals_root=tmp_path)

    assert to_pause == [verdict]
    assert skipped == []


def test_plan_pauses_never_selects_chitra_monitor_or_boomtown(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _goal("trailhead:monitor:0.0"))
    upsert_goal(tmp_path, _goal("trailhead:boomtown:0.0"))
    verdicts = [
        AccountedVerdict(
            session_id=f"chitra-{tmux_session}",
            tmux_session=tmux_session,
            kind="claude",
            account="a",
            level="pause",
            binding_window=binding_window,
            resume_at_epoch=1_700_000_000,
            self_fresh=True,
            account_attributed=False,
        )
        for tmux_session, binding_window in (("monitor", "5h"), ("boomtown", "7d"))
    ]

    to_pause, skipped = plan_pauses(verdicts, host="trailhead", goals_root=tmp_path)

    assert to_pause == []
    assert len(skipped) == 2
    assert all("Chitra's own monitor/harness session is never paused" in reason for reason in skipped)


def test_apply_pause_freezes_immediately_and_starts_pause_requested(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _goal())
    verdict = AccountedVerdict(
        session_id="s1",
        tmux_session="lane1",
        kind="claude",
        account="a",
        level="pause",
        binding_window="5h",
        resume_at_epoch=1_700_000_000,
        self_fresh=True,
        account_attributed=False,
    )
    txn = apply_pause(verdict, host="tophand", goals_root=tmp_path, now=_now())

    assert txn.phase == "pause_requested"
    goal = get_goal(tmp_path, "tophand:lane1:0.0")
    assert goal is not None and goal.status == "held"
    assert goal.hold_reason == "rate-limit:5h"


def test_progressive_resume_starts_one_stable_lane_per_sweep(tmp_path: Path) -> None:
    usage_dir = tmp_path / "usage"
    refs = [f"tophand:lane{i}:0.0" for i in (3, 1, 2)]
    for ref in refs:
        upsert_goal(tmp_path, _goal(ref))
        hold_goal(tmp_path, ref, reason="rate-limit:5h", resume_at=_iso(minutes=-1))
        upsert_transaction(
            tmp_path,
            Transaction(
                session_ref=ref,
                phase="held",
                hold_reason="rate-limit:5h",
                resume_at=_iso(minutes=-1),
                created_at=_iso(),
                updated_at=_iso(),
            ),
        )
        session = ref.split(":")[1]
        _write_snapshot(usage_dir, _snapshot(session_id=session, tmux_session=session, five_hour_pct=5, ts=_iso()))

    sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path,
        queue_dir=tmp_path / "queue",
        policy=FAST_POLICY,
        pressure_sample=CLEAR_PRESSURE,
        now=_now(),
    )

    assert get_transaction(tmp_path, "tophand:lane1:0.0").phase == "resume_requested"
    assert get_transaction(tmp_path, "tophand:lane2:0.0").phase == "held"
    assert get_transaction(tmp_path, "tophand:lane3:0.0").phase == "held"

    sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path,
        queue_dir=tmp_path / "queue",
        policy=FAST_POLICY,
        pressure_sample=CLEAR_PRESSURE,
        now=_now(minutes=1),
    )

    assert get_transaction(tmp_path, "tophand:lane1:0.0").phase == "resume_sent"
    assert get_transaction(tmp_path, "tophand:lane2:0.0").phase == "resume_requested"
    assert get_transaction(tmp_path, "tophand:lane3:0.0").phase == "held"


def test_progressive_resume_does_not_limit_brand_new_rate_limit_pauses(tmp_path: Path) -> None:
    usage_dir = tmp_path / "usage"
    for index in range(3):
        session = f"hot{index}"
        upsert_goal(tmp_path, _goal(f"tophand:{session}:0.0"))
        _write_snapshot(usage_dir, _snapshot(session_id=session, tmux_session=session, five_hour_pct=99, ts=_iso()))

    sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path,
        queue_dir=tmp_path / "queue",
        policy=FAST_POLICY,
        pressure_sample=CLEAR_PRESSURE,
        now=_now(),
    )

    assert all(get_goal(tmp_path, f"tophand:hot{index}:0.0").status == "held" for index in range(3))
    assert all(get_transaction(tmp_path, f"tophand:hot{index}:0.0").phase == "pause_requested" for index in range(3))


def test_superseded_hold_janitor_closes_dead_goal_without_dispatch(tmp_path: Path) -> None:
    ref = "tophand:old-lane:0.0"
    upsert_goal(tmp_path, _goal(ref))
    hold_goal(tmp_path, ref, reason="superseded-by:host:other:0.0")

    report = sweep(
        usage_dir=tmp_path / "usage",
        host="tophand",
        goals_root=tmp_path,
        queue_dir=tmp_path / "queue",
        policy=FAST_POLICY,
        pressure_sample=CLEAR_PRESSURE,
        now=_now(),
    )

    assert report.cleared == [ref]
    assert get_goal(tmp_path, ref) is None
    assert get_transaction(tmp_path, ref) is None
    assert not list((tmp_path / "queue" / "orders").glob("*.json"))


# --- full multi-sweep pause sequence ----------------------------------------


def test_full_pause_sequence_checkpoint_stop_and_verified_quiescence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drives the exact chain the spec requires: pause_requested ->
    checkpoint delivered+verified -> held, with the deterministic /goal
    clear stop order and transcript-quiescence verification in between --
    not merely a status label."""
    goals_root = tmp_path / "state"
    queue_dir = tmp_path / "queue"
    usage_dir = tmp_path / "usage"
    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("CHITRA_LOCAL_HOST", "tophand")
    upsert_goal(goals_root, _goal())
    _write_snapshot(usage_dir, _snapshot(session_id="s1", tmux_session="lane1", five_hour_pct=93, ts=_iso()))

    report1 = sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now())
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"  # frozen immediately
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn is not None and txn.phase == "pause_requested"
    assert report1.paused == []

    # sweep 2: pause_requested -> checkpoint_sent (enqueues the checkpoint order)
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=1))
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "checkpoint_sent"
    order_path = queue_dir / "orders" / f"{txn.checkpoint_order_id}.json"
    assert order_path.exists()
    assert json.loads(order_path.read_text())["nudge"] == CHECKPOINT_NUDGE
    assert json.loads(order_path.read_text())["bypass_rate_limit_freeze"] is True
    assert json.loads(order_path.read_text())["task_type"] == "rate-limit-checkpoint"

    # sweep 3, no result yet: stays in checkpoint_sent, no double-enqueue
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=2))
    assert get_transaction(goals_root, "tophand:lane1:0.0").phase == "checkpoint_sent"

    # dispatchd delivers the checkpoint
    _deliver(queue_dir, txn.checkpoint_order_id, transcript_path=str(transcript))
    transcript.write_text("checkpoint delivered\n", encoding="utf-8")

    # sweep 4: checkpoint confirmed -> stop_sent (enqueues the deterministic /goal clear order)
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=3))
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "stop_sent"
    stop_order_path = queue_dir / "orders" / f"{txn.stop_order_id}.json"
    assert json.loads(stop_order_path.read_text())["nudge"] == STOP_NUDGE == "/goal clear"

    # dispatchd delivers the stop-clear order
    _deliver(queue_dir, txn.stop_order_id, transcript_path=str(transcript))

    # sweep 5: stop confirmed -> awaiting_quiescence (transcript path recorded; the mtime
    # observation itself is a separate step, taken the NEXT sweep -- one phase per sweep).
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=4))
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "awaiting_quiescence"
    assert txn.last_transcript_mtime is None

    # sweep 6: first mtime observation.
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=5))
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "awaiting_quiescence"
    assert txn.last_transcript_mtime is not None

    # sweep 7: transcript still unchanged, but not yet quiet long enough (FAST_POLICY quiet=30s, only ~1s elapsed)
    sweep(
        usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=5, seconds=1)
    )
    assert get_transaction(goals_root, "tophand:lane1:0.0").phase == "awaiting_quiescence"

    # sweep 8: still unchanged, now past the quiet window -> verified held
    report_final = sweep(
        usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=5, seconds=31)
    )
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "held"
    assert len(report_final.paused) == 1
    assert report_final.paused[0].session_ref == "tophand:lane1:0.0"
    recovery_records = load_recovery_records(goals_root)
    assert len(recovery_records) == 1
    recovery = recovery_records[0]
    assert recovery.pause_id
    assert recovery.session_ref == "tophand:lane1:0.0"
    assert recovery.hold_reason == "rate-limit:5h"
    assert recovery.transcript_path == str(transcript)
    assert "Ship the tested rate-limit guard safely" in recovery.resume_note
    assert "Tests pass and the full suite is green" in recovery.resume_note
    assert recovery.resume_at
    assert recovery.paused_at == _iso(minutes=5, seconds=31)
    assert recovery_records_path(goals_root).exists()
    # The goal itself was never re-touched to something other than held.
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"


def test_quiescence_resets_if_the_transcript_is_still_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the target transcript keeps growing (the turn has NOT actually
    stopped), the quiet window must reset, not silently tick toward held."""
    goals_root = tmp_path / "state"
    queue_dir = tmp_path / "queue"
    transcript = tmp_path / "session.jsonl"
    monkeypatch.setenv("CHITRA_LOCAL_HOST", "tophand")
    transcript.write_text("still going\n", encoding="utf-8")
    upsert_goal(goals_root, _goal())
    txn = Transaction(
        session_ref="tophand:lane1:0.0",
        phase="awaiting_quiescence",
        hold_reason="rate-limit:5h",
        resume_at=_iso(minutes=60),
        transcript_path=str(transcript),
        last_transcript_mtime=transcript.stat().st_mtime,
        quiescent_since=_iso(),
        deadline_at=_iso(minutes=10),
        created_at=_iso(),
        updated_at=_iso(),
    )
    upsert_transaction(goals_root, txn)

    import time as time_mod

    time_mod.sleep(0.05)
    transcript.write_text("still going -- more output\n", encoding="utf-8")  # mtime changes: still active

    from chitra.usage import evaluate_grouped

    report = sweep(
        usage_dir=tmp_path / "usage",
        host="tophand",
        goals_root=goals_root,
        queue_dir=queue_dir,
        policy=FAST_POLICY,
        now=_now(seconds=31),  # past the original quiet window, but the transcript just changed
    )
    del evaluate_grouped  # unused import guard -- kept for readability of intent above
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "awaiting_quiescence"  # never advanced to held
    assert report.paused == []


def test_no_verified_transcript_escalates_instead_of_falsely_claiming_stopped(tmp_path: Path) -> None:
    """A checkpoint/stop confirmed only via the weaker pane-capture fallback
    (no transcript_path) cannot be verified deterministically -- this must
    escalate, never silently mark held without evidence."""
    from chitra.goals import hold_goal

    goals_root = tmp_path / "state"
    queue_dir = tmp_path / "queue"
    upsert_goal(goals_root, _goal())
    hold_goal(goals_root, "tophand:lane1:0.0", reason="rate-limit:5h", resume_at=_iso(minutes=60))
    txn = Transaction(
        session_ref="tophand:lane1:0.0",
        phase="awaiting_quiescence",
        hold_reason="rate-limit:5h",
        resume_at=_iso(minutes=60),
        transcript_path="",  # no transcript evidence available
        deadline_at=_iso(seconds=-1),  # already past deadline
        created_at=_iso(),
        updated_at=_iso(),
    )
    upsert_transaction(goals_root, txn)

    report = sweep(usage_dir=tmp_path / "usage", host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now())

    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "awaiting_quiescence"  # never silently marked held
    assert txn.escalated is True
    assert any("cannot safely" not in e and "pane-capture fallback" in e for e in report.escalations)
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"  # freeze remains


# --- bounded retry / no-strand-forever --------------------------------------


def test_missing_checkpoint_result_retries_then_escalates_without_dropping_the_freeze(tmp_path: Path) -> None:
    """No result at all (dispatchd may simply not be running, or the order
    is still queued) is treated as 'wait longer, then escalate for operator
    visibility' -- NOT as license to spam duplicate checkpoint orders. The
    order id must stay unchanged across the retry: the guard only creates a
    new delivery attempt once it has POSITIVE evidence (a terminal
    BLOCKED/FAILED result) that the prior attempt actually failed -- see
    test_terminal_delivery_failure_retries_with_a_fresh_order below."""
    from chitra.goals import hold_goal

    goals_root = tmp_path / "state"
    queue_dir = tmp_path / "queue"
    upsert_goal(goals_root, _goal())
    hold_goal(goals_root, "tophand:lane1:0.0", reason="rate-limit:5h", resume_at=_iso(minutes=60))
    txn = Transaction(
        session_ref="tophand:lane1:0.0",
        phase="checkpoint_sent",
        hold_reason="rate-limit:5h",
        resume_at=_iso(minutes=60),
        checkpoint_order_id="ord-checkpoint-1",
        deadline_at=_iso(seconds=-1),  # already overdue, no result ever written
        created_at=_iso(),
        updated_at=_iso(),
    )
    upsert_transaction(goals_root, txn)
    policy = PolicyConfig(pause=PausePolicy(checkpoint_deadline_seconds=1, max_retry_attempts=2))

    # sweep 1: overdue, attempt 1 of 2 -- waits longer, same order id (no evidence of failure to retry against)
    sweep(usage_dir=tmp_path / "usage", host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=policy, now=_now())
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "checkpoint_sent"
    assert txn.attempts == 1
    assert txn.checkpoint_order_id == "ord-checkpoint-1"
    assert not txn.escalated

    # sweep 2: overdue again, attempts+1 >= max_attempts -- escalates permanently
    report = sweep(
        usage_dir=tmp_path / "usage",
        host="tophand",
        goals_root=goals_root,
        queue_dir=queue_dir,
        policy=policy,
        now=_now(seconds=5),
    )
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.escalated is True
    assert any("escalating" in e for e in report.escalations)
    # The freeze is never lifted by escalation.
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"

    # sweep 3: already escalated -- reported again for visibility, not re-retried into a new attempt count
    report3 = sweep(
        usage_dir=tmp_path / "usage",
        host="tophand",
        goals_root=goals_root,
        queue_dir=queue_dir,
        policy=policy,
        now=_now(seconds=10),
    )
    txn_after = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn_after.attempts == txn.attempts  # not incremented further
    assert any("escalated earlier" in e for e in report3.escalations)


def test_terminal_delivery_failure_retries_with_a_fresh_order(tmp_path: Path) -> None:
    """Unlike a missing result, a TERMINAL non-SENT result (BLOCKED/FAILED)
    is positive evidence the prior attempt failed -- the guard retries with
    a genuinely fresh order id."""
    from chitra.goals import hold_goal

    goals_root = tmp_path / "state"
    queue_dir = tmp_path / "queue"
    upsert_goal(goals_root, _goal())
    hold_goal(goals_root, "tophand:lane1:0.0", reason="rate-limit:5h", resume_at=_iso(minutes=60))
    txn = Transaction(
        session_ref="tophand:lane1:0.0",
        phase="checkpoint_sent",
        hold_reason="rate-limit:5h",
        resume_at=_iso(minutes=60),
        checkpoint_order_id="ord-checkpoint-1",
        deadline_at=_iso(minutes=10),
        created_at=_iso(),
        updated_at=_iso(),
    )
    upsert_transaction(goals_root, txn)
    _deliver(queue_dir, "ord-checkpoint-1", status=DispatchStatus.FAILED)
    policy = PolicyConfig(pause=PausePolicy(checkpoint_deadline_seconds=60, max_retry_attempts=3))

    sweep(usage_dir=tmp_path / "usage", host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=policy, now=_now())

    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.attempts == 1
    assert txn.checkpoint_order_id != "ord-checkpoint-1"
    assert (queue_dir / "orders" / f"{txn.checkpoint_order_id}.json").exists()


def test_orphaned_hold_with_no_transaction_is_reconciled_not_stranded(tmp_path: Path) -> None:
    """Regression for SOL finding #2 item 1: a hold that exists (e.g. an
    operator-applied rate-limit: hold, or a crash right after hold_goal but
    before the transaction's own first write) with NO transaction record
    must be recovered, not left frozen forever with no forward progress."""
    from chitra.goals import hold_goal

    goals_root = tmp_path / "state"
    upsert_goal(goals_root, _goal())
    hold_goal(goals_root, "tophand:lane1:0.0", reason="rate-limit:5h", resume_at=_iso(minutes=60))
    assert get_transaction(goals_root, "tophand:lane1:0.0") is None  # no transaction yet -- the orphan scenario

    report = sweep(
        usage_dir=tmp_path / "usage", host="tophand", goals_root=goals_root, queue_dir=tmp_path / "queue", policy=FAST_POLICY, now=_now()
    )

    # Reconciled AND immediately progressed one more step within the same
    # sweep (the freshly-recovered transaction is picked up by this same
    # sweep's progression pass) -- the key regression check is that it is no
    # longer stuck with NO transaction at all.
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn is not None and txn.phase == "checkpoint_sent"
    assert any("reconciled" in note for note in report.advanced)


# --- full resume sequence + deferred-order redelivery (SOL findings #1, #2) --


def test_full_resume_sequence_clears_hold_only_after_confirmed_delivery_and_requeues_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: an ordinary order arrives while a lane is rate-limit held
    -> no pane I/O, durably deferred -> the resume sequence completes only
    after the resume nudge is CONFIRMED delivered -> the hold clears -> the
    deferred order is delivered exactly once."""
    goals_root = tmp_path / "state"
    queue_dir = tmp_path / "queue"

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    upsert_goal(goals_root, _goal())
    held = Transaction(
        session_ref="tophand:lane1:0.0",
        phase="held",
        hold_reason="rate-limit:5h",
        resume_at=_iso(minutes=1),
        created_at=_iso(),
        updated_at=_iso(),
    )
    upsert_transaction(goals_root, held)
    from chitra.goals import hold_goal

    hold_goal(goals_root, "tophand:lane1:0.0", reason="rate-limit:5h", resume_at=_iso(minutes=1))

    # An ordinary order arrives while held -- must defer, no pane I/O.
    ordinary = DispatchOrder(order_id="ord-ordinary", session_ref="tophand:lane1:0.0", nudge="do the real work")
    (queue_dir / "orders").mkdir(parents=True, exist_ok=True)
    (queue_dir / "orders" / "ord-ordinary.json").write_text(ordinary.model_dump_json(), encoding="utf-8")
    deferred_results = dispatchd_mod.run_once(
        queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root
    )
    assert deferred_results[0].status == DispatchStatus.DEFERRED
    assert (queue_dir / "deferred" / "ord-ordinary.json").exists()

    usage_dir = tmp_path / "usage"
    _write_snapshot(usage_dir, _snapshot(session_id="s1", tmux_session="lane1", five_hour_pct=5, ts=_iso(minutes=2)))  # back to ok

    # sweep: window due + fresh verdict ok -> starts the resume sequence
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=2))
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "resume_requested"
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"  # hold NOT cleared yet

    # sweep: resume_requested -> resume_sent (enqueues the re-arm nudge)
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=3))
    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn.phase == "resume_sent"
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"  # still held -- not yet confirmed delivered

    # dispatchd actually delivers the resume order for real (drains the queue).
    real_results = dispatchd_mod.run_once(
        queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root
    )
    assert any(r.order_id == txn.resume_order_id and r.status == DispatchStatus.SENT for r in real_results)

    # sweep: resume confirmed -> hold cleared, deferred backlog requeued, transaction removed
    report = sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=queue_dir, policy=FAST_POLICY, now=_now(minutes=4))
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "working"
    assert get_transaction(goals_root, "tophand:lane1:0.0") is None
    assert len(report.resumed) == 1
    assert (queue_dir / "orders" / "ord-ordinary.json").exists()  # requeued out of deferred/
    assert not (queue_dir / "deferred" / "ord-ordinary.json").exists()

    # The originally-deferred order is now delivered -- exactly once.
    final_results = dispatchd_mod.run_once(
        queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root
    )
    assert any(r.order_id == "ord-ordinary" and r.status == DispatchStatus.SENT for r in final_results)
    assert (queue_dir / "results" / "ord-ordinary.json").exists()

    again = dispatchd_mod.run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)
    assert again == []  # never redelivered


def _pause_held_with_matching_window(goals_root: Path, *, session_ref: str, resets_epoch: int, now: datetime) -> AccountedVerdict:
    """Set up an already-``held`` lane whose stored hold_reason/resume_at
    exactly match what a fresh verdict computed from ``resets_epoch`` would
    produce -- avoiding a spurious plan_pauses re-trigger from an
    artificially mismatched resume_at in these resume-gating tests."""
    verdict = AccountedVerdict(
        session_id="s1",
        tmux_session=session_ref.split(":")[1],
        kind="claude",
        account="a",
        level="pause",
        binding_window="5h",
        resume_at_epoch=resets_epoch,
        self_fresh=True,
        account_attributed=False,
    )
    txn = apply_pause(verdict, host=session_ref.split(":")[0], goals_root=goals_root, now=now)
    upsert_transaction(goals_root, dataclasses.replace(txn, phase="held"))
    return verdict


def test_never_resumes_into_a_still_hot_window(tmp_path: Path) -> None:
    goals_root = tmp_path / "state"
    upsert_goal(goals_root, _goal())
    resets_epoch = int(_now(minutes=2).timestamp()) + 3600
    _pause_held_with_matching_window(goals_root, session_ref="tophand:lane1:0.0", resets_epoch=resets_epoch, now=_now())
    usage_dir = tmp_path / "usage"
    _write_snapshot(
        usage_dir,
        UsageSnapshot(
            kind="claude",
            ts=_iso(minutes=2),
            session_id="s1",
            tmux_session="lane1",
            five_hour=UsageWindow(95, resets_epoch),
            seven_day=UsageWindow(10, resets_epoch + 86400),
            account="a",
        ),
    )  # STILL hot -- same window, not yet reset

    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=tmp_path / "queue", policy=FAST_POLICY, now=_now(minutes=2))

    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn is not None and txn.phase not in ("resume_requested", "resume_sent")  # no resume transaction started
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"


def test_auto_resume_false_escalates_instead_of_resuming(tmp_path: Path) -> None:
    goals_root = tmp_path / "state"
    upsert_goal(goals_root, _goal())
    resets_epoch = int(_now(minutes=1).timestamp())  # due by the time the sweep below runs at minutes=2
    _pause_held_with_matching_window(goals_root, session_ref="tophand:lane1:0.0", resets_epoch=resets_epoch, now=_now())
    usage_dir = tmp_path / "usage"
    _write_snapshot(usage_dir, _snapshot(session_id="s1", tmux_session="lane1", five_hour_pct=5, ts=_iso(minutes=2)))
    policy = PolicyConfig(usage=UsagePolicy(auto_resume=False), pause=FAST_POLICY.pause)

    report = sweep(
        usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=tmp_path / "queue", policy=policy, now=_now(minutes=2)
    )

    txn = get_transaction(goals_root, "tophand:lane1:0.0")
    assert txn is not None and txn.phase == "held"  # never advanced into a resume transaction
    assert get_goal(goals_root, "tophand:lane1:0.0").status == "held"
    assert any("auto_resume is False" in e for e in report.escalations)


def test_plan_resumes_waits_for_the_pause_sequence_to_finish_before_starting_resume(tmp_path: Path) -> None:
    """If the window resets while the pause sequence is still mid-flight
    (e.g. awaiting_quiescence), resume must not jump the gun."""
    upsert_goal(tmp_path, _goal())
    from chitra.goals import hold_goal

    hold_goal(tmp_path, "tophand:lane1:0.0", reason="rate-limit:5h", resume_at=_iso(minutes=1))
    upsert_transaction(
        tmp_path,
        Transaction(
            session_ref="tophand:lane1:0.0",
            phase="awaiting_quiescence",
            hold_reason="rate-limit:5h",
            resume_at=_iso(minutes=1),
            created_at=_iso(),
            updated_at=_iso(),
        ),
    )
    verdicts = [
        AccountedVerdict(
            session_id="s1",
            tmux_session="lane1",
            kind="claude",
            account="a",
            level="ok",
            binding_window="",
            resume_at_epoch=0,
            self_fresh=True,
            account_attributed=False,
        )
    ]
    to_resume, _escalations = plan_resumes(goals_root=tmp_path, verdicts=verdicts, policy=UsagePolicy(), now=_now(minutes=2))
    assert to_resume == []


def test_apply_resume_uses_existing_held_transaction_if_present(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _goal())
    from chitra.goals import hold_goal

    hold_goal(tmp_path, "tophand:lane1:0.0", reason="rate-limit:5h", resume_at=_iso())
    upsert_transaction(
        tmp_path,
        Transaction(
            session_ref="tophand:lane1:0.0",
            phase="held",
            hold_reason="rate-limit:5h",
            resume_at=_iso(),
            created_at=_iso(),
            updated_at=_iso(),
        ),
    )
    record = get_goal(tmp_path, "tophand:lane1:0.0")
    txn = apply_resume(record, goals_root=tmp_path, now=_now())
    assert txn.phase == "resume_requested"


# --- account registry integration (SOL finding #6) --------------------------


def test_sweep_escalates_when_a_tracked_session_disappears_from_the_usage_batch(tmp_path: Path) -> None:
    goals_root = tmp_path / "state"
    usage_dir = tmp_path / "usage"
    upsert_goal(goals_root, _goal())
    _write_snapshot(usage_dir, _snapshot(session_id="s1", tmux_session="lane1", five_hour_pct=10, ts=_iso()))
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=tmp_path / "queue", policy=FAST_POLICY, now=_now())
    assert get_entry(goals_root, "lane1") is not None

    (usage_dir / "s1.json").unlink()  # sidecar stops writing entirely
    report = sweep(
        usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=tmp_path / "queue", policy=FAST_POLICY, now=_now(minutes=1)
    )

    assert any("lane1" in e and "missing this sweep" in e for e in report.escalations)


def test_sweep_escalates_on_account_identity_change_between_sweeps(tmp_path: Path) -> None:
    goals_root = tmp_path / "state"
    usage_dir = tmp_path / "usage"
    upsert_goal(goals_root, _goal())
    _write_snapshot(usage_dir, _snapshot(session_id="s1", tmux_session="lane1", account="old@example.com", five_hour_pct=10, ts=_iso()))
    sweep(usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=tmp_path / "queue", policy=FAST_POLICY, now=_now())

    _write_snapshot(
        usage_dir, _snapshot(session_id="s1", tmux_session="lane1", account="new@example.com", five_hour_pct=10, ts=_iso(minutes=1))
    )
    report = sweep(
        usage_dir=usage_dir, host="tophand", goals_root=goals_root, queue_dir=tmp_path / "queue", policy=FAST_POLICY, now=_now(minutes=1)
    )

    assert any("old@example.com" in e and "new@example.com" in e for e in report.escalations)


def test_registry_entry_records_the_observed_account(tmp_path: Path) -> None:
    usage_dir = tmp_path / "usage"
    upsert_goal(tmp_path, _goal())
    _write_snapshot(usage_dir, _snapshot(session_id="s1", tmux_session="lane1", account="a@x.com", five_hour_pct=10, ts=_iso()))
    sweep(usage_dir=usage_dir, host="tophand", goals_root=tmp_path, queue_dir=tmp_path / "queue", policy=FAST_POLICY, now=_now())
    entry = get_entry(tmp_path, "lane1")
    assert isinstance(entry, RegistryEntry)
    assert entry.account == "a@x.com"


# --- Codex fan-out: explicitly excluded, fails closed (SOL finding #6) ------


def test_codex_synthetic_verdict_is_skipped_not_silently_fanned_out(tmp_path: Path) -> None:
    verdicts = [
        AccountedVerdict(
            session_id="codex-account",
            tmux_session="",
            kind="codex",
            account="a",
            level="pause",
            binding_window="5h",
            resume_at_epoch=1_700_000_000,
            self_fresh=True,
            account_attributed=False,
        )
    ]
    to_pause, skipped = plan_pauses(verdicts, host="tophand", goals_root=tmp_path)
    assert to_pause == []
    assert any("no tmux_session" in reason for reason in skipped)


# --- host-load ladder integration -------------------------------------------


def test_load_shed_acts_only_after_second_breach_and_uses_distinct_prefix(tmp_path: Path) -> None:
    usage_dir = tmp_path / "usage"
    queue_dir = tmp_path / "queue"
    for index in range(7):
        status = "blocked" if index == 0 else "working"
        upsert_goal(tmp_path, dataclasses.replace(_goal(f"tophand:lane{index}:0.0"), status=status))
    codex_lane = dataclasses.replace(_snapshot(session_id="lane0", tmux_session="lane0", five_hour_pct=5, ts=_iso()), kind="codex")
    _write_snapshot(usage_dir, codex_lane)
    breach = PressureSample(24, 0, 0, 0)

    first = sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path,
        queue_dir=queue_dir,
        policy=FAST_POLICY,
        pressure_sample=breach,
        now=_now(),
    )
    assert first.load_level == 0
    assert all(get_goal(tmp_path, f"tophand:lane{index}:0.0").status != "held" for index in range(7))

    second = sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path,
        queue_dir=queue_dir,
        policy=FAST_POLICY,
        pressure_sample=breach,
        now=_now(minutes=1),
    )

    shed = get_goal(tmp_path, "tophand:lane0:0.0")
    txn = get_transaction(tmp_path, "tophand:lane0:0.0")
    assert second.load_level == 1
    assert shed is not None and shed.status == "held"
    assert shed.hold_reason == "load-shed:tophand:1"
    assert shed.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX)
    assert not shed.hold_reason.startswith(RATE_LIMIT_HOLD_REASON_PREFIX)
    assert txn is not None and txn.backend == "codex" and txn.phase == "pause_requested"
    assert second.shed_lanes == ["tophand:lane0:0.0"]

    upsert_lane_activity(
        tmp_path,
        [
            LaneActivity(
                session_ref="tophand:lane0:0.0",
                pane_id="%1",
                last_change_at=_iso(minutes=1),
                last_seen_at=_iso(minutes=1),
                attached=True,
                backend="codex",
            )
        ],
    )
    sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path,
        queue_dir=queue_dir,
        policy=FAST_POLICY,
        pressure_sample=breach,
        now=_now(minutes=2),
    )
    txn = get_transaction(tmp_path, "tophand:lane0:0.0")
    assert txn is not None and txn.phase == "checkpoint_sent"
    checkpoint = json.loads((queue_dir / "orders" / f"{txn.checkpoint_order_id}.json").read_text(encoding="utf-8"))
    assert checkpoint["task_type"] == "load-shed-checkpoint"
    assert "Codex lane" in checkpoint["nudge"]

    _deliver(queue_dir, txn.checkpoint_order_id)
    sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path,
        queue_dir=queue_dir,
        policy=FAST_POLICY,
        pressure_sample=breach,
        now=_now(minutes=3),
    )
    txn = get_transaction(tmp_path, "tophand:lane0:0.0")
    assert txn is not None and txn.phase == "awaiting_quiescence"
    assert txn.stop_order_id == ""
    assert not any(json.loads(path.read_text())["task_type"] == "load-shed-stop" for path in (queue_dir / "orders").glob("*.json"))


def test_load_resume_waits_for_two_clear_sweeps_then_uses_last_shed_first(tmp_path: Path) -> None:
    refs = ("tophand:first:0.0", "tophand:last:0.0")
    for ref in refs:
        upsert_goal(tmp_path, _goal(ref))
        hold_goal(tmp_path, ref, reason="load-shed:tophand:1")
        upsert_transaction(
            tmp_path,
            Transaction(
                session_ref=ref,
                phase="held",
                hold_reason="load-shed:tophand:1",
                created_at=_iso(),
                updated_at=_iso(),
            ),
        )
    upsert_load_state(
        tmp_path,
        LoadHostState(
            host="tophand",
            observed_level=1,
            load_level=1,
            shed_lanes=refs,
            updated_at=_iso(),
        ),
    )

    first = sweep(
        usage_dir=tmp_path / "usage",
        host="tophand",
        goals_root=tmp_path,
        queue_dir=tmp_path / "queue",
        policy=FAST_POLICY,
        pressure_sample=CLEAR_PRESSURE,
        now=_now(minutes=1),
    )
    assert first.load_level == 1
    assert get_transaction(tmp_path, refs[1]).phase == "held"

    second = sweep(
        usage_dir=tmp_path / "usage",
        host="tophand",
        goals_root=tmp_path,
        queue_dir=tmp_path / "queue",
        policy=FAST_POLICY,
        pressure_sample=CLEAR_PRESSURE,
        now=_now(minutes=2),
    )

    state = get_load_state(tmp_path, "tophand")
    assert second.load_level == 0
    assert state is not None and state.load_level == 0
    assert get_transaction(tmp_path, refs[1]).phase == "resume_requested"
    assert get_transaction(tmp_path, refs[0]).phase == "held"
