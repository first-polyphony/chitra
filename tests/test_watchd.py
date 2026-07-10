"""Tests for chitra.watchd's pane normalization and triage handoff."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from chitra.triaged import parse_event_line
from chitra.watchd import Pane, Watchd, WatchdConfig, append_event, event_line, list_panes, normalize, resolve_config


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


def test_list_panes_uses_live_tmux_enumeration_and_deduplicates_pane_id() -> None:
    seen_commands: list[Sequence[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen_commands.append(command)
        return _completed(command, "%1\tfleet:0.0\n%2\tfleet:0.1\n%1\tduplicate:9.9\n")

    assert list_panes(runner=runner) == [Pane(pane_id="%1", target="fleet:0.0"), Pane(pane_id="%2", target="fleet:0.1")]
    assert seen_commands == [["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"]]


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

    config = resolve_config()

    assert config.events_log == tmp_path / "state" / "events.log"
    assert config.interval_seconds == 2.5
    assert config.panes_override == ("%1", "%2")
