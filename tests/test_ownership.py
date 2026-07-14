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
        _TrackedLane("tophand:working:0.0", "working"),
        _TrackedLane("tophand:blocked:0.0", "blocked"),
        _TrackedLane("trailhead:other-host:0.0", "working"),
    ]

    result = query_ownership(
        host="tophand",
        session_refs=("tophand:missing:0.0", "tophand:working:0.0", "tophand:blocked:0.0"),
        tracked_lanes=lanes,
    )

    assert result.owned is True
    assert result.owned_session_refs == ("tophand:working:0.0",)
    assert result.unowned_session_refs == ("tophand:blocked:0.0", "tophand:missing:0.0")


def test_ownership_query_is_false_when_no_current_working_lane_matches() -> None:
    result = query_ownership(
        host="tophand",
        session_refs=("tophand:held:0.0",),
        tracked_lanes=[_TrackedLane("tophand:held:0.0", "held")],
    )
    assert result.owned is False
