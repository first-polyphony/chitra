"""Deterministic storage for monitor-owned per-lane goal state.

No LLM calls in this module's own code path — it only records the monitor's
stated goal, completion condition, and current state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from chitra.state_paths import state_dir

logger = structlog.get_logger(__name__)

GoalStatus = Literal["working", "held", "idle", "blocked", "done-pending-verification", "done-pending-close"]
GOAL_STATUSES: tuple[GoalStatus, ...] = (
    "working",
    "held",
    "idle",
    "blocked",
    "done-pending-verification",
    "done-pending-close",
)
SCHEMA = "chitra.goals.v1"


class GoalValidationError(ValueError):
    """Raised when a goal record is not valid monitor doctrine."""


class GoalNotFoundError(KeyError):
    """Raised when an operation requires a goal record that is absent."""


@dataclass(frozen=True, slots=True)
class GoalRecord:
    """The five canonical fields plus monitor-maintained tactical metadata."""

    session_ref: str
    goal: str
    done_when: str
    source: str
    status: GoalStatus
    now: str = ""
    last_verified: str = ""
    created_at: str = ""
    updated_at: str = ""
    open_asks: tuple[str, ...] = ()
    needs: str = ""
    hold_reason: str = ""
    resume_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "session_ref": self.session_ref,
            "goal": self.goal,
            "done_when": self.done_when,
            "source": self.source,
            "status": self.status,
            "now": self.now,
            "last_verified": self.last_verified,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "open_asks": list(self.open_asks),
            "needs": self.needs,
            "hold_reason": self.hold_reason,
            "resume_at": self.resume_at,
        }


def session_host(session_ref: str) -> str:
    """Return the host component, gracefully retaining a bare token."""
    return session_ref.split(":", 1)[0]


def session_name(session_ref: str) -> str:
    """Return the session component, or a bare token when no host is known."""
    parts = session_ref.split(":")
    return parts[1] if len(parts) >= 2 and parts[1] else parts[0]


def validate_goal(rec: GoalRecord) -> list[str]:
    """Return deterministic doctrine violations for a prospective goal record."""
    issues: list[str] = []
    if not rec.goal.strip():
        issues.append("goal must be non-empty")
    elif len(rec.goal.split()) < 6:
        issues.append("goal must contain at least six words")
    if not rec.done_when.strip():
        issues.append("done_when must be non-empty")
    if not rec.source.strip():
        issues.append("source must be non-empty")
    if rec.status not in GOAL_STATUSES:
        issues.append(f"status must be one of {', '.join(GOAL_STATUSES)}")
    return issues


def goals_path(root: Path | None = None) -> Path:
    """Return the persistent goals document path for ``root``."""
    return (state_dir() if root is None else root) / "goals.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _record_from_dict(payload: object) -> GoalRecord:
    if not isinstance(payload, dict):
        raise ValueError("goal record must be an object")
    fields = ("session_ref", "goal", "done_when", "source", "status", "now", "last_verified", "created_at", "updated_at")
    values: dict[str, str] = {}
    for field in fields:
        value = payload.get(field)
        if not isinstance(value, str):
            raise ValueError(f"goal record {field} must be a string")
        values[field] = value
    return GoalRecord(
        session_ref=values["session_ref"],
        goal=values["goal"],
        done_when=values["done_when"],
        source=values["source"],
        status=cast(GoalStatus, values["status"]),
        now=values["now"],
        last_verified=values["last_verified"],
        created_at=values["created_at"],
        updated_at=values["updated_at"],
        open_asks=_open_asks_from_payload(payload),
        needs=_needs_from_payload(payload),
        hold_reason=_hold_reason_from_payload(payload),
        resume_at=_resume_at_from_payload(payload),
    )


def _open_asks_from_payload(payload: dict[str, object]) -> tuple[str, ...]:
    """Read optional persisted asks, retaining compatibility with v1 records."""
    raw_open_asks = payload.get("open_asks", [])
    if not isinstance(raw_open_asks, list) or not all(isinstance(ask, str) for ask in raw_open_asks):
        raise ValueError("goal record open_asks must be a list of strings")
    return tuple(raw_open_asks)


def _needs_from_payload(payload: dict[str, object]) -> str:
    """Read the optional unblock text retained by older persisted records."""
    needs = payload.get("needs", "")
    if not isinstance(needs, str):
        raise ValueError("goal record needs must be a string")
    return needs


def _hold_reason_from_payload(payload: dict[str, object]) -> str:
    """Read optional hold provenance retained by older persisted records."""
    hold_reason = payload.get("hold_reason", "")
    if not isinstance(hold_reason, str):
        raise ValueError("goal record hold_reason must be a string")
    return hold_reason


def _resume_at_from_payload(payload: dict[str, object]) -> str:
    """Read optional timed-hold deadline retained by older persisted records."""
    resume_at = payload.get("resume_at", "")
    if not isinstance(resume_at, str):
        raise ValueError("goal record resume_at must be a string")
    return resume_at


def load_goals(root: Path | None = None) -> list[GoalRecord]:
    """Load stored records; a missing store has no recorded lanes."""
    path = goals_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("goals.json is not a chitra.goals.v1 document")
    raw_goals = payload.get("goals")
    if not isinstance(raw_goals, list):
        raise ValueError("goals.json goals must be a list")
    return [_record_from_dict(item) for item in raw_goals]


def _write_goals(root: Path | None, records: list[GoalRecord]) -> None:
    path = goals_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "updated_at": _utc_now(), "goals": [record.to_dict() for record in records]}
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def upsert_goal(root: Path | None, rec: GoalRecord, *, clear_open_asks: bool = False) -> GoalRecord:
    """Validate and atomically insert or update one record by ``session_ref``.

    An update with no incoming asks preserves any stored asks, so routine
    status or ``now`` revisions cannot silently retire an operator request.
    Pass ``clear_open_asks=True`` only for an explicit retirement path.
    """
    issues = validate_goal(rec)
    if issues:
        raise GoalValidationError("; ".join(issues))
    existing = get_goal(root, rec.session_ref)
    now = _utc_now()
    open_asks = rec.open_asks
    if existing is not None and not open_asks and existing.open_asks and not clear_open_asks:
        open_asks = existing.open_asks
    hold_reason = rec.hold_reason
    resume_at = rec.resume_at
    if existing is not None and rec.status == existing.status and not hold_reason and not resume_at:
        hold_reason = existing.hold_reason
        resume_at = existing.resume_at
    stored = GoalRecord(
        session_ref=rec.session_ref,
        goal=rec.goal,
        done_when=rec.done_when,
        source=rec.source,
        status=rec.status,
        now=rec.now,
        last_verified=rec.last_verified,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
        open_asks=open_asks,
        needs=rec.needs,
        hold_reason=hold_reason,
        resume_at=resume_at,
    )
    records = [record for record in load_goals(root) if record.session_ref != rec.session_ref]
    records.append(stored)
    _write_goals(root, records)
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


def _parse_iso8601(value: str) -> datetime:
    """Parse one timezone-aware ISO8601 datetime for timed hold metadata."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("resume_at must be an ISO8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("resume_at must be an ISO8601 datetime with timezone")
    return parsed


