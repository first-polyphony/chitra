"""Tests for the deterministic operator-facing board renderer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from chitra.board import _shell_quote, capture_tail, render, render_board
from chitra.board_updater import validate_board_facts


def _facts() -> dict[str, Any]:
    return {
        "generated_note": "A < B",
        "snapshot_owner": "monitor",
        "sessions": [
            {
                "id": "row-work",
                "name": "Working",
                "sid": "working · host",
                "wants": False,
                "goal": "ship <safe>",
                "doing": "testing",
                "you": None,
                "state": {"word": "WORKING", "cls": "st-work", "extra": ""},
                "detail": [{"kv": "status", "text": "all good"}],
                "tmux": {"host": "unconfigured", "session": "working"},
            },
            {
                "id": "row-needs",
                "name": "Needs",
                "sid": "needs · host",
                "wants": True,
                "goal": "answer",
                "doing": "waiting",
                "you": "please decide",
                "state": {"word": "WAITING", "cls": "st-you", "extra": ""},
                "detail": [{"text": "operator response needed"}],
                "tmux": {"host": "unconfigured", "session": "needs"},
            },
            {
                "id": "row-done",
                "name": "Done",
                "sid": "done · host",
                "wants": False,
                "goal": "finish",
                "doing": "closed",
                "you": None,
                "state": {"word": "DONE", "cls": "st-done", "extra": ""},
                "detail": [{"text": "finished"}],
                "tmux": {"host": "unconfigured", "session": "done"},
            },
        ],
        "log": [{"t": "now", "chip": "WORK", "chip_target": "row-work", "text": "A < B"}],
        "selfcheck": {"solid": "yes", "weak": "", "unsure": ""},
    }


def test_render_produces_interactive_html_from_valid_facts() -> None:
    output = render(_facts(), epoch=0, local_host="local")

    assert "{{" not in output
    assert "A &lt; B" in output
    assert output.index('id="row-needs"') < output.index('id="row-work"')
    assert 'id="row-done"' not in output
    assert "session closed · closed" in output
    assert "tail unavailable: host is not configured for board capture" in output
    assert "setTimeout(function(){location.reload()},90000)" in output


def test_render_does_not_rescan_replacement_values_for_template_tokens() -> None:
    facts = _facts()
    facts["generated_note"] = "{{snapshot_stamp}}"

    output = render(
        facts,
        template="{{generated_note_html}} / {{snapshot_stamp}}",
        epoch=0,
        local_host="local",
    )

    assert output.startswith("{{snapshot_stamp}} / ")


def test_capture_tail_uses_tmux_for_local_session(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="recent output\n", stderr=""))
    monkeypatch.setattr("chitra.board.subprocess.run", run)

    tail = capture_tail(
        {"tmux": {"host": "local", "session": "receiving"}},
        local_host="local",
        remote_hosts=set(),
        remote_user="ubuntu",
    )

    assert tail == "recent output"
    run.assert_called_once_with(
        ["tmux", "capture-pane", "-p", "-J", "-t", "receiving", "-S", "-60"],
        text=True,
        capture_output=True,
        timeout=6,
        check=False,
    )


def test_capture_tail_quotes_remote_session_name(monkeypatch: pytest.MonkeyPatch) -> None:
    target = "queue'; `not-a-command` $(still-not-a-command)"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="recent output\n", stderr=""))
    monkeypatch.setattr("chitra.board.subprocess.run", run)

    tail = capture_tail(
        {"tmux": {"host": "relay", "session": target}},
        local_host="local",
        remote_hosts={"relay"},
        remote_user="board",
    )

    assert tail == "recent output"
    assert _shell_quote(target) == "'queue'\"'\"'; `not-a-command` $(still-not-a-command)'"
    run.assert_called_once_with(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "board@relay",
            "tmux capture-pane -p -J -t 'queue'\"'\"'; `not-a-command` $(still-not-a-command)' -S -60",
        ],
        text=True,
        capture_output=True,
        timeout=6,
        check=False,
    )


def test_capture_tail_degrades_when_capture_command_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chitra.board.subprocess.run", Mock(side_effect=FileNotFoundError("tmux not found")))

    tail = capture_tail(
        {"tmux": {"host": "local", "session": "receiving"}},
        local_host="local",
        remote_hosts=set(),
        remote_user="ubuntu",
    )

    assert tail == "tail unavailable: tmux not found"


def test_render_board_writes_index_and_health_atomically(tmp_path: Path) -> None:
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    (board_dir / "facts.json").write_text(json.dumps(_facts()), encoding="utf-8")

    index = render_board(board_dir, local_host="local")

    assert index == board_dir / "index.html"
    assert "Fleet Session Board" in index.read_text(encoding="utf-8")
    assert json.loads((board_dir / "health.json").read_text(encoding="utf-8"))["ok"] is True


def test_render_board_preserves_existing_index_and_marks_unhealthy_on_invalid_facts(tmp_path: Path) -> None:
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    index = board_dir / "index.html"
    index.write_text("keep-existing-board", encoding="utf-8")
    (board_dir / "facts.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="facts.json root must be an object"):
        render_board(board_dir)

    assert index.read_text(encoding="utf-8") == "keep-existing-board"
    assert json.loads((board_dir / "health.json").read_text(encoding="utf-8"))["ok"] is False


def test_validate_board_facts_rejects_a_missing_rendered_field() -> None:
    facts = _facts()
    del facts["sessions"][0]["tmux"]

    result = validate_board_facts(facts)

    assert result.ok is False
    assert "$.sessions[0].tmux must be dict" in result.errors


def test_validate_board_facts_honors_deployment_constraints() -> None:
    result = validate_board_facts(_facts(), expected_owner="other", valid_hosts={"allowed"})

    assert result.ok is False
    assert "$.snapshot_owner must be exactly 'other'" in result.errors
    assert "$.sessions[0].tmux.host must be one of ['allowed']" in result.errors
