"""Tests for validated board-facts plumbing."""

from __future__ import annotations

from typing import Any

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
