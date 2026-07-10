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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from chitra.state_paths import state_dir

logger = structlog.get_logger(__name__)

GoalStatus = Literal["working", "held", "blocked", "done-pending-verification", "done-pending-close"]
GOAL_STATUSES: tuple[GoalStatus, ...] = (
    "working",
    "held",
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

    def to_dict(self) -> dict[str, str]:
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
    )


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


def upsert_goal(root: Path | None, rec: GoalRecord) -> GoalRecord:
    """Validate and atomically insert or update one record by ``session_ref``."""
    issues = validate_goal(rec)
    if issues:
        raise GoalValidationError("; ".join(issues))
    existing = get_goal(root, rec.session_ref)
    now = _utc_now()
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
    )
    records = [record for record in load_goals(root) if record.session_ref != rec.session_ref]
    records.append(stored)
    _write_goals(root, records)
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


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

    get_command = commands.add_parser("get", help="Print one lane goal as JSON.")
    add_root(get_command)
    get_command.add_argument("--session-ref", required=True)

    list_command = commands.add_parser("list", help="List current lane goals.")
    add_root(list_command)
    list_command.add_argument("--json", action="store_true")

    close_command = commands.add_parser("close", help="Remove a closed lane goal.")
    add_root(close_command)
    close_command.add_argument("--session-ref", required=True)

    roster_command = commands.add_parser("roster", help="Render the operator roster table.")
    add_root(roster_command)
    roster_command.add_argument("--format", choices=("box", "markdown"), default="box")
    roster_command.add_argument("--lint", action="store_true", help="Print optional board roster-lint advice to stderr.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "set":
            requested_record = GoalRecord(
                session_ref=args.session_ref,
                goal=args.goal,
                done_when=args.done_when,
                source=args.source,
                status=args.status,
                now=args.now,
                last_verified=args.last_verified,
            )
            _print_record(upsert_goal(args.root, requested_record))
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
                    print(f"{record.session_ref}\t{record.status}\t{record.goal}")
        elif args.command == "close":
            _print_record(close_goal(args.root, args.session_ref))
        else:
            from chitra import board

            records = list_goals(args.root)
            print(board.render_roster(records, fmt=args.format))
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
