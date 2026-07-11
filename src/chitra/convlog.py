"""Validate, render, and record operator-brief conversation threads.

The fleet monitor's messages to the operator today mostly echo a lane's raw
text. This module is the deterministic half of an interpretive translation
layer: the CALLER (the monitor harness LLM) composes an OperatorBrief — session
context, process stage, the pending decision, a recommendation with research
already folded in — and this module validates it, renders it in a fixed BLUF
(bottom-line-up-front) layout, and records the full four-state exchange (raw
session message → operator brief → operator ruling → lane directive) in an
append-only conversation log. No LLM calls here: chitra validates, renders,
and logs; it never composes.

No LLM calls in this module's own code path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from chitra.state_paths import default_convlog_path

logger = structlog.get_logger(__name__)

SCHEMA: Literal["chitra.convlog.v2"] = "chitra.convlog.v2"
type EntryKind = Literal["session_msg", "operator_brief", "operator_ruling", "lane_directive"]
type BriefCategory = Literal["decision", "incident", "milestone", "fyi"]
type RecommendationBasis = Literal["research", "operator-preference"]
_BARE_CODENAME = re.compile(r"(?:[A-Za-z]*-?\d+|\d+-\d+)", re.IGNORECASE)


class BriefValidationError(ValueError):
    """Raised when caller-supplied operator-brief data is invalid."""


class ConversationNotFoundError(KeyError):
    """Raised when an operation requires a conversation thread that is absent."""


class BriefOption(BaseModel):
    """One numbered answer choice supplied by the monitor harness."""

    label: str = Field(min_length=1)
    consequence: str = Field(min_length=1)

    @field_validator("label", "consequence")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value


class OperatorBrief(BaseModel):
    """A caller-composed, deterministic-to-render operator brief."""

    session_ref: str = Field(min_length=1)
    program: str = Field(min_length=1)
    subject: str = ""
    progress: str = ""
    stage: str = Field(min_length=1, max_length=140)
    category: BriefCategory
    decision: str | None = None
    recommendation: str = ""
    recommendation_basis: RecommendationBasis = "research"
    options: list[BriefOption] = Field(default_factory=list)
    source_quote: list[str] = Field(min_length=1, max_length=4)
    source_ref: str = Field(min_length=1)

    @field_validator("session_ref", "source_ref")
    @classmethod
    def _required_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @field_validator("program")
    @classmethod
    def _program_is_plain_language(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("program must be non-empty")
        if _BARE_CODENAME.fullmatch(stripped) or (":" in stripped and " " not in stripped):
            raise ValueError("use the plain-language program name, optionally with the codename in parentheses")
        return value

    @field_validator("stage")
    @classmethod
    def _stage_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("stage must be non-empty")
        return value

    @field_validator("decision")
    @classmethod
    def _decision_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("decision must be non-empty when provided")
        return value

    @field_validator("source_quote")
    @classmethod
    def _source_quotes_are_verbatim_anchors(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.strip():
                raise ValueError("source quotes must be non-empty")
            if len(value) > 400:
                raise ValueError("source quotes must be at most 400 characters")
        return values

    @model_validator(mode="after")
    def _decision_has_research_or_explicit_preference(self) -> OperatorBrief:
        if self.category == "decision" and self.decision is None:
            raise ValueError("decision must be non-empty when category is decision")
        if self.decision is not None and not self.recommendation.strip() and self.recommendation_basis != "operator-preference":
            raise ValueError(
                "the monitor does the research first and folds the result in — "
                'a decision brief may not punt with "would benefit from more research"'
            )
        return self


class ConversationEntry(BaseModel):
    """One append-only record in the four-rung operator conversation log."""

    schema_: Literal["chitra.convlog.v1", "chitra.convlog.v2"] = Field(default=SCHEMA, alias="schema")
    thread_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    kind: EntryKind
    at: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    payload: dict[str, Any]


class _SessionMessagePayload(BaseModel):
    text: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


class _OperatorBriefPayload(BaseModel):
    brief: OperatorBrief
    rendered: str


class _OperatorRulingPayload(BaseModel):
    text: str = Field(min_length=1)
    via: Literal["chat", "in-pane", "slack"]


class _LaneDirectivePayload(BaseModel):
    text: str = Field(min_length=1)
    order_id: str | None


@dataclass(frozen=True, slots=True)
class ConversationThread:
    """The current, derived state of one append-only conversation thread."""

    thread_id: str
    session_ref: str
    opened_at: str
    latest_brief: OperatorBrief
    latest_brief_at: str
    pending: bool
    entries: tuple[ConversationEntry, ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("conversation entry timestamp must be timezone-aware")
    return parsed


def validate_brief(payload: object) -> OperatorBrief:
    """Validate one caller payload and normalize Pydantic errors for callers."""
    try:
        return OperatorBrief.model_validate(payload)
    except ValidationError as exc:
        raise BriefValidationError(str(exc)) from exc


def _validated_payload(entry: ConversationEntry) -> dict[str, Any]:
    """Validate one kind-specific envelope payload before exposing it to readers."""
    match entry.kind:
        case "session_msg":
            return _SessionMessagePayload.model_validate(entry.payload).model_dump(mode="json")
        case "operator_brief":
            return _OperatorBriefPayload.model_validate(entry.payload).model_dump(mode="json")
        case "operator_ruling":
            return _OperatorRulingPayload.model_validate(entry.payload).model_dump(mode="json")
        case "lane_directive":
            return _LaneDirectivePayload.model_validate(entry.payload).model_dump(mode="json")


def _grounding_line(brief: OperatorBrief) -> str:
    """Render the v2 context lead-in while keeping v1 records readable."""
    line = f"This is {brief.program} ({brief.session_ref})"
    if brief.subject.strip():
        line = f"{line} working on {brief.subject}"
    if brief.progress.strip():
        line = f"{line}: {brief.progress}"
    return f"{line}."


def render_brief(brief: OperatorBrief) -> str:
    """Render one brief in the fixed plain-text BLUF layout."""
    if brief.decision is not None:
        lines = [
            _grounding_line(brief),
            f"🔴 {brief.program} ({brief.session_ref}) — needs you: {brief.decision}",
            f"Stage: {brief.stage}",
        ]
        if brief.recommendation_basis == "operator-preference":
            recommendation = "Recommendation: your call — no research applies."
            if brief.recommendation:
                recommendation = f"{recommendation} {brief.recommendation}"
            lines.append(recommendation)
        else:
            lines.append(f"Recommendation: {brief.recommendation}")
        if brief.options:
            lines.append("Options (reply by number):")
            lines.extend(f"  {index}. {option.label} — {option.consequence}" for index, option in enumerate(brief.options, start=1))
    else:
        marker = {"incident": "🟧", "milestone": "✅", "fyi": "🟦"}[brief.category]
        lines = [
            _grounding_line(brief),
            f"{marker} {brief.program} ({brief.session_ref}) — {brief.category}; nothing to answer yet.",
            f"Stage: {brief.stage}",
        ]
        if brief.recommendation:
            lines.append(f"Recommendation: {brief.recommendation}")
    lines.append("— from the session, verbatim —")
    lines.extend(f"> {quote}" for quote in brief.source_quote)
    return "\n".join(lines)


def _age_label(opened_at: str, now: datetime) -> str:
    elapsed_seconds = max(0, int((now - _parse_at(opened_at)).total_seconds()))
    if elapsed_seconds < 3600:
        return f"open {elapsed_seconds // 60}m"
    if elapsed_seconds < 86400:
        return f"open {elapsed_seconds // 3600}h"
    return f"open {elapsed_seconds // 86400}d"


def render_group(briefs_or_pending: Sequence[OperatorBrief | ConversationThread], *, now: datetime | None = None) -> str:
    """Render several briefs as one numbered operator message."""
    current = datetime.now(UTC) if now is None else now
    sections: list[str] = []
    for index, item in enumerate(briefs_or_pending, start=1):
        if isinstance(item, ConversationThread):
            heading = f"[{index}] — {_age_label(item.latest_brief_at, current)}"
            brief = item.latest_brief
        else:
            heading = f"[{index}]"
            brief = item
        body = "\n".join(f"  {line}" if line else "" for line in render_brief(brief).splitlines())
        sections.append(f"{heading}\n{body}")
    return "\n\n".join(sections)


def read_entries(convlog_path: Path | None = None) -> list[ConversationEntry]:
    """Load valid log entries, logging and skipping malformed JSONL records."""
    path = default_convlog_path() if convlog_path is None else convlog_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries: list[ConversationEntry] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = ConversationEntry.model_validate_json(line)
            entry.payload = _validated_payload(entry)
        except ValidationError:
            logger.warning("convlog_malformed_line", path=str(path), line_number=line_number)
            continue
        entries.append(entry)
    return entries


def entries_for_thread(convlog_path: Path | None, thread_id: str) -> list[ConversationEntry]:
    """Return one thread's valid entries in persisted order."""
    return [entry for entry in read_entries(convlog_path) if entry.thread_id == thread_id]


