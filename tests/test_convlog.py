"""Tests for deterministic operator-brief rendering and conversation logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.convlog import (
    BriefValidationError,
    OperatorBrief,
    append_directive,
    append_entry,
    append_ruling,
    append_session_message,
    entries_for_thread,
    list_threads,
    main,
    open_thread,
    pending_threads,
    read_entries,
    render_brief,
    render_group,
    validate_brief,
)


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_ref": "tophand:feeds:0.0",
        "program": "Feeds digest redesign (F2)",
        "subject": "Feeds digest compiler",
        "progress": "implementation-ready; final interface choice pending",
        "stage": "The implementation is ready for the final interface choice.",
        "category": "decision",
        "decision": "Should the digest ship as one combined feed?",
        "recommendation": "Ship one combined feed because the tested readers preferred it.",
        "recommendation_basis": "research",
        "options": [
            {"label": "Combined feed", "consequence": "Readers get one ranked digest."},
            {"label": "Separate feeds", "consequence": "Readers choose a source first."},
        ],
        "source_quote": ["The combined prototype passed the reader test.", "I need the operator's product decision."],
        "source_ref": "transcripts/feeds.jsonl",
    }
    payload.update(changes)
    return payload


def _brief(**changes: object) -> OperatorBrief:
    return validate_brief(_payload(**changes))


def test_valid_brief_round_trip_render_log_and_read_back(tmp_path: Path) -> None:
    brief = _brief()
    thread_id = open_thread(tmp_path / "conversation.jsonl", brief=brief, raw_text="Full raw session message.")

    entries = entries_for_thread(tmp_path / "conversation.jsonl", thread_id)
    assert [entry.kind for entry in entries] == ["session_msg", "operator_brief"]
    assert entries[0].payload == {"text": "Full raw session message.", "source_ref": brief.source_ref}
    assert entries[1].schema_ == "chitra.convlog.v2"
    assert validate_brief(entries[1].payload["brief"]) == brief
    assert entries[1].payload["brief"]["subject"] == "Feeds digest compiler"
    assert entries[1].payload["brief"]["progress"] == "implementation-ready; final interface choice pending"
    assert entries[1].payload["rendered"] == render_brief(brief)
    assert not list(tmp_path.glob("*.tmp"))


def test_v1_entry_loads_with_empty_grounding_fields(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    legacy_brief = _payload()
    legacy_brief.pop("subject")
    legacy_brief.pop("progress")
    legacy_entry = {
        "schema": "chitra.convlog.v1",
        "thread_id": "legacy-thread",
        "seq": 1,
        "kind": "operator_brief",
        "at": "2026-07-11T12:00:00+00:00",
        "session_ref": "tophand:feeds:0.0",
        "payload": {"brief": legacy_brief, "rendered": "legacy rendered brief"},
    }
    path.write_text(json.dumps(legacy_entry) + "\n", encoding="utf-8")

    entries = read_entries(path)

    assert entries[0].schema_ == "chitra.convlog.v1"
    loaded_brief = validate_brief(entries[0].payload["brief"])
    assert loaded_brief.subject == ""
    assert loaded_brief.progress == ""
    assert render_brief(loaded_brief).splitlines()[0] == "This is Feeds digest redesign (F2) (tophand:feeds:0.0)."


@pytest.mark.parametrize("program", ["F2", "fix-6", "1-109", "tophand:F2:1"])
def test_program_rejects_bare_codenames_and_session_refs(program: str) -> None:
    with pytest.raises(BriefValidationError, match="plain-language program"):
        validate_brief(_payload(program=program))


def test_program_accepts_plain_language_name_with_codename() -> None:
    assert _brief().program == "Feeds digest redesign (F2)"


def test_decision_without_recommendation_requires_research_first() -> None:
    with pytest.raises(BriefValidationError, match="monitor does the research first"):
        _brief(recommendation="")


def test_operator_preference_basis_allows_no_recommendation() -> None:
    brief = _brief(recommendation="", recommendation_basis="operator-preference")
    assert "Recommendation: your call — no research applies." in render_brief(brief)


@pytest.mark.parametrize("quotes", [[], ["one"] * 5, ["x" * 401]])
def test_source_quote_bounds_are_enforced(quotes: list[str]) -> None:
    with pytest.raises(BriefValidationError):
        _brief(source_quote=quotes)


def test_category_decision_requires_decision_but_milestone_may_ask() -> None:
    with pytest.raises(BriefValidationError, match="category is decision"):
        _brief(decision=None)

    milestone = _brief(category="milestone")
    assert render_brief(milestone).splitlines()[1].startswith("🔴 ")


def test_render_snapshots() -> None:
    assert render_brief(_brief()) == (
        "This is Feeds digest redesign (F2) (tophand:feeds:0.0) working on Feeds digest compiler: "
        "implementation-ready; final interface choice pending.\n"
        "🔴 Feeds digest redesign (F2) (tophand:feeds:0.0) — needs you: Should the digest ship as one combined feed?\n"
        "Stage: The implementation is ready for the final interface choice.\n"
        "Recommendation: Ship one combined feed because the tested readers preferred it.\n"
        "Options (reply by number):\n"
        "  1. Combined feed — Readers get one ranked digest.\n"
        "  2. Separate feeds — Readers choose a source first.\n"
        "— from the session, verbatim —\n"
        "> The combined prototype passed the reader test.\n"
        "> I need the operator's product decision."
    )
    fyi = _brief(category="fyi", decision=None, recommendation="", options=[])
    assert render_brief(fyi) == (
        "This is Feeds digest redesign (F2) (tophand:feeds:0.0) working on Feeds digest compiler: "
        "implementation-ready; final interface choice pending.\n"
        "🟦 Feeds digest redesign (F2) (tophand:feeds:0.0) — fyi; nothing to answer yet.\n"
        "Stage: The implementation is ready for the final interface choice.\n"
        "— from the session, verbatim —\n"
        "> The combined prototype passed the reader test.\n"
        "> I need the operator's product decision."
    )


def test_decisionless_brief_says_nothing_is_ready_to_answer() -> None:
    brief = _brief(category="milestone", decision=None, recommendation="I will return with a recommendation.", options=[])

    rendered = render_brief(brief)

    assert "nothing to answer yet" in rendered
    assert "needs you:" not in rendered


def test_render_group_numbers_briefs(tmp_path: Path) -> None:
    first = _brief()
    second = _brief(session_ref="tophand:other:0.0", program="Other program (F3)")
    first_thread = open_thread(tmp_path / "conversation.jsonl", brief=first, raw_text="first")
    second_thread = open_thread(tmp_path / "conversation.jsonl", brief=second, raw_text="second")
    grouped = render_group(pending_threads(tmp_path / "conversation.jsonl"), now=datetime.now(UTC))

    assert first_thread in {thread.thread_id for thread in pending_threads(tmp_path / "conversation.jsonl")}
    assert second_thread in {thread.thread_id for thread in pending_threads(tmp_path / "conversation.jsonl")}
    assert grouped.startswith("[1] — open 0m\n  This is")
    assert "\n  🔴" in grouped
    assert "\n\n[2] — open 0m\n  This is" in grouped


def test_cli_four_rung_lifecycle_show_list_and_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "conversation.jsonl"
    json_path = tmp_path / "brief.json"
    json_path.write_text(json.dumps(_payload()), encoding="utf-8")
    assert main(["brief", "--convlog-path", str(path), "--session-ref", "tophand:feeds:0.0", "--json", str(json_path), "--raw", "raw"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("This is")
    thread_id = captured.err.strip().removeprefix("thread=")

    assert main(["pending", "--convlog-path", str(path)]) == 0
    assert "[1] — open" in capsys.readouterr().out
    assert main(["rule", "--convlog-path", str(path), "--thread", thread_id, "--text", "Ship option 1."]) == 0
    assert (
        main(
            ["directive", "--convlog-path", str(path), "--thread", thread_id, "--text", "Ship the combined feed.", "--order-id", "ord-1"]
        )
        == 0
    )
    assert main(["show", "--convlog-path", str(path), "--thread", thread_id]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["kind"] for line in shown] == ["session_msg", "operator_brief", "operator_ruling", "lane_directive"]
    assert main(["list", "--convlog-path", str(path)]) == 0
    assert "\truled\t" in capsys.readouterr().out
    assert main(["pending", "--convlog-path", str(path)]) == 0
    assert capsys.readouterr().out == "No pending decisions.\n"


def test_batch_rule_and_revision_make_latest_brief_authoritative(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "conversation.jsonl"
    first = open_thread(path, brief=_brief(), raw_text="first")
    second = open_thread(path, brief=_brief(session_ref="tophand:second:0.0", program="Second program (F3)"), raw_text="second")

    assert main(["rule", "--convlog-path", str(path), "--thread", first, "--thread", second, "--text", "Proceed.", "--via", "slack"]) == 0
    assert pending_threads(path) == []
    revised = _brief(decision="Should the combined feed include alerts?", recommendation="No; keep alerts separate.")
    revised_path = tmp_path / "revised.json"
    revised_path.write_text(json.dumps(revised.model_dump()), encoding="utf-8")
    assert (
        main(
            [
                "brief",
                "--convlog-path",
                str(path),
                "--thread",
                first,
                "--session-ref",
                revised.session_ref,
                "--json",
                str(revised_path),
            ]
        )
        == 0
    )
    assert "thread=" + first in capsys.readouterr().err
    pending = pending_threads(path)
    assert [thread.thread_id for thread in pending] == [first]
    assert pending[0].latest_brief.decision == "Should the combined feed include alerts?"


def test_malformed_lines_are_skipped_and_sequence_is_monotonic(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    thread_id = "abc123"
    append_session_message(path, thread_id=thread_id, session_ref="tophand:f:0", text="raw", source_ref="source")
    path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    append_entry(path, thread_id=thread_id, kind="operator_ruling", session_ref="tophand:f:0", payload={"text": "ok", "via": "chat"})

    assert [entry.seq for entry in read_entries(path)] == [1, 2]


def test_pending_age_rendering_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    brief = _brief()
    append_session_message(path, thread_id="age-test", session_ref=brief.session_ref, text="raw", source_ref=brief.source_ref)
    append_entry(
        path,
        thread_id="age-test",
        kind="operator_brief",
        session_ref=brief.session_ref,
        payload={"brief": brief.model_dump(), "rendered": render_brief(brief)},
        at="2026-07-11T10:00:00+00:00",
    )
    now = datetime(2026, 7, 11, 13, 30, tzinfo=UTC)
    assert "[1] — open 3h" in render_group(pending_threads(path), now=now)


def test_direct_helpers_complete_four_rungs(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    thread_id = open_thread(path, brief=_brief(), raw_text="raw")
    append_ruling(path, thread_id=thread_id, text="yes")
    append_directive(path, thread_id=thread_id, text="do it", order_id=None)
    assert list_threads(path)[0].pending is False
