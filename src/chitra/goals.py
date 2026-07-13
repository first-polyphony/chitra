"""Deterministic storage for monitor-owned per-lane goal state.

No LLM calls in this module's own code path — it only records the monitor's
stated goal, completion condition, and current state.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Iterator
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

# Shared hold_reason convention: a hold_reason starting with this prefix
# (e.g. "rate-limit:5h") marks a timed pause driven by provider usage
# thresholds (see chitra.rate_limit_guard), distinct from an operator- or
# throttle-initiated hold. goals.py itself stays decision-free -- this is
# just the string convention two callers (chitra.rate_limit_guard, which
# sets it, and chitra.dispatchd, which reads it to freeze a held session's
# queue) need to agree on.
RATE_LIMIT_HOLD_REASON_PREFIX = "rate-limit:"


class GoalValidationError(ValueError):
    """Raised when a goal record is not valid monitor doctrine."""


class GoalRedirectRequiredError(GoalValidationError):
    """Raised when a strategic goal revision must use the redirect path."""


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
    intent: str = ""
    scope: str = ""
    goal_version: int = 1
    goal_history: tuple[dict[str, str], ...] = ()
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
            "intent": self.intent,
            "scope": self.scope,
            "goal_version": self.goal_version,
            "goal_history": list(self.goal_history),
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


def check_specification(rec: GoalRecord) -> list[str]:
    """Return stricter deterministic interview-bypass criteria for a goal."""
    issues: list[str] = []
    if not rec.intent.strip() or len(rec.intent.split()) < 8:
        issues.append("intent must be non-empty and contain at least eight words")
    if len(rec.goal.split()) < 6:
        issues.append("goal must contain at least six words")
    if not rec.done_when.strip() or len(rec.done_when.split()) < 5:
        issues.append("done_when must be non-empty and contain at least five words")
    if not rec.scope.strip() or len(rec.scope.split()) < 4:
        issues.append("scope must be non-empty and contain at least four words")
    if not rec.source.startswith(("task-file", "branch", "transcript-first-msg")):
        issues.append("source must start with task-file, branch, or transcript-first-msg")
    return issues


def goals_path(root: Path | None = None) -> Path:
    """Return the persistent goals document path for ``root``."""
    return (state_dir() if root is None else root) / "goals.json"


@contextlib.contextmanager
def _goal_store_lock(root: Path | None) -> Iterator[None]:
    """Serialize one full read-modify-write transaction against the goals store.

    Concurrent writers (the CLI, a live monitor sweep, ``chitra.rate_limit_
    guard``'s sweep, etc.) can each read the same on-disk snapshot and then
    replace it with their own mutation, silently discarding whichever wrote
    last -- goal updates are atomic PER WRITE (``_write_goals``'s
    write-temp-then-``os.replace``), but not against a concurrent reader
    racing the same read-modify-write window. An exclusive ``flock`` on a
    sidecar lock file (never the document itself, so a lock holder's crash
    cannot corrupt or strand the document) forces every read-modify-write
    transaction in this module to run one at a time, closing that
    lost-update window. Blocking, not best-effort: callers wait for the
    lock rather than silently skipping serialization.
    """
    path = goals_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
        intent=_intent_from_payload(payload),
        scope=_scope_from_payload(payload),
        goal_version=_goal_version_from_payload(payload),
        goal_history=_goal_history_from_payload(payload),
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


def _intent_from_payload(payload: dict[str, object]) -> str:
    """Read optional original operator intent from compatible goal records."""
    intent = payload.get("intent", "")
    if not isinstance(intent, str):
        raise ValueError("goal record intent must be a string")
    return intent


def _scope_from_payload(payload: dict[str, object]) -> str:
    """Read optional strategic scope boundaries from compatible goal records."""
    scope = payload.get("scope", "")
    if not isinstance(scope, str):
        raise ValueError("goal record scope must be a string")
    return scope


def _goal_version_from_payload(payload: dict[str, object]) -> int:
    """Read the optional monotonic strategic-goal version."""
    goal_version = payload.get("goal_version", 1)
    if not isinstance(goal_version, int) or isinstance(goal_version, bool):
        raise ValueError("goal record goal_version must be an integer")
    return goal_version


def _goal_history_from_payload(payload: dict[str, object]) -> tuple[dict[str, str], ...]:
    """Read redirect history entries from compatible goal records strictly."""
    raw_history = payload.get("goal_history", [])
    if not isinstance(raw_history, list):
        raise ValueError("goal record goal_history must be a list of objects")
    fields = ("goal", "done_when", "intent", "scope", "revised_at", "reason")
    history: list[dict[str, str]] = []
    for entry in raw_history:
        if not isinstance(entry, dict) or set(entry) != set(fields):
            raise ValueError("goal record goal_history entries must contain strategic prior values")
        if not all(isinstance(entry[field], str) for field in fields):
            raise ValueError("goal record goal_history entries must contain strings")
        history.append({field: entry[field] for field in fields})
    return tuple(history)


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
    with _goal_store_lock(root):
        stored = _upsert_goal_locked(root, rec, clear_open_asks=clear_open_asks)
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


def _upsert_goal_locked(root: Path | None, rec: GoalRecord, *, clear_open_asks: bool = False) -> GoalRecord:
    """The body of ``upsert_goal``, assuming the caller already holds
    ``_goal_store_lock``. Callers that must read-then-modify an existing
    record (``add_ask``, ``resolve_ask``, ``hold_goal``, ``resume_goal``,
    ``update_now``) take the lock ONCE around their own read AND this write
    so the read cannot go stale before the write lands -- calling the public
    ``upsert_goal`` (which re-acquires the lock itself) from inside an
    already-locked section would either deadlock (a second ``flock`` from
    the same process on a fresh fd still blocks on the first) or, worse,
    leave a real window between the caller's own read and the write. See
    docs/SOL-ADVERSARIAL-REVIEW finding #9.
    """
    existing = get_goal(root, rec.session_ref)
    if existing is not None and not _strategic_fields_match(existing, rec):
        raise GoalRedirectRequiredError("strategic goal fields changed; use chitra-goals redirect --reason ...")
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
        intent=rec.intent,
        scope=rec.scope,
        goal_version=existing.goal_version if existing is not None else rec.goal_version,
        goal_history=existing.goal_history if existing is not None else rec.goal_history,
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
    return stored


def _strategic_fields_match(left: GoalRecord, right: GoalRecord) -> bool:
    """Compare strategic fields while treating whitespace-only revisions alike."""
    return all(
        getattr(left, field).strip() == getattr(right, field).strip()
        for field in ("goal", "done_when", "intent", "scope", "source")
    )


def redirect_goal(
    root: Path | None,
    session_ref: str,
    *,
    reason: str,
    goal: str | None = None,
    done_when: str | None = None,
    intent: str | None = None,
    scope: str | None = None,
    source: str | None = None,
) -> GoalRecord:
    """Replace strategic values after recording the prior operator direction."""
    if not reason.strip():
        raise ValueError("redirect reason must be non-empty")
    with _goal_store_lock(root):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        redirected = replace(
            existing,
            goal=existing.goal if goal is None else goal,
            done_when=existing.done_when if done_when is None else done_when,
            intent=existing.intent if intent is None else intent,
            scope=existing.scope if scope is None else scope,
            source=existing.source if source is None else source,
        )
        if _strategic_fields_match(existing, redirected):
            raise ValueError("redirect must change at least one strategic field")
        issues = validate_goal(redirected)
        if issues:
            raise GoalValidationError("; ".join(issues))
        revised_at = _utc_now()
        history_entry = {
            "goal": existing.goal,
            "done_when": existing.done_when,
            "intent": existing.intent,
            "scope": existing.scope,
            "revised_at": revised_at,
            "reason": reason,
        }
        stored = replace(
            redirected,
            goal_version=existing.goal_version + 1,
            goal_history=(*existing.goal_history, history_entry),
            created_at=existing.created_at,
            updated_at=revised_at,
        )
        records = [record for record in load_goals(root) if record.session_ref != session_ref]
        records.append(stored)
        _write_goals(root, records)
    logger.info("goal_mutated", session_ref=session_ref, action="redirect")
    return stored


def update_now(
    root: Path | None,
    session_ref: str,
    *,
    now: str | None = None,
    status: GoalStatus | None = None,
    last_verified: str | None = None,
) -> GoalRecord:
    """Update only the current tactical state of an existing goal record."""
    with _goal_store_lock(root):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        stored = _upsert_goal_locked(
            root,
            replace(
                existing,
                now=existing.now if now is None else now,
                status=existing.status if status is None else status,
                last_verified=existing.last_verified if last_verified is None else last_verified,
            ),
        )
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
    with _goal_store_lock(root):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        held = _upsert_goal_locked(root, replace(existing, status="held", hold_reason=reason, resume_at=resume_at))
    logger.info("goal_mutated", session_ref=session_ref, action="hold")
    return held


def resume_goal(root: Path | None, session_ref: str) -> GoalRecord:
    """Return an explicitly held lane to working state and clear hold metadata."""
    with _goal_store_lock(root):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        if existing.status != "held":
            raise ValueError("goal is not held")
        resumed = _upsert_goal_locked(root, replace(existing, status="working", hold_reason="", resume_at=""))
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
    with _goal_store_lock(root):
        existing = get_goal(root, session_ref)
        if existing is None:
            raise GoalNotFoundError(session_ref)
        if ask in existing.open_asks:
            return existing
        stored = _upsert_goal_locked(root, replace(existing, open_asks=(*existing.open_asks, ask)))
    logger.info("goal_mutated", session_ref=stored.session_ref, action="upsert")
    return stored


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
    with _goal_store_lock(root):
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
        stored = _upsert_goal_locked(root, replace(existing, open_asks=remaining), clear_open_asks=True)
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
    with _goal_store_lock(root):
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
    set_command.add_argument("--intent", default=None)
    set_command.add_argument("--scope", default=None)
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

    redirect_command = commands.add_parser("redirect", help="Record a reasoned revision to a lane's strategic goal.")
    add_root(redirect_command)
    redirect_command.add_argument("--session-ref", required=True)
    redirect_command.add_argument("--reason", required=True)
    redirect_command.add_argument("--goal")
    redirect_command.add_argument("--done-when")
    redirect_command.add_argument("--intent")
    redirect_command.add_argument("--scope")
    redirect_command.add_argument("--source")

    now_command = commands.add_parser("now", help="Update only a lane's tactical current state.")
    add_root(now_command)
    now_command.add_argument("--session-ref", required=True)
    now_command.add_argument("--now")
    now_command.add_argument("--status", choices=GOAL_STATUSES)
    now_command.add_argument("--last-verified")

    check_command = commands.add_parser("check", help="Check whether a lane meets the specification threshold.")
    add_root(check_command)
    check_command.add_argument("--session-ref", required=True)

    guidance_command = commands.add_parser("guidance", help="Locate canonical operator guidance for a working directory.")
    guidance_command.add_argument("--cwd", type=Path, required=True)
    guidance_command.add_argument("--show", action="store_true")

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

    from chitra.board import ROSTER_DEFAULT_FORMAT  # deferred: board imports goals at module top

    roster_command = commands.add_parser("roster", help="Render the operator roster.")
    add_root(roster_command)
    roster_command.add_argument("--format", choices=("cards", "box", "markdown"), default=ROSTER_DEFAULT_FORMAT)
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
                intent=args.intent if args.intent is not None else (existing.intent if existing is not None else ""),
                scope=args.scope if args.scope is not None else (existing.scope if existing is not None else ""),
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
        elif args.command == "redirect":
            _print_record(
                redirect_goal(
                    args.root,
                    args.session_ref,
                    reason=args.reason,
                    goal=args.goal,
                    done_when=args.done_when,
                    intent=args.intent,
                    scope=args.scope,
                    source=args.source,
                )
            )
        elif args.command == "now":
            _print_record(
                update_now(
                    args.root,
                    args.session_ref,
                    now=args.now,
                    status=args.status,
                    last_verified=args.last_verified,
                )
            )
        elif args.command == "check":
            found_record = get_goal(args.root, args.session_ref)
            if found_record is None:
                raise GoalNotFoundError(args.session_ref)
            specification_issues = check_specification(found_record)
            if specification_issues:
                print("\n".join(specification_issues))
                return 1
            print("well-specified")
        elif args.command == "guidance":
            from chitra.policy_config import load_policy_config, resolve_guidance

            guidance_path = resolve_guidance(load_policy_config(), args.cwd)
            if guidance_path is None:
                raise ValueError(f"no guidance is configured for {args.cwd}")
            if not guidance_path.is_file():
                raise ValueError(f"configured guidance file is missing: {guidance_path}")
            if args.show:
                print(guidance_path.read_text(encoding="utf-8"), end="")
            else:
                print(guidance_path)
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
    except GoalRedirectRequiredError as exc:
        print(f"chitra-goals: {exc}; use chitra-goals redirect --reason ...", file=sys.stderr)
        return 1
    except (GoalValidationError, GoalNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-goals: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