def _next_seq(convlog_path: Path, thread_id: str) -> int:
    entries = entries_for_thread(convlog_path, thread_id)
    return max((entry.seq for entry in entries), default=0) + 1


def append_entry(
    convlog_path: Path,
    *,
    thread_id: str,
    kind: EntryKind,
    session_ref: str,
    payload: dict[str, Any],
    at: str | None = None,
) -> ConversationEntry:
    """Append one flushed JSONL entry, never rewriting an existing record."""
    entry = ConversationEntry(
        thread_id=thread_id,
        seq=_next_seq(convlog_path, thread_id),
        kind=kind,
        at=at or _utc_now(),
        session_ref=session_ref,
        payload=payload,
    )
    convlog_path.parent.mkdir(parents=True, exist_ok=True)
    line = entry.model_dump_json(by_alias=True) + "\n"
    with convlog_path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def _require_thread(convlog_path: Path, thread_id: str) -> list[ConversationEntry]:
    entries = entries_for_thread(convlog_path, thread_id)
    if not entries:
        raise ConversationNotFoundError(thread_id)
    return entries


def append_session_message(convlog_path: Path, *, thread_id: str, session_ref: str, text: str, source_ref: str) -> ConversationEntry:
    """Record one full, verbatim upstream session message."""
    if not text:
        raise ValueError("session message text must be non-empty")
    if not source_ref.strip():
        raise ValueError("source_ref must be non-empty")
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="session_msg",
        session_ref=session_ref,
        payload={"text": text, "source_ref": source_ref},
    )


