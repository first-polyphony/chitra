"""Tests for deterministic per-task_type outcomes aggregation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from chitra.outcomes import compute_outcomes

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")


def test_compute_outcomes_groups_and_joins_effectiveness_metrics(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "ledger.jsonl",
        [
            {
                "order_id": "build-1",
                "session_ref": "host:lane-a:0.0",
                "task_type": "build",
                "status": "completed",
                "created_at": "2026-07-14T09:00:00+00:00",
                "terminal_at": "2026-07-14T09:10:00+00:00",
            },
            {
                "order_id": "build-2",
                "session_ref": "host:lane-a:0.0",
                "task_type": "build",
                "status": "completed",
                "created_at": "2026-07-14T09:20:00+00:00",
                "terminal_at": "2026-07-14T09:50:00+00:00",
            },
            {
                "order_id": "untyped-1",
                "session_ref": "host:lane-b:0.0",
                "status": "completed",
                "created_at": "2026-07-14T10:00:00+00:00",
                "terminal_at": "2026-07-14T10:05:00+00:00",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "completion_reviews.jsonl",
        [
            {
                "session_ref": "host:lane-a:0.0",
                "condition": "completion_claim",
                "review_verdict": "accept",
                "recorded_at": "2026-07-14T09:10:00+00:00",
            },
            {
                "session_ref": "host:lane-a:0.0",
                "condition": "completion_claim",
                "review_verdict": "reject",
                "recorded_at": "2026-07-14T09:50:00+00:00",
            },
            {
                "session_ref": "host:lane-b:0.0",
                "condition": "turn_end_without_completion_claim",
                "review_verdict": "unavailable",
                "recorded_at": "2026-07-14T10:05:00+00:00",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "attestations.jsonl",
        [
            {
                "order_id": "build-1",
                "session_ref": "host:lane-a:0.0",
                "logged_at": "2026-07-14T09:01:00+00:00",
                "attestation": {"operator_confirmation_required": True, "autonomy": "operator_required"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "queue_hygiene.jsonl",
        [
            {
                "action": "rollback",
                "session_ref": "host:lane-a:0.0",
                "logged_at": "2026-07-14T09:40:00+00:00",
            },
            {
                "action": "hold-red",
                "session_ref": "host:lane-b:0.0",
                "logged_at": "2026-07-14T10:04:00+00:00",
            },
        ],
    )
    (tmp_path / "pause_recovery.json").write_text(
        json.dumps(
            {
                "schema": "chitra.pause_recovery.v1",
                "records": [
                    {
                        "pause_id": "pause-1",
                        "session_ref": "host:lane-a:0.0",
                        "hold_reason": "rate-limit:5h",
                        "transcript_path": "/tmp/lane-a.jsonl",
                        "resume_note": "Resume the build.",
                        "resume_at": "2026-07-14T09:30:00+00:00",
                        "paused_at": "2026-07-14T09:25:00+00:00",
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    rollup = compute_outcomes(tmp_path, now=NOW)

    assert [family.family for family in rollup.families] == ["build", "untyped"]
    build, untyped = rollup.families
    assert build.dispatch_count == 2
    assert build.task_success_after_validation.ratio == 0.5
    assert build.task_success_after_validation.accepted_count == 1
    assert build.task_success_after_validation.completion_claim_review_count == 2
    assert build.retry_count == 1
    assert build.retries_per_dispatch == 0.5
    assert build.human_intervention_count == 2
    assert build.human_intervention_rate == 1.0
    assert build.escaped_defects == 1
    assert build.cycle_time_seconds == 1350.0
    assert untyped.dispatch_count == 1
    assert untyped.task_success_after_validation.ratio is None
    assert untyped.human_intervention_rate == 0.0
    assert untyped.escaped_defects == 0
    assert untyped.cycle_time_seconds == 300.0
    assert rollup.totals.dispatch_count == 3
    assert rollup.totals.retry_count == 1
    assert rollup.totals.escaped_defects == 1
    assert rollup.totals.cycle_time_seconds == 1000.0


def test_compute_outcomes_treats_missing_state_as_empty(tmp_path: Path) -> None:
    rollup = compute_outcomes(tmp_path, now=NOW)

    assert rollup.generated_at == NOW
    assert rollup.families == []
    assert rollup.totals.dispatch_count == 0
    assert rollup.totals.task_success_after_validation.ratio is None
    assert rollup.totals.human_intervention_rate == 0.0
    assert rollup.totals.retry_count == 0
    assert rollup.totals.escaped_defects == 0
    assert rollup.totals.cycle_time_seconds is None
