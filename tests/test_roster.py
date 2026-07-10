"""Tests for the compact terminal roster renderer in the board module."""

from __future__ import annotations

from typing import cast

import pytest

from chitra.board import ROSTER_GOAL_MAX_WIDTH, ROSTER_NOW_MAX_WIDTH, marker_for, render_roster
from chitra.goals import GoalRecord, GoalStatus


def _record(
    session_ref: str,
    status: GoalStatus,
    *,
    goal: str = "Keep this durable roster objective clear and verifiable.",
    now: str = "running checks",
) -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal=goal,
        done_when="Every required validation command passes cleanly.",
        source="branch",
        status=status,
        now=now,
    )


def test_marker_for_covers_every_status_and_rejects_unknown() -> None:
    assert marker_for("blocked") == "🔴"
    assert marker_for("held") == "🟡"
    assert marker_for("working") == "🟢"
    assert marker_for("done-pending-verification") == "🟢"
    assert marker_for("done-pending-close") == "🟢"
    with pytest.raises(ValueError, match="unknown goal status"):
        marker_for(cast(GoalStatus, "unknown"))


def test_render_roster_includes_every_lane_columns_markers_and_stable_order() -> None:
    records = [
        _record("zeta:build:0.0", "blocked"),
        _record("alpha:zeta:0.0", "done-pending-close"),
        _record("alpha:alpha:0.0", "held"),
    ]
    rendered = render_roster(records)

    assert "status-marker" in rendered
    assert rendered.index("status-marker") < rendered.index("Session") < rendered.index("Goal") < rendered.index("Now")
    assert all(name in rendered for name in ("build", "zeta", "alpha"))
    assert all(marker in rendered for marker in ("🔴", "🟡", "🟢"))
    assert rendered.index("alpha") < rendered.index("zeta") < rendered.index("build")
    assert rendered == render_roster(list(reversed(records)))


def test_box_and_markdown_include_all_sessions_and_truncate_cells() -> None:
    long = "word " * 40
    records = [
        _record("host:one:0.0", "working", goal=long, now=long),
        _record("other:two:0.0", "done-pending-verification"),
    ]
    box = render_roster(records)
    markdown = render_roster(records, fmt="markdown")

    assert "┌" in box and "│" in box
    assert markdown.startswith("| status-marker | Session | Goal | Now |")
    assert all(name in box and name in markdown for name in ("one", "two"))
    assert "…" in box and "…" in markdown
    long_box_goal = next(line for line in box.splitlines() if "one" in line)
    assert len(long_box_goal) < ROSTER_GOAL_MAX_WIDTH + ROSTER_NOW_MAX_WIDTH + 110


def test_render_roster_empty_store_is_a_small_no_crash_line() -> None:
    assert render_roster([]) == "no lanes recorded"
