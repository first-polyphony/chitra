"""Deterministic full-message transcript reading for one Claude Code lane.

No LLM calls in this module — it reads the complete final assistant message
and extracts literal operator asks only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+")
_OPEN_ASK_HEADING_RE = re.compile(
    r"awaiting ruling|open (?:question|ask|decision)s?|decisions? (?:needed|for you)|"
    r"need(?:s)? (?:you|operator|trey)|for (?:you|trey) to (?:decide|rule)",
    re.IGNORECASE,
)
_NON_LIST_HEADING_RE = re.compile(r"^(?:#{1,6}\s+|(?:\*\*|__).*?(?:\*\*|__)\s*$|[^.!?]+:\s*$)")


def _assistant_message_text(payload: object) -> str | None:
    """Return assistant text for one parsed transcript record, if present."""
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    message_role = message.get("role") if isinstance(message, dict) else None
    if payload.get("type") != "assistant" and payload.get("role") != "assistant" and message_role != "assistant":
        return None
    content: object = message.get("content") if isinstance(message, dict) else payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text_blocks: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_blocks.append(block)
        elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            text_blocks.append(block["text"])
    return "".join(text_blocks)


def read_last_assistant_message(transcript_path: Path) -> str:
    """Return the full final assistant message from JSONL, never a pane window.

    Malformed transcript lines are logged and skipped so one interrupted write
    cannot prevent an idle-lane check from reading later valid records.
    """
    last_message = ""
    found_assistant = False
    with transcript_path.open(encoding="utf-8") as transcript:
        for line_number, line in enumerate(transcript, start=1):
            try:
                payload: Any = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("transcript_line_skipped", path=str(transcript_path), line_number=line_number)
                continue
            message = _assistant_message_text(payload)
            if message is not None:
                last_message = message
                found_assistant = True
    return last_message if found_assistant else ""


def _is_non_list_heading(line: str) -> bool:
    """Return whether a non-list line visibly begins a new text section."""
    return bool(_NON_LIST_HEADING_RE.match(line))


def extract_open_asks(message_text: str) -> list[str]:
    """Extract verbatim numbered open asks from a full assistant message only."""
    asks: list[str] = []
    seen: set[str] = set()
    in_open_ask_block = False
    for raw_line in message_text.splitlines():
        line = raw_line.strip()
        is_numbered = bool(_NUMBERED_ITEM_RE.match(raw_line))
        if is_numbered and (in_open_ask_block or "?" in line):
            if line not in seen:
                asks.append(line)
                seen.add(line)
            continue
        if _OPEN_ASK_HEADING_RE.search(line):
            in_open_ask_block = True
        elif in_open_ask_block and (not line or _is_non_list_heading(line)):
            in_open_ask_block = False
    return asks
