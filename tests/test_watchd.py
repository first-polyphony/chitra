"""Tests for chitra.watchd's pane normalization and triage handoff."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from chitra.goal_enforcement import ReviewerVerdict
from chitra.goals import GoalRecord, get_goal, upsert_goal
from chitra.lane_activity import load_lane_activity
from chitra.triaged import parse_event_line
from chitra.watchd import (
    Pane,
    Watchd,
    WatchdConfig,
    append_event,
    build_arg_parser,
    event_line,
    list_panes,
    normalize,
    resolve_config,
)


def _completed(command: Sequence[str], stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=list(command), returncode=returncode, stdout=stdout, stderr="")


def test_normalize_removes_input_box_and_volatile_chrome() -> None:
    content = """useful state (12m 4s)
    ✻ thinking
tokens 12,345
Press up to edit
another useful state
❯ operator's unsent input
this is part of the live input box
"""

    assert normalize(content) == ["useful state", "another useful state"]


def test_watchd_emits_real_change_but_not_input_box_typing(tmp_path: Path) -> None:
    captures = iter(
        [
            "status: working\n❯ first operator draft\n",
            "status: working\n❯ a completely different operator draft\n",
            "status: blocked\n❯ operator draft remains unsent\n",
        ]
    )

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "capture-pane":
            return _completed(command, next(captures))
        raise AssertionError(f"unexpected command: {command}")

    events_log = tmp_path / "events.log"
    watcher = Watchd(
        WatchdConfig(state_dir=tmp_path, events_log=events_log, panes_override=("%7",)),
        runner=runner,
    )

    assert watcher.poll_once() == 0  # first capture establishes the baseline
    assert watcher.poll_once() == 0  # operator typing only is not a state change
    assert watcher.poll_once() == 1
    raw_captures = list((tmp_path / "watchd").glob("*.raw"))
    assert len(raw_captures) == 1
    assert "status: blocked" in raw_captures[0].read_text(encoding="utf-8")

    parsed = parse_event_line(events_log.read_text(encoding="utf-8"))
    assert parsed is not None
    _timestamp, lane_id, text = parsed
    assert lane_id == "%7"
    assert text == "CHANGE DETECTED: status: blocked"


class _AcceptingReviewer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def review(self, goal, behavior, reviewer_id: str) -> ReviewerVerdict:
        self.calls.append(reviewer_id)
        return ReviewerVerdict(
            reviewer_id=reviewer_id,
            goal_contract_id=goal.contract_id,
            behavior_sha256=behavior.behavior_sha256,
            verdict="accept",
        )


def _tracked_goal(root: Path) -> GoalRecord:
    return upsert_goal(
        root,
        GoalRecord(
            session_ref="localhost:fleet:0.0",
            intent="Deliver the requested gate while preserving every explicit operator boundary.",
            goal="Build and verify the requested forced completion review.",
            done_when="The live completion probe passes with cited evidence.",
            scope="WS1 source tests and documentation only.",
            source="task-file:/tmp/ws1.md",
            status="working",
        ),
    )


def test_turn_end_automatically_runs_review_and_marks_cited_completion_pending_close(tmp_path: Path) -> None:
    goal = _tracked_goal(tmp_path)
    captures = iter(
        [
            "working on the implementation\nesc to interrupt\n❯\n",
            """What was built: The forced completion review was completed and deployed at SHA abc1234.
