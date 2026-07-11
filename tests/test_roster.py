"""Tests for the compact terminal roster renderer in the board module."""

from __future__ import annotations

from typing import cast

import pytest

from chitra.board import (
    ROSTER_GOAL_MAX_WIDTH,
    ROSTER_NEEDS_MAX_WIDTH,
    ROSTER_NOW_MAX_WIDTH,
    compute_marker,
    marker_for,
    render_roster,
)
from chitra.goals import GoalRecord, GoalStatus


def _record(
    session_ref: str,
    status: GoalStatus,
    *,
    goal: str = "Keep this durable roster objective clear and verifiable.",
    now: str = "running checks",
    open_asks: tuple[str, ...] = (),
    needs: str = "",
) -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal=goal,
        done_when="Every required validation command passes cleanly.",
        source="branch",
        status=status,
        now=now,
        open_asks=open_asks,
        needs=needs,
    )


def test_marker_for_covers_every_status_and_rejects_unknown() -> None:
    assert marker_for("blocked") == "🔴"
    assert marker_for("held") == "🟡"
    assert marker_for("idle") == "🟡"
    assert marker_for("working") == "🟢"
    assert marker_for("done-pending-verification") == "🟢"
    assert marker_for("done-pending-close") == "🟢"
    with pytest.raises(ValueError, match="unknown goal status"):
        marker_for(cast(GoalStatus, "unknown"))


@pytest.mark.parametrize(
    ("status", "open_asks", "expected"),
    [
        ("working", (), "🟢"),
        ("done-pending-verification", (), "🟢"),
        ("done-pending-close", (), "🟢"),
        ("held", (), "🟡"),
        ("idle", (), "🟡"),
        ("blocked", (), "🔴"),
        ("working", ("1. Decide the deployment window.",), "🔴"),
        ("held", ("1. Decide the deployment window.",), "🔴"),
    ],
)
def test_compute_marker_has_operator_precedence(
    status: GoalStatus, open_asks: tuple[str, ...], expected: str
) -> None:
    assert compute_marker(_record("host:lane:0.0", status, open_asks=open_asks)) == expected


def test_compute_marker_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="uncolorable status"):
        compute_marker(_record("host:lane:0.0", cast(GoalStatus, "unknown")))


def test_render_roster_includes_every_lane_columns_markers_and_stable_order() -> None:
    records = [
        _record("zeta:build:0.0", "blocked"),
        _record("alpha:zeta:0.0", "done-pending-close"),
        _record("alpha:alpha:0.0", "held"),
    ]
    rendered = render_roster(records)

    assert "marker" in rendered
    assert rendered.index("marker") < rendered.index("Session") < rendered.index("Goal") < rendered.index("Now") < rendered.index("Needs")
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
    assert markdown.startswith("| marker | Session | Goal | Now | Needs |")
    assert all(name in box and name in markdown for name in ("one", "two"))
    assert "…" in box and "…" in markdown
    long_box_goal = next(line for line in box.splitlines() if "one" in line)
    assert len(long_box_goal) < ROSTER_GOAL_MAX_WIDTH + ROSTER_NOW_MAX_WIDTH + ROSTER_NEEDS_MAX_WIDTH + 110


def test_render_roster_empty_store_is_a_small_no_crash_line() -> None:
    assert render_roster([]) == "no lanes recorded"


def test_roster_surfaces_every_open_ask_below_the_five_column_table() -> None:
    records = [
        _record("zeta:build:0.0", "blocked", open_asks=("1. Decide release window?",)),
        _record("alpha:review:0.0", "working", open_asks=("1. Approve tenancy.", "2. Choose rollback owner.")),
    ]

    box = render_roster(records)
    markdown = render_roster(records, fmt="markdown")
    for rendered in (box, markdown):
        assert "marker" in rendered and "Session" in rendered and "Goal" in rendered and "Now" in rendered and "Needs" in rendered
        assert "AWAITING RULING — surfaced every report until you rule" in rendered
        assert all(ask in rendered for record in records for ask in record.open_asks)
        assert rendered.index("alpha:review:0.0: 1. Approve tenancy.") < rendered.index("zeta:build:0.0: 1. Decide release window?")
    assert "  • alpha:review:0.0: 1. Approve tenancy." in box
    assert "- alpha:review:0.0: 1. Approve tenancy." in markdown


def test_render_roster_has_five_columns_needs_and_idle_by_design_markers() -> None:
    records = [
        _record("host:working:0.0", "working"),
        _record("host:idle:0.0", "idle"),
        _record("host:held:0.0", "held"),
        _record("host:blocked:0.0", "blocked", needs="you: run the interview"),
        _record("host:ask:0.0", "working", open_asks=("you: where does F2 live?",)),
    ]

    box = render_roster(records)
    markdown = render_roster(records, fmt="markdown")
    for rendered in (box, markdown):
        assert "🟢" in rendered and rendered.count("🟡") >= 2 and rendered.count("🔴") >= 2
        assert "you: run the interview" in rendered
        assert "you: where does F2 live?" in rendered
        assert "—" in rendered
    assert markdown.startswith("| marker | Session | Goal | Now | Needs |")
    idle_line = next(line for line in box.splitlines() if " idle " in line)
    assert "🟡" in idle_line
    assert "🟢" not in idle_line


def test_render_roster_truncates_long_needs_without_wrapping() -> None:
    long = "unblock " * 20
    rendered = render_roster([_record("host:blocked:0.0", "blocked", needs=long, goal=long, now=long)])

    row = next(line for line in rendered.splitlines() if "blocked" in line)
    assert row.count("│") == 6
    assert "…" in row
    assert len(row) <= 133


def test_roster_omits_an_empty_awaiting_ruling_block() -> None:
    rendered = render_roster([_record("host:lane:0.0", "working")])
    assert "AWAITING RULING" not in rendered