def hold_goal(root: Path | None, session_ref: str, *, reason: str, resume_at: str = "") -> GoalRecord:
    """Mark an existing lane held while retaining its re-arm payload."""
    if not reason.strip():
        raise ValueError("hold reason must be non-empty")
    if resume_at:
        _parse_iso8601(resume_at)
    existing = get_goal(root, session_ref)
    if existing is None:
        raise GoalNotFoundError(session_ref)
    held = upsert_goal(root, replace(existing, status="held", hold_reason=reason, resume_at=resume_at))
    logger.info("goal_mutated", session_ref=session_ref, action="hold")
    return held


def resume_goal(root: Path | None, session_ref: str) -> GoalRecord:
    """Return an explicitly held lane to working state and clear hold metadata."""
    existing = get_goal(root, session_ref)
    if existing is None:
        raise GoalNotFoundError(session_ref)
    if existing.status != "held":
        raise ValueError("goal is not held")
    resumed = upsert_goal(root, replace(existing, status="working", hold_reason="", resume_at=""))
    logger.info("goal_mutated", session_ref=session_ref, action="resume")
    return resumed


def due_goals(root: Path | None = None, *, now: datetime | None = None) -> list[GoalRecord]:
    """Return timed held lanes due at or before ``now`` in stable operator order."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    due: list[tuple[datetime, GoalRecord]] = []
    for record in load_goals(root):
        if record.status != "held" or not record.resume_at:
            continue
        resume_at = _parse_iso8601(record.resume_at)
        if resume_at <= current:
            due.append((resume_at, record))
    return [record for _, record in sorted(due, key=lambda item: (item[0], item[1].session_ref))]


def add_ask(root: Path | None, session_ref: str, ask: str) -> GoalRecord:
    """Persist one exact open operator ask for an existing lane, once only."""
    existing = get_goal(root, session_ref)
    if existing is None:
        raise GoalNotFoundError(session_ref)
    if ask in existing.open_asks:
        return existing
    return upsert_goal(root, replace(existing, open_asks=(*existing.open_asks, ask)))


def resolve_ask(
    root: Path | None,
    session_ref: str,
    *,
    ask: str | None = None,
    index: int | None = None,
    all: bool = False,
) -> GoalRecord:
    """Explicitly remove one matching ask, one indexed ask, or every ask."""
    selector_count = int(ask is not None) + int(index is not None) + int(all)
    if selector_count != 1:
        raise ValueError("select exactly one of ask, index, or all")
    existing = get_goal(root, session_ref)
    if existing is None:
        raise GoalNotFoundError(session_ref)
    if all:
        remaining: tuple[str, ...] = ()
    elif ask is not None:
        if ask not in existing.open_asks:
            raise ValueError("open ask was not found")
        remaining = tuple(item for item in existing.open_asks if item != ask)
    else:
        assert index is not None
        if index < 0 or index >= len(existing.open_asks):
            raise ValueError("open ask index is out of range")
        remaining = existing.open_asks[:index] + existing.open_asks[index + 1 :]
    return upsert_goal(root, replace(existing, open_asks=remaining), clear_open_asks=True)


def get_goal(root: Path | None, session_ref: str) -> GoalRecord | None:
    """Return the record for ``session_ref``, if the monitor has stored one."""
    return next((record for record in load_goals(root) if record.session_ref == session_ref), None)


def list_goals(root: Path | None = None) -> list[GoalRecord]:
    """Return all current goal records in their persisted order."""
    return load_goals(root)


def close_goal(root: Path | None, session_ref: str) -> GoalRecord:
    """Remove a closed record from the deliberately small current-state store."""
    records = load_goals(root)
    closed = next((record for record in records if record.session_ref == session_ref), None)
    if closed is None:
        raise GoalNotFoundError(session_ref)
    _write_goals(root, [record for record in records if record.session_ref != session_ref])
    logger.info("goal_mutated", session_ref=session_ref, action="close")
    return closed


def _print_record(record: GoalRecord) -> None:
    print(json.dumps(record.to_dict(), indent=2, sort_keys=True))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chitra-goals", description="Store deterministic monitor goal state and render its roster.")
    parser.add_argument("--root", type=Path, default=state_dir())
    commands = parser.add_subparsers(dest="command", required=True)

    def add_root(command: argparse.ArgumentParser) -> None:
        command.add_argument("--root", type=Path, default=argparse.SUPPRESS)

    set_command = commands.add_parser("set", help="Create or update a lane goal.")
    add_root(set_command)
    set_command.add_argument("--session-ref", required=True)
    set_command.add_argument("--goal", required=True)
    set_command.add_argument("--done-when", required=True)
    set_command.add_argument("--source", required=True)
    set_command.add_argument("--status", choices=GOAL_STATUSES, default="working")
    set_command.add_argument("--now", default="")
    set_command.add_argument("--last-verified", default="")
    set_command.add_argument("--needs", default=None, help="Specific human action required to unblock this lane.")
    asks_group = set_command.add_mutually_exclusive_group()
    asks_group.add_argument("--open-ask", action="append", default=[])
    asks_group.add_argument("--clear-asks", action="store_true")

    get_command = commands.add_parser("get", help="Print one lane goal as JSON.")
    add_root(get_command)
    get_command.add_argument("--session-ref", required=True)

    list_command = commands.add_parser("list", help="List current lane goals.")
    add_root(list_command)
    list_command.add_argument("--json", action="store_true")

    close_command = commands.add_parser("close", help="Remove a closed lane goal.")
    add_root(close_command)
    close_command.add_argument("--session-ref", required=True)

    hold_command = commands.add_parser("hold", help="Hold an existing lane without discarding its goal.")
    add_root(hold_command)
    hold_command.add_argument("--session-ref", required=True)
    hold_command.add_argument("--reason", required=True)
    hold_command.add_argument("--resume-at", default="")

    resume_command = commands.add_parser("resume", help="Return an explicitly held lane to working state.")
    add_root(resume_command)
    resume_command.add_argument("--session-ref", required=True)

    due_command = commands.add_parser("due", help="List timed holds that are due for operator review.")
    add_root(due_command)
    due_command.add_argument("--now", default="")

    add_ask_command = commands.add_parser("add-ask", help="Add one persistent open operator ask to a lane.")
    add_root(add_ask_command)
    add_ask_command.add_argument("--session-ref", required=True)
    add_ask_command.add_argument("--ask", required=True)

    resolve_ask_command = commands.add_parser("resolve-ask", help="Explicitly retire persisted open operator asks.")
    add_root(resolve_ask_command)
    resolve_ask_command.add_argument("--session-ref", required=True)
    selectors = resolve_ask_command.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--ask")
    selectors.add_argument("--index", type=int)
    selectors.add_argument("--all", action="store_true")

    scan_asks_command = commands.add_parser("scan-asks", help="Extract verbatim open asks from a lane transcript.")
    add_root(scan_asks_command)
    scan_asks_command.add_argument("--transcript", type=Path, required=True)
    scan_asks_command.add_argument("--session-ref")
    scan_asks_command.add_argument("--record", action="store_true")

    roster_command = commands.add_parser("roster", help="Render the operator roster table.")
    add_root(roster_command)
    roster_command.add_argument("--format", choices=("cards", "box", "markdown"), default="cards")
    roster_command.add_argument("--lint", action="store_true", help="Print optional board roster-lint advice to stderr.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "set":
            existing = get_goal(args.root, args.session_ref)
            requested_record = GoalRecord(
                session_ref=args.session_ref,
                goal=args.goal,
                done_when=args.done_when,
                source=args.source,
                status=args.status,
                now=args.now,
                last_verified=args.last_verified,
                open_asks=tuple(args.open_ask),
                needs=args.needs if args.needs is not None else (existing.needs if existing is not None else ""),
            )
            _print_record(upsert_goal(args.root, requested_record, clear_open_asks=args.clear_asks))
        elif args.command == "get":
            found_record = get_goal(args.root, args.session_ref)
            if found_record is None:
                raise GoalNotFoundError(args.session_ref)
            _print_record(found_record)
        elif args.command == "list":
            records = list_goals(args.root)
            if args.json:
                print(json.dumps([record.to_dict() for record in records], indent=2, sort_keys=True))
            else:
                for record in records:
                    print(f"{record.session_ref}\t{record.status}\t{record.goal}\t{json.dumps(list(record.open_asks))}")
        elif args.command == "close":
            _print_record(close_goal(args.root, args.session_ref))
        elif args.command == "hold":
            _print_record(hold_goal(args.root, args.session_ref, reason=args.reason, resume_at=args.resume_at))
        elif args.command == "resume":
            _print_record(resume_goal(args.root, args.session_ref))
        elif args.command == "due":
            due_now = _parse_iso8601(args.now) if args.now else None
            print(json.dumps([record.to_dict() for record in due_goals(args.root, now=due_now)], indent=2, sort_keys=True))
        elif args.command == "add-ask":
            _print_record(add_ask(args.root, args.session_ref, args.ask))
        elif args.command == "resolve-ask":
            _print_record(resolve_ask(args.root, args.session_ref, ask=args.ask, index=args.index, all=args.all))
        elif args.command == "scan-asks":
            if args.record and args.session_ref is None:
                raise ValueError("--record requires --session-ref")
            from chitra.lane_read import extract_open_asks, read_last_assistant_message

            asks = extract_open_asks(read_last_assistant_message(args.transcript))
            for ask in asks:
                print(ask)
                if args.record:
                    assert args.session_ref is not None
                    add_ask(args.root, args.session_ref, ask)
        else:
            from chitra import board
            from chitra.artifacts import list_unreviewed_artifacts

            records = list_goals(args.root)
            print(board.render_roster(records, fmt=args.format, artifacts=list_unreviewed_artifacts(args.root)))
            if args.lint:
                roster_lint = getattr(board, "roster_lint", None)
                if roster_lint is not None:
                    for issue in roster_lint(records):
                        print(issue, file=sys.stderr)
    except (GoalValidationError, GoalNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-goals: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
