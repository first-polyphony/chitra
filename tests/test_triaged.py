"""Tests for chitra.triaged: state-transition dedup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chitra.triaged import critical_hits, parse_event_line, process_lines, run_once


def test_operator_aliases_default_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHITRA_OPERATOR_ALIASES", raising=False)

    assert critical_hits("waiting on trey") == []
    assert critical_hits("needs operator input") == [("needs_operator", "needs operator input")]


def test_operator_aliases_accept_one_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_OPERATOR_ALIASES", "trey")

    assert critical_hits("waiting on trey") == [("needs_operator", "waiting on trey")]


def test_operator_aliases_accept_comma_separated_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_OPERATOR_ALIASES", " alex, robin ")

    assert critical_hits("waiting on alex") == [("needs_operator", "waiting on alex")]
    assert critical_hits("needs robin") == [("needs_operator", "needs robin")]


def test_parse_event_line_extracts_ts_lane_text() -> None:
    parsed = parse_event_line("2026-07-09T13:00:00Z f3 CHANGE DETECTED lane state moved to stuck")
    assert parsed == ("2026-07-09T13:00:00Z", "f3", "CHANGE DETECTED lane state moved to stuck")


def test_parse_event_line_returns_none_for_blank_line() -> None:
    assert parse_event_line("\n") is None
    assert parse_event_line("   ") is None


def test_process_lines_dedups_same_signature_emits_once() -> None:
    state: dict[str, str] = {}
    lines = [
        "2026-07-09T13:00:00Z f3 HEARTBEAT nothing new\n",
        "2026-07-09T13:05:00Z f3 HEARTBEAT nothing new\n",  # identical text -> no new event
        "2026-07-09T13:10:00Z f3 HEARTBEAT nothing new\n",  # identical text -> no new event
    ]
    emitted = process_lines(lines, state=state, triage_log=Path("/dev/null"))
    assert emitted == 1


def test_process_lines_emits_on_actual_transition(tmp_path: Path) -> None:
    triage_log = tmp_path / "triaged.log"
    state: dict[str, str] = {}
    lines = [
        "2026-07-09T13:00:00Z f3 state=working\n",
        "2026-07-09T13:05:00Z f3 state=stuck\n",  # real transition
    ]
    emitted = process_lines(lines, state=state, triage_log=triage_log)
    assert emitted == 2
    logged = [json.loads(line) for line in triage_log.read_text(encoding="utf-8").splitlines()]
    assert [entry["text"] for entry in logged] == ["state=working", "state=stuck"]


def test_run_once_tracks_offset_and_does_not_reread_old_lines(tmp_path: Path) -> None:
    events_log = tmp_path / "events.log"
    state_file = tmp_path / "state.json"
    triage_log = tmp_path / "triaged.log"

    events_log.write_text("2026-07-09T13:00:00Z f3 state=working\n", encoding="utf-8")
    emitted_1 = run_once(events_log, state_file=state_file, triage_log=triage_log)
    assert emitted_1 == 1

    # No new lines appended -> a second run must emit nothing (not reread
    # the same line via the byte offset).
    emitted_2 = run_once(events_log, state_file=state_file, triage_log=triage_log)
    assert emitted_2 == 0

    with events_log.open("a", encoding="utf-8") as fh:
        fh.write("2026-07-09T13:10:00Z f3 state=stuck\n")
    emitted_3 = run_once(events_log, state_file=state_file, triage_log=triage_log)
    assert emitted_3 == 1


def test_process_lines_logs_warning_and_skips_unparseable(tmp_path: Path) -> None:
    # A single token with no whitespace fails the "<ts> <lane> <text>"
    # contract (needs at least two whitespace-separated fields).
    state: dict[str, str] = {}
    lines = ["single-token-no-whitespace\n"]
    emitted = process_lines(lines, state=state, triage_log=tmp_path / "triaged.log")
    assert emitted == 0
