"""Tests for chitra.rate_limit_guard: detection, pause-state transitions,
and the resume trigger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from chitra.dispatch import DispatchOrder
from chitra.goals import GoalRecord, due_goals, get_goal, upsert_goal
from chitra.policy_config import PolicyConfig, UsagePolicy
from chitra.rate_limit_guard import (
    CHECKPOINT_NUDGE,
    apply_pause,
    apply_resume,
    plan_pauses,
    plan_resumes,
    sweep,
)
from chitra.usage import AccountedVerdict, UsageSnapshot, UsageWindow


def _record(**changes: object) -> GoalRecord:
    values: dict[str, object] = {
        "session_ref": "tophand:feeds-111:0.0",
        "goal": "Ship the tested rate-limit pause/resume sweep.",
        "done_when": "The full suite and static checks pass.",
        "source": "task-file:/tmp/rate-limit.md",
        "status": "working",
    }
    values.update(changes)
    return GoalRecord(**values)  # type: ignore[arg-type]


def _verdict(
    *,
    session_id: str = "sess-1",
    tmux_session: str = "feeds-111",
    level: str = "pause",
    binding_window: str = "5h",
    resume_at_epoch: int = 1_720_000_000,
    account: str = "acct@example.com",
    self_fresh: bool = True,
    account_attributed: bool = False,
) -> AccountedVerdict:
    return AccountedVerdict(
        session_id=session_id,
        tmux_session=tmux_session,
        kind="claude",
        account=account,
        level=level,  # type: ignore[arg-type]
        binding_window=binding_window,  # type: ignore[arg-type]
        resume_at_epoch=resume_at_epoch,
        self_fresh=self_fresh,
        account_attributed=account_attributed,
    )


# ---------------------------------------------------------------------------
# plan_pauses
# ---------------------------------------------------------------------------


def test_plan_pauses_selects_a_tracked_lane_at_pause_level(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _record())
    verdict = _verdict()

    to_pause, skipped = plan_pauses([verdict], host="tophand", goals_root=tmp_path)

    assert to_pause == [verdict]
    assert skipped == []


def test_plan_pauses_ignores_ok_and_approaching_verdicts(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _record())
    ok = _verdict(level="ok")
    approaching = _verdict(level="approaching")

    to_pause, skipped = plan_pauses([ok, approaching], host="tophand", goals_root=tmp_path)

    assert to_pause == []
    assert skipped == []


def test_plan_pauses_skips_untracked_lane_with_no_goal_record(tmp_path: Path) -> None:
    to_pause, skipped = plan_pauses([_verdict()], host="tophand", goals_root=tmp_path)

    assert to_pause == []
    assert len(skipped) == 1
    assert "no chitra goal record" in skipped[0]


def test_plan_pauses_skips_verdict_with_no_tmux_session(tmp_path: Path) -> None:
    upsert_goal(tmp_path, _record())
    verdict = _verdict(tmux_session="")

    to_pause, skipped = plan_pauses([verdict], host="tophand", goals_root=tmp_path)

    assert to_pause == []
    assert len(skipped) == 1
    assert "cannot resolve a dispatch target" in skipped[0]


def test_plan_pauses_is_idempotent_for_the_same_window(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())
    verdict = _verdict()
    resume_at_iso = datetime.fromtimestamp(verdict.resume_at_epoch, UTC).isoformat()
    from chitra.goals import hold_goal

    hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at=resume_at_iso)

    to_pause, skipped = plan_pauses([verdict], host="tophand", goals_root=tmp_path)

    assert to_pause == []
    assert skipped == []  # already paused for this exact window -- silent no-op, not a skip-reason


def test_plan_pauses_never_overrides_a_non_rate_limit_hold(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())
    from chitra.goals import hold_goal

    hold_goal(tmp_path, stored.session_ref, reason="operator")

    to_pause, skipped = plan_pauses([_verdict()], host="tophand", goals_root=tmp_path)

    assert to_pause == []
    assert len(skipped) == 1
    assert "non-rate-limit reason" in skipped[0]
    assert get_goal(tmp_path, stored.session_ref).hold_reason == "operator"  # type: ignore[union-attr]


def test_plan_pauses_re_pauses_after_a_new_window_replaces_a_resolved_one(tmp_path: Path) -> None:
    """A lane already RESUMED (working, no hold) from a prior rate-limit
    window must still be selected for a brand-new pause verdict."""
    upsert_goal(tmp_path, _record(status="working"))

    to_pause, skipped = plan_pauses([_verdict()], host="tophand", goals_root=tmp_path)

    assert to_pause == [_verdict()]
    assert skipped == []


# ---------------------------------------------------------------------------
# apply_pause
# ---------------------------------------------------------------------------


def test_apply_pause_holds_the_goal_and_enqueues_a_bypass_checkpoint_order(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path, _record())
    verdict = _verdict()
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    queue_dir = tmp_path / "queue"

    outcome = apply_pause(verdict, host="tophand", goals_root=tmp_path, queue_dir=queue_dir, now=now)

    held = get_goal(tmp_path, stored.session_ref)
    assert held is not None
    assert held.status == "held"
    assert held.hold_reason == "rate-limit:5h"
    assert held.resume_at == datetime.fromtimestamp(verdict.resume_at_epoch, UTC).isoformat()
    # Strategic fields (the re-arm payload) are untouched by a pause.
    assert held.goal == stored.goal
    assert held.done_when == stored.done_when

    order_path = queue_dir / "orders" / f"{outcome.checkpoint_order_id}.json"
    assert order_path.exists()
    order = DispatchOrder.model_validate_json(order_path.read_text(encoding="utf-8"))
    assert order.session_ref == "tophand:feeds-111:0.0"
    assert order.nudge == CHECKPOINT_NUDGE
    assert order.bypass_rate_limit_freeze is True
    assert order.task_type == "rate-limit-checkpoint"
    # The checkpoint nudge must never trip dispatch's own directive-voice guard.
    from chitra.dispatch import directive_voice_violation

    assert directive_voice_violation(order.nudge) is None


# ---------------------------------------------------------------------------
# plan_resumes
# ---------------------------------------------------------------------------


def test_plan_resumes_selects_a_due_rate_limit_hold_once_verdict_is_ok(tmp_path: Path) -> None:
    from chitra.goals import hold_goal

    stored = upsert_goal(tmp_path, _record())
    hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at="2026-07-12T10:00:00+00:00")
    fresh_ok = _verdict(level="ok")
    policy = UsagePolicy()
    now = datetime(2026, 7, 12, 10, 5, tzinfo=UTC)

    to_resume, escalations = plan_resumes(goals_root=tmp_path, verdicts=[fresh_ok], policy=policy, now=now)

    assert [r.session_ref for r in to_resume] == [stored.session_ref]
    assert escalations == []


def test_plan_resumes_never_resumes_into_a_still_hot_window(tmp_path: Path) -> None:
    from chitra.goals import hold_goal

    stored = upsert_goal(tmp_path, _record())
    hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at="2026-07-12T10:00:00+00:00")
    still_pause = _verdict(level="pause")
    now = datetime(2026, 7, 12, 10, 5, tzinfo=UTC)

    to_resume, escalations = plan_resumes(goals_root=tmp_path, verdicts=[still_pause], policy=UsagePolicy(), now=now)

    assert to_resume == []
    assert escalations == []


def test_plan_resumes_fails_quiet_with_no_fresh_verdict(tmp_path: Path) -> None:
    from chitra.goals import hold_goal

    stored = upsert_goal(tmp_path, _record())
    hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at="2026-07-12T10:00:00+00:00")
    now = datetime(2026, 7, 12, 10, 5, tzinfo=UTC)

    to_resume, escalations = plan_resumes(goals_root=tmp_path, verdicts=[], policy=UsagePolicy(), now=now)

    assert to_resume == []
    assert escalations == []


def test_plan_resumes_never_touches_operator_or_throttle_holds(tmp_path: Path) -> None:
    from chitra.goals import hold_goal

    stored = upsert_goal(tmp_path, _record())
    hold_goal(tmp_path, stored.session_ref, reason="throttle", resume_at="2026-07-12T10:00:00+00:00")
    fresh_ok = _verdict(level="ok")
    now = datetime(2026, 7, 12, 10, 5, tzinfo=UTC)

    to_resume, escalations = plan_resumes(goals_root=tmp_path, verdicts=[fresh_ok], policy=UsagePolicy(), now=now)

    assert to_resume == []
    assert escalations == []


def test_plan_resumes_surfaces_an_escalation_when_auto_resume_is_false(tmp_path: Path) -> None:
    from chitra.goals import hold_goal

    stored = upsert_goal(tmp_path, _record())
    hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at="2026-07-12T10:00:00+00:00")
    fresh_ok = _verdict(level="ok")
    policy = UsagePolicy(auto_resume=False)
    now = datetime(2026, 7, 12, 10, 5, tzinfo=UTC)

    to_resume, escalations = plan_resumes(goals_root=tmp_path, verdicts=[fresh_ok], policy=policy, now=now)

    assert to_resume == []
    assert len(escalations) == 1
    assert stored.session_ref in escalations[0]
    # No goal-state mutation on an escalation.
    assert get_goal(tmp_path, stored.session_ref).status == "held"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# apply_resume
# ---------------------------------------------------------------------------


def test_apply_resume_clears_the_hold_before_enqueueing_the_rearm_nudge(tmp_path: Path) -> None:
    from chitra.goals import hold_goal

    stored = upsert_goal(tmp_path, _record())
    held = hold_goal(tmp_path, stored.session_ref, reason="rate-limit:5h", resume_at="2026-07-12T10:00:00+00:00")
    queue_dir = tmp_path / "queue"
    now = datetime(2026, 7, 12, 10, 5, tzinfo=UTC)

    outcome = apply_resume(held, goals_root=tmp_path, queue_dir=queue_dir, now=now)

    resumed = get_goal(tmp_path, stored.session_ref)
    assert resumed is not None
    assert resumed.status == "working"
    assert resumed.hold_reason == ""
    assert resumed.resume_at == ""

    order_path = queue_dir / "orders" / f"{outcome.resume_order_id}.json"
    order = DispatchOrder.model_validate_json(order_path.read_text(encoding="utf-8"))
    assert order.session_ref == stored.session_ref
    assert stored.goal in order.nudge
    assert stored.done_when in order.nudge
    assert order.bypass_rate_limit_freeze is True
    assert order.task_type == "rate-limit-resume"


# ---------------------------------------------------------------------------
# sweep() end to end
# ---------------------------------------------------------------------------


def _write_snapshot(usage_dir: Path, snapshot: UsageSnapshot) -> None:
    usage_dir.mkdir(parents=True, exist_ok=True)
    (usage_dir / f"{snapshot.session_id}.json").write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")


def test_sweep_pauses_a_tracked_lane_over_threshold_end_to_end(tmp_path: Path) -> None:
    stored = upsert_goal(tmp_path / "goals", _record())
    usage_dir = tmp_path / "usage"
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    _write_snapshot(
        usage_dir,
        UsageSnapshot(
            kind="claude",
            ts=now.isoformat(),
            session_id="sess-1",
            tmux_session="feeds-111",
            five_hour=UsageWindow(pct=95.0, resets_at=int(now.timestamp()) + 3600),
            seven_day=None,
            account="acct@example.com",
        ),
    )

    report = sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=tmp_path / "goals",
        queue_dir=tmp_path / "queue",
        policy=PolicyConfig(),
        now=now,
    )

    assert len(report.paused) == 1
    assert report.paused[0].session_ref == stored.session_ref
    held = get_goal(tmp_path / "goals", stored.session_ref)
    assert held is not None
    assert held.status == "held"
    assert held.hold_reason == "rate-limit:5h"
    assert (tmp_path / "queue" / "orders" / f"{report.paused[0].checkpoint_order_id}.json").exists()


def test_sweep_resumes_a_due_lane_once_the_window_clears(tmp_path: Path) -> None:
    from chitra.goals import hold_goal

    goals_root = tmp_path / "goals"
    stored = upsert_goal(goals_root, _record())
    later = datetime(2026, 7, 12, 11, 0, tzinfo=UTC)
    hold_goal(goals_root, stored.session_ref, reason="rate-limit:5h", resume_at=later.isoformat())
    usage_dir = tmp_path / "usage"
    _write_snapshot(
        usage_dir,
        UsageSnapshot(
            kind="claude",
            ts=later.isoformat(),
            session_id="sess-1",
            tmux_session="feeds-111",
            five_hour=UsageWindow(pct=5.0, resets_at=int(later.timestamp()) + 3600),
            seven_day=None,
            account="acct@example.com",
        ),
    )

    report = sweep(
        usage_dir=usage_dir,
        host="tophand",
        goals_root=goals_root,
        queue_dir=tmp_path / "queue",
        policy=PolicyConfig(),
        now=later,
    )

    assert len(report.resumed) == 1
    resumed = get_goal(goals_root, stored.session_ref)
    assert resumed is not None
    assert resumed.status == "working"
    assert due_goals(goals_root, now=later) == []


def test_sweep_is_a_no_op_pass_with_no_snapshots_or_due_holds(tmp_path: Path) -> None:
    upsert_goal(tmp_path / "goals", _record())
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()

    report = sweep(usage_dir=usage_dir, host="tophand", goals_root=tmp_path / "goals", queue_dir=tmp_path / "queue")

    assert report.paused == []
    assert report.resumed == []
    assert report.escalations == []