What it does: It reviews every finished lane turn before any done state is trusted.
Does it actually work: Live health probe status=200 with 24 requests; /tmp/live-review.log.
❯
""",
        ]
    )

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\n")
        if command[1] == "capture-pane":
            return _completed(command, next(captures))
        raise AssertionError(f"unexpected command: {command}")

    reviewer = _AcceptingReviewer()
    watcher = Watchd(WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"), runner=runner, reviewer=reviewer)

    assert watcher.poll_once() == 0
    assert watcher.poll_once() == 1

    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "done-pending-close"
    assert stored.last_verified
    assert reviewer.calls == ["reviewer-1-1", "reviewer-1-2"]
    review = json.loads((tmp_path / "completion_reviews.jsonl").read_text(encoding="utf-8"))
    assert review["condition"] == "completion_claim"
    assert review["completion_verdict"] == "CLEAN"


def test_turn_end_without_claim_is_finished_unverified_not_idle_green(tmp_path: Path) -> None:
    goal = _tracked_goal(tmp_path)
    reviewer = _AcceptingReviewer()

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\n")
        if command[1] == "capture-pane":
            return _completed(command, "I need the exact release target before continuing.\n❯\n")
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(
        WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"),
        runner=runner,
        reviewer=reviewer,
    )
    watcher.poll_once()

    stored = get_goal(tmp_path, goal.session_ref)
    assert stored is not None
    assert stored.status == "turn-finished-unverified"
    assert "without a completion claim" in stored.now
    assert reviewer.calls == []
    review = json.loads((tmp_path / "completion_reviews.jsonl").read_text(encoding="utf-8"))
    assert review["review_verdict"] == "unavailable"
    assert "isolated review was not run" in review["summary"]


def test_list_panes_uses_live_tmux_enumeration_and_deduplicates_pane_id() -> None:
    seen_commands: list[Sequence[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen_commands.append(command)
        return _completed(command, "%1\tfleet:0.0\n%2\tfleet:0.1\n%1\tduplicate:9.9\n")

    assert list_panes(runner=runner) == [Pane(pane_id="%1", target="fleet:0.0"), Pane(pane_id="%2", target="fleet:0.1")]
    assert seen_commands == [
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_attached}\t#{pane_current_command}",
        ]
    ]


def test_watchd_persists_backend_neutral_change_recency_and_attachment(tmp_path: Path) -> None:
    _tracked_goal(tmp_path)

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if command[1] == "list-panes":
            return _completed(command, "%1\tfleet:0.0\t0\tcodex\n")
        if command[1] == "capture-pane":
            return _completed(command, "working on the requested change\n")
        raise AssertionError(f"unexpected command: {command}")

    watcher = Watchd(WatchdConfig(state_dir=tmp_path, events_log=tmp_path / "events.log"), runner=runner)
    watcher.poll_once()

    activity = load_lane_activity(tmp_path)
    assert len(activity) == 1
    assert activity[0].session_ref == "localhost:fleet:0.0"
    assert activity[0].attached is False
    assert activity[0].backend == "codex"
    assert activity[0].last_change_at


def test_list_panes_can_isolate_a_session_namespace() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return _completed(
            command,
            "%1\tmonitor:0.0\n%2\tboomtown:0.0\n%3\tboomtown-design-a:0.0\n%4\tother:0.0\n",
        )

    assert list_panes(runner=runner, session_prefixes=("boomtown-",)) == [Pane(pane_id="%3", target="boomtown-design-a:0.0")]
    assert list_panes(runner=runner, excluded_session_prefixes=("boomtown",)) == [
        Pane(pane_id="%1", target="monitor:0.0"),
        Pane(pane_id="%4", target="other:0.0"),
    ]


def test_event_line_matches_triaged_reader_contract() -> None:
    line = event_line("%9", ["state: waiting", "needs operator input"])

    parsed = parse_event_line(line)
    assert parsed is not None
    timestamp, lane_id, text = parsed
    assert timestamp.endswith("Z")
    assert lane_id == "%9"
    assert text == "CHANGE DETECTED: state: waiting | needs operator input"


def test_append_event_rotates_at_max_size_under_lock(tmp_path: Path) -> None:
    events_log = tmp_path / "events.log"
    events_log.write_text("old\n", encoding="utf-8")

    append_event(events_log, "new\n", max_log_bytes=4)

    assert (tmp_path / "events.log.1").read_text(encoding="utf-8") == "old\n"
    assert events_log.read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "events.log.lock").exists()


def test_resolve_config_uses_chitra_state_and_watchd_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHITRA_WATCHD_INTERVAL", "2.5")
    monkeypatch.setenv("CHITRA_WATCHD_PANES", "%1, %2")
    monkeypatch.setenv("CHITRA_WATCHD_SESSION_PREFIXES", "boomtown-, boomtown-review-")
    monkeypatch.setenv("CHITRA_WATCHD_EXCLUDE_SESSION_PREFIXES", "boomtown-control")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COUNT", "1")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COMMAND", "/opt/chitra/bin/review-with-monitor-credentials")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_MODEL", "operator-cheap-model")

    config = resolve_config()

    assert config.events_log == tmp_path / "state" / "events.log"
    assert config.interval_seconds == 2.5
    assert config.panes_override == ("%1", "%2")
    assert config.session_prefixes == ("boomtown-", "boomtown-review-")
    assert config.excluded_session_prefixes == ("boomtown-control",)
    assert config.reviewer_count == 1
    assert config.reviewer_command == "/opt/chitra/bin/review-with-monitor-credentials"
    assert config.reviewer_model == "operator-cheap-model"


def test_reviewer_config_precedence_is_cli_then_env_then_pinned_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    defaults = resolve_config(state_dir=tmp_path / "defaults")
    assert defaults.reviewer_count == 2
    assert defaults.reviewer_command == "claude"
    assert defaults.reviewer_model == "claude-haiku-4-5"

    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COUNT", "1")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_COMMAND", "env-claude")
    monkeypatch.setenv("CHITRA_WATCHD_REVIEWER_MODEL", "env-model")
    environment = resolve_config(state_dir=tmp_path / "environment")
    assert environment.reviewer_count == 1
    assert environment.reviewer_command == "env-claude"
    assert environment.reviewer_model == "env-model"

    args = build_arg_parser().parse_args(
        ["--reviewer-count", "3", "--reviewer-command", "cli-claude", "--reviewer-model", "cli-model"]
    )
    cli = resolve_config(
        state_dir=tmp_path / "cli",
        reviewer_count=args.reviewer_count,
        reviewer_command=args.reviewer_command,
        reviewer_model=args.reviewer_model,
    )
    assert cli.reviewer_count == 3
    assert cli.reviewer_command == "cli-claude"
    assert cli.reviewer_model == "cli-model"


@pytest.mark.parametrize("reviewer_count", [0, -1])
def test_resolve_config_rejects_non_positive_reviewer_count(tmp_path: Path, reviewer_count: int) -> None:
    with pytest.raises(ValueError, match="reviewer_count must be a positive integer"):
        resolve_config(state_dir=tmp_path, reviewer_count=reviewer_count)
