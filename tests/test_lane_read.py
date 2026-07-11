"""Tests for deterministic full-message lane transcript reads."""

from __future__ import annotations

import json
from pathlib import Path

from chitra.lane_read import extract_open_asks, read_last_assistant_message


def _assistant(text: str) -> dict[str, object]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_read_last_assistant_message_returns_the_full_final_message_and_skips_bad_json(tmp_path: Path) -> None:
    long_final = "\n".join(["context line" for _ in range(60)] + ["This final sentence must not be truncated."])
    transcript = tmp_path / "lane.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(_assistant("earlier assistant message")),
                "{not json}",
                json.dumps({"type": "user", "message": {"content": "continue"}}),
                json.dumps(_assistant(long_final)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert read_last_assistant_message(transcript) == long_final
    assert "final sentence must not be truncated" in read_last_assistant_message(transcript)


def test_read_last_assistant_message_handles_string_content_and_no_assistant(tmp_path: Path) -> None:
    transcript = tmp_path / "lane.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": "complete text"}}) + "\n", encoding="utf-8")
    assert read_last_assistant_message(transcript) == "complete text"

    transcript.write_text(
        json.dumps(
            {"role": "assistant", "content": [{"type": "text", "text": "first "}, {"type": "text", "text": "second"}]}
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_last_assistant_message(transcript) == "first second"

    transcript.write_text(json.dumps({"type": "user", "content": "operator text"}) + "\n", encoding="utf-8")
    assert read_last_assistant_message(transcript) == ""


def test_extract_open_asks_reads_every_numbered_item_in_a_long_open_block_verbatim() -> None:
    asks = [
        "1. Approve the Folio shared-board tenancy choice?",
        "2) Decide whether #1888 can merge today.",
        "3. Name the operator who owns the release call.",
        "4) Confirm the rollback boundary remains unchanged.",
        "5. Rule on the final deployment window!",
    ]
    message = "\n".join(["Earlier context" for _ in range(50)] + ["Awaiting ruling:", *asks, "", "Next steps:", "prose"])

    assert extract_open_asks(message) == asks


def test_extract_open_asks_includes_standalone_questions_deduplicates_and_ignores_prose() -> None:
    message = "\n".join(
        (
            "1. Is this standalone question captured?",
            "Open questions:",
            "1. Preserve punctuation, please?!",
            "2) Preserve punctuation, please?!",
            "Status: done",
            "No operator action remains.",
        )
    )

    assert extract_open_asks(message) == [
        "1. Is this standalone question captured?",
        "1. Preserve punctuation, please?!",
        "2) Preserve punctuation, please?!",
    ]
    assert extract_open_asks("The lane completed its work without a question.") == []