def append_operator_brief(convlog_path: Path, *, thread_id: str, brief: OperatorBrief) -> ConversationEntry:
    """Record a validated brief and its deterministic rendering."""
    _require_thread(convlog_path, thread_id)
    rendered = render_brief(brief)
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="operator_brief",
        session_ref=brief.session_ref,
        payload={"brief": brief.model_dump(mode="json"), "rendered": rendered},
    )


def open_thread(convlog_path: Path, *, brief: OperatorBrief, raw_text: str) -> str:
    """Open a new thread by recording its raw message before its first brief."""
    thread_id = uuid.uuid4().hex[:12]
    append_session_message(convlog_path, thread_id=thread_id, session_ref=brief.session_ref, text=raw_text, source_ref=brief.source_ref)
    append_operator_brief(convlog_path, thread_id=thread_id, brief=brief)
    return thread_id


def append_ruling(convlog_path: Path, *, thread_id: str, text: str, via: Literal["chat", "in-pane", "slack"] = "chat") -> ConversationEntry:
    """Record one explicit operator ruling for an existing thread."""
    entries = _require_thread(convlog_path, thread_id)
    if not text:
        raise ValueError("operator ruling text must be non-empty")
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="operator_ruling",
        session_ref=entries[-1].session_ref,
        payload={"text": text, "via": via},
    )


def append_directive(convlog_path: Path, *, thread_id: str, text: str, order_id: str | None = None) -> ConversationEntry:
    """Record the exact directive sent down to the lane."""
    entries = _require_thread(convlog_path, thread_id)
    if not text:
        raise ValueError("lane directive text must be non-empty")
    return append_entry(
        convlog_path,
        thread_id=thread_id,
        kind="lane_directive",
        session_ref=entries[-1].session_ref,
        payload={"text": text, "order_id": order_id},
    )


def _thread_from_entries(thread_id: str, entries: list[ConversationEntry]) -> ConversationThread | None:
    brief_entries = [entry for entry in entries if entry.kind == "operator_brief"]
    if not brief_entries:
        return None
    latest = brief_entries[-1]
    try:
        brief = validate_brief(latest.payload["brief"])
    except (BriefValidationError, KeyError):
        logger.warning("convlog_invalid_brief_entry", thread_id=thread_id, seq=latest.seq)
        return None
    ruled_after = any(entry.kind == "operator_ruling" and entry.seq > latest.seq for entry in entries)
    return ConversationThread(
        thread_id=thread_id,
        session_ref=latest.session_ref,
        opened_at=entries[0].at,
        latest_brief=brief,
        latest_brief_at=latest.at,
        pending=brief.decision is not None and not ruled_after,
        entries=tuple(entries),
    )


