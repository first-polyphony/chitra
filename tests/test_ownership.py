"""Tests for the read-only Watchtower ownership query."""

from __future__ import annotations

from dataclasses import dataclass

from chitra.goals import GoalStatus
from chitra.ownership import query_ownership


@dataclass(frozen=True)
class _TrackedLane:
    session_ref: str
    goal_status: GoalStatus | None


def test_ownership_query_partitions_owned_and_unowned_session_refs() -> None:
    lanes = [
        _TrackedLane("host-b:working:0.0", "working"),
        _TrackedLane("host-b:blocked:0.0", "blocked"),
        _TrackedLane("host-a:other-host:0.0", "working"),
    ]

    result = query_ownership(
        host="host-b",
        session_refs=("host-b:missing:0.0", "host-b:working:0.0", "host-b:blocked:0.0"),
        tracked_lanes=lanes,
    )

    assert result.owned is True
    assert result.owned_session_refs == ("host-b:working:0.0",)
    assert result.unowned_session_refs == ("host-b:blocked:0.0", "host-b:missing:0.0")


def test_ownership_query_is_false_when_no_current_working_lane_matches() -> None:
    result = query_ownership(
        host="host-b",
        session_refs=("host-b:held:0.0",),
        tracked_lanes=[_TrackedLane("host-b:held:0.0", "held")],
    )
    assert result.owned is False
