"""Tests for triaged's queue/flag compatibility artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from chitra.triaged import ReceivingOutputs, run_once


def test_receiving_outputs_classify_alerts_and_ignore_command_echoes(tmp_path: Path) -> None:
    events = tmp_path / "events.log"
    state = tmp_path / "state.json"
    triage_log = tmp_path / "triaged.log"
    outputs = ReceivingOutputs(
        queue_file=tmp_path / "queue.tsv",
        flags_file=tmp_path / "flags.log",
        stats_file=tmp_path / "stats.json",
        alert_state_file=tmp_path / "alerts.json",
    )
    events.write_text(
        "2026-07-10T12:00:00Z lane-1 gh pr view 12 --json state MERGED | needs operator input\n",
        encoding="utf-8",
    )

    assert run_once(events, state_file=state, triage_log=triage_log, receiving_outputs=outputs) == 1

    queue = outputs.queue_file.read_text(encoding="utf-8")
    assert "\tCRIT\tlane-1\tneeds_operator\tneeds operator input" in queue
    flags = outputs.flags_file.read_text(encoding="utf-8")
    assert "needs_operator" in flags
    assert "merge_landed" not in flags
    stats = json.loads(outputs.stats_file.read_text(encoding="utf-8"))
    assert stats["events"] == 1
    assert stats["changes"] == 1
    assert stats["crit_raw"] == 1
    assert stats["crit_emitted"] == 1


def test_receiving_outputs_classify_every_other_critical_rule_and_info(tmp_path: Path) -> None:
    events = tmp_path / "events.log"
    state = tmp_path / "state.json"
    triage_log = tmp_path / "triaged.log"
    outputs = ReceivingOutputs(
        queue_file=tmp_path / "queue.tsv",
        flags_file=tmp_path / "flags.log",
        stats_file=tmp_path / "stats.json",
        alert_state_file=tmp_path / "alerts.json",
    )
    events.write_text(
        "2026-07-10T12:00:00Z lane-merge Merged #123\n"
        "2026-07-10T12:01:00Z lane-crash Traceback (most recent call last)\n"
        "2026-07-10T12:02:00Z lane-ci CI required check failed\n"
        "2026-07-10T12:03:00Z lane-blocked BLOCKED pending operator decision\n"
        "2026-07-10T12:04:00Z lane-rate usage at 92% of rate limit\n"
        "2026-07-10T12:05:00Z lane-info routine progress update\n",
        encoding="utf-8",
    )

    assert run_once(events, state_file=state, triage_log=triage_log, receiving_outputs=outputs) == 6

    queue = outputs.queue_file.read_text(encoding="utf-8")
    flags = outputs.flags_file.read_text(encoding="utf-8")
    for lane, rule in (
        ("lane-merge", "merge_landed"),
        ("lane-crash", "crash"),
        ("lane-ci", "ci_red"),
        ("lane-blocked", "blocked"),
        ("lane-rate", "rate_limit"),
    ):
        assert f"\tCRIT\t{lane}\t{rule}\t" in queue
        assert f" {lane} {rule}:" in flags
    assert "\tINFO\tlane-info\t-\troutine progress update" in queue
    assert "lane-info" not in flags


def test_receiving_outputs_persist_alert_dedup_across_transitions(tmp_path: Path) -> None:
    events = tmp_path / "events.log"
    state = tmp_path / "state.json"
    outputs = ReceivingOutputs(
        queue_file=tmp_path / "queue.tsv",
        flags_file=tmp_path / "flags.log",
        stats_file=tmp_path / "stats.json",
        alert_state_file=tmp_path / "alerts.json",
    )
    events.write_text("2026-07-10T12:00:00Z lane-1 needs operator input\n", encoding="utf-8")
    run_once(events, state_file=state, triage_log=tmp_path / "triaged.log", receiving_outputs=outputs)
    with events.open("a", encoding="utf-8") as output:
        output.write("2026-07-10T12:01:00Z lane-1 state changed | needs operator input\n")

    assert run_once(events, state_file=state, triage_log=tmp_path / "triaged.log", receiving_outputs=outputs) == 1
    assert len(outputs.queue_file.read_text(encoding="utf-8").splitlines()) == 2
    assert len(outputs.flags_file.read_text(encoding="utf-8").splitlines()) == 1


def test_run_once_restarts_at_new_file_after_inode_rotation(tmp_path: Path) -> None:
    events = tmp_path / "events.log"
    state = tmp_path / "state.json"
    triage_log = tmp_path / "triaged.log"
    events.write_text("2026-07-10T12:00:00Z lane-1 old\n", encoding="utf-8")
    assert run_once(events, state_file=state, triage_log=triage_log) == 1

    rotated = tmp_path / "events.log.1"
    events.replace(rotated)
    events.write_text(
        "2026-07-10T12:01:00Z lane-1 replacement-longer-than-old-file\n",
        encoding="utf-8",
    )

    assert run_once(events, state_file=state, triage_log=triage_log) == 1