def list_threads(convlog_path: Path | None = None, *, session_ref: str | None = None) -> list[ConversationThread]:
    """Derive current conversation-thread state in opening order."""
    by_thread: dict[str, list[ConversationEntry]] = {}
    for entry in read_entries(convlog_path):
        by_thread.setdefault(entry.thread_id, []).append(entry)
    threads = [thread for thread_id, entries in by_thread.items() if (thread := _thread_from_entries(thread_id, entries)) is not None]
    if session_ref is not None:
        threads = [thread for thread in threads if thread.session_ref == session_ref]
    return sorted(threads, key=lambda thread: (_parse_at(thread.opened_at), thread.thread_id))


def pending_threads(convlog_path: Path | None = None) -> list[ConversationThread]:
    """Return unresolved decision threads, oldest first; silence never retires one."""
    return [thread for thread in list_threads(convlog_path) if thread.pending]


def _read_json_argument(path: str) -> object:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_raw_argument(raw: str | None, raw_file: Path | None) -> str | None:
    if raw is not None:
        return raw
    if raw_file is not None:
        return raw_file.read_text(encoding="utf-8")
    return None


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``chitra-convo`` command interface."""
    parser = argparse.ArgumentParser(prog="chitra-convo", description="Validate, render, and log caller-composed operator briefs.")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_convlog_path(command: argparse.ArgumentParser) -> None:
        command.add_argument("--convlog-path", type=Path, default=default_convlog_path())

    brief_command = commands.add_parser("brief", help="Validate, render, and append an operator brief.")
    add_convlog_path(brief_command)
    brief_command.add_argument("--session-ref", required=True)
    brief_command.add_argument("--json", required=True)
    raw_group = brief_command.add_mutually_exclusive_group()
    raw_group.add_argument("--raw")
    raw_group.add_argument("--raw-file", type=Path)
    brief_command.add_argument("--thread")

    rule_command = commands.add_parser("rule", help="Append an explicit operator ruling to one or more threads.")
    add_convlog_path(rule_command)
    rule_command.add_argument("--thread", action="append", required=True)
    rule_command.add_argument("--text", required=True)
    rule_command.add_argument("--via", choices=("chat", "in-pane", "slack"), default="chat")

    directive_command = commands.add_parser("directive", help="Append a directive sent down to a lane.")
    add_convlog_path(directive_command)
    directive_command.add_argument("--thread", required=True)
    directive_command.add_argument("--text", required=True)
    directive_command.add_argument("--order-id")

    pending_command = commands.add_parser("pending", help="Render all unresolved decision briefs.")
    add_convlog_path(pending_command)

    show_command = commands.add_parser("show", help="Print one thread's JSONL entries.")
    add_convlog_path(show_command)
    show_command.add_argument("--thread", required=True)

    list_command = commands.add_parser("list", help="List derived conversation-thread states.")
    add_convlog_path(list_command)
    list_command.add_argument("--session-ref")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the conversation-log CLI and return a conventional exit status."""
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "brief":
            brief = validate_brief(_read_json_argument(args.json))
            if brief.session_ref != args.session_ref:
                raise ValueError("--session-ref must match the brief session_ref")
            raw_text = _read_raw_argument(args.raw, args.raw_file)
            if args.thread is None:
                if raw_text is None:
                    raise ValueError("--raw or --raw-file is required when opening a new thread")
                thread_id = open_thread(args.convlog_path, brief=brief, raw_text=raw_text)
            else:
                _require_thread(args.convlog_path, args.thread)
                if raw_text is not None:
                    append_session_message(
                        args.convlog_path, thread_id=args.thread, session_ref=brief.session_ref, text=raw_text, source_ref=brief.source_ref
                    )
                append_operator_brief(args.convlog_path, thread_id=args.thread, brief=brief)
                thread_id = args.thread
            print(render_brief(brief))
            print(f"thread={thread_id}", file=sys.stderr)
        elif args.command == "rule":
            for thread_id in args.thread:
                append_ruling(args.convlog_path, thread_id=thread_id, text=args.text, via=args.via)
        elif args.command == "directive":
            append_directive(args.convlog_path, thread_id=args.thread, text=args.text, order_id=args.order_id)
        elif args.command == "pending":
            threads = pending_threads(args.convlog_path)
            print(render_group(threads) if threads else "No pending decisions.")
        elif args.command == "show":
            for entry in _require_thread(args.convlog_path, args.thread):
                print(entry.model_dump_json(by_alias=True))
        else:
            for thread in list_threads(args.convlog_path, session_ref=args.session_ref):
                state = "pending" if thread.pending else "ruled"
                print(f"{thread.thread_id}\t{thread.session_ref}\t{thread.latest_brief.category}\t{state}\t{thread.opened_at}")
    except (BriefValidationError, ConversationNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-convo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
