"""Tests for pure merge-queue hygiene decisions and durable local state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from chitra.capabilities import enable_capability
from chitra.merge_queue import (
    HOLD_LABEL,
    READY_LABEL,
    CheckResult,
    HoldMarker,
    QueueAction,
    QueueDecision,
    QueueHeadSnapshot,
    QueueHoldNotFoundError,
    QueueInvariantError,
    QueuePolicy,
    RequiredCheck,
    active_holds,
    classify_checks,
    close_hold,
    decide,
    log_action,
    main,
    parse_hold_marker,
    record_decision,
    record_hold,
    render_hold_comment,
    validate_decision,
)

NOW = datetime(2026, 7, 11, 16, tzinfo=UTC)
REQUIRED = QueuePolicy(required=(RequiredCheck("test", 17),))


def _check(
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    started_at: datetime | None = None,
    app_id: int = 17,
) -> CheckResult:
    return CheckResult(
        name="test",
        app_id=app_id,
        status=status,
        conclusion=conclusion,
        started_at=(NOW - timedelta(minutes=20) if started_at is None else started_at).isoformat(),
    )


def _snapshot(
    *,
    checks: tuple[CheckResult, ...] = (_check(),),
    labels: tuple[str, ...] = (READY_LABEL,),
    marker: HoldMarker | None = None,
    merge_state: str = "CLEAN",
    draft: bool = False,
    cross_repo: bool = False,
    control_plane_paths: bool = False,
) -> QueueHeadSnapshot:
    return QueueHeadSnapshot(
        number=42,
        head_sha="abc123",
        created_at=(NOW - timedelta(hours=1)).isoformat(),
        is_draft=draft,
        is_cross_repo=cross_repo,
        mergeable=True,
        merge_state=merge_state,
        labels=labels,
        checks=checks,
        chitra_hold_marker=marker,
        control_plane_paths=control_plane_paths,
        observed_at=NOW.isoformat(),
    )


def _marker(*, head_sha: str = "abc123", reassert_count: int = 0) -> HoldMarker:
    return HoldMarker(
        pr=42,
        head_sha=head_sha,
        red_since=(NOW - timedelta(minutes=20)).isoformat(),
        reassert_count=reassert_count,
    )


def test_classify_checks_uses_latest_exact_app_and_ignores_wrong_app() -> None:
    older_failure = _check(conclusion="failure", started_at=NOW - timedelta(minutes=20))
    newer_success = _check(conclusion="success", started_at=NOW - timedelta(minutes=2))
    wrong_application = _check(app_id=99, conclusion="failure", started_at=NOW)

    assert classify_checks((older_failure, wrong_application, newer_success), REQUIRED.required) == "green"
    assert classify_checks((newer_success, older_failure), REQUIRED.required) == "green"
    assert classify_checks((wrong_application,), REQUIRED.required) == "missing"


def test_decide_covers_hold_release_repair_reassert_and_escalate_paths() -> None:
    red = _check(conclusion="failure")
    hold = decide(_snapshot(checks=(red,)), REQUIRED, NOW)
    assert hold.action == "hold-red"
    assert hold.labels_to_add == (HOLD_LABEL,)
    assert hold.labels_to_remove == (READY_LABEL,)
    assert parse_hold_marker(hold.comment_body) == _marker()

    release = decide(_snapshot(labels=(HOLD_LABEL,), marker=_marker()), REQUIRED, NOW)
    assert release.action == "release-hold"
    assert release.labels_to_add == (READY_LABEL,)
    assert release.labels_to_remove == (HOLD_LABEL,)

    repair = decide(_snapshot(labels=(HOLD_LABEL, READY_LABEL), marker=None), REQUIRED, NOW)
    assert repair.action == "repair-labels"
    assert repair.labels_to_remove == (READY_LABEL,)

    reassert = decide(_snapshot(checks=(red,), labels=(READY_LABEL,), marker=_marker()), REQUIRED, NOW)
    assert reassert.action == "reassert-hold"
    assert parse_hold_marker(reassert.comment_body) == _marker(reassert_count=1)

    escalated = decide(_snapshot(checks=(red,), labels=(READY_LABEL,), marker=_marker(reassert_count=1)), REQUIRED, NOW)
    assert escalated.action == "escalate"
    assert decide(_snapshot(draft=True), REQUIRED, NOW).action == "escalate"
    assert decide(None, REQUIRED, NOW).action == "escalate"


def test_all_queue_invariants_raise() -> None:
    with pytest.raises(QueueInvariantError, match="INV-1"):
        QueueDecision(cast(QueueAction, "merge"), "bad", "", (), ())

    red_snapshot = _snapshot(checks=(_check(conclusion="failure"),), labels=())
    add_ready = QueueDecision("none", "bad", "", (READY_LABEL,), ())
    with pytest.raises(QueueInvariantError, match="INV-2"):
        validate_decision(add_ready, red_snapshot, check_state="red")

    held_snapshot = _snapshot(labels=(HOLD_LABEL,), marker=None)
    with pytest.raises(QueueInvariantError, match="INV-3"):
        validate_decision(add_ready, held_snapshot, check_state="green")

    remove_hold = QueueDecision("none", "bad", "", (), (HOLD_LABEL,))
    with pytest.raises(QueueInvariantError, match="INV-4"):
        validate_decision(remove_hold, _snapshot(labels=(), marker=None), check_state="green")


def test_hold_marker_round_trips_and_keeps_required_first_line() -> None:
    marker = _marker(reassert_count=1)
    rendered = render_hold_comment(marker)

    assert rendered.splitlines()[0] == (
        "<!-- chitra-queue-hold v1 pr=42 head_sha=abc123 red_since=2026-07-11T15:40:00+00:00 holder=chitra -->"
    )
    assert parse_hold_marker(rendered) == marker
    assert parse_hold_marker("unrelated comment") is None


def test_hold_store_is_atomic_and_hygiene_log_is_append_only(tmp_path: Path) -> None:
    marker = _marker()
    record_hold(tmp_path, marker)

    payload = json.loads((tmp_path / "queue_holds.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "chitra.queue_holds.v1"
    assert active_holds(tmp_path) == [marker]
    assert not list(tmp_path.glob("*.tmp"))

    decision = QueueDecision("hold-red", "checks red", render_hold_comment(marker), (HOLD_LABEL,), (READY_LABEL,))
    first = log_action(tmp_path, decision, marker=marker, now=NOW)
    second = log_action(tmp_path, decision, marker=marker, now=NOW + timedelta(seconds=1))
    entries = (tmp_path / "queue_hygiene.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(entries) == 2
    assert json.loads(entries[0]) == first
    assert json.loads(entries[1]) == second

    close_hold(tmp_path, 42)
    assert active_holds(tmp_path) == []
    with pytest.raises(QueueHoldNotFoundError):
        close_hold(tmp_path, 42)


def test_record_decision_and_mutating_cli_verbs_require_queue_management(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    marker = _marker()
    decision = QueueDecision("hold-red", "checks red", render_hold_comment(marker), (HOLD_LABEL,), (READY_LABEL,))

    record_decision(tmp_path, decision, now=NOW)
    assert active_holds(tmp_path) == [marker]
    assert main(["dequeue-hold", "--pr", "42", "--reason", "operator review", "--root", str(tmp_path)]) == 1
    assert "capability is disabled" in capsys.readouterr().err
    assert active_holds(tmp_path) == [marker]

    enable_capability("queue-management", reason="approved", root=tmp_path, now=NOW)
    assert main(["dequeue-hold", "--pr", "42", "--reason", "operator review", "--root", str(tmp_path)]) == 0
    assert active_holds(tmp_path) == []
