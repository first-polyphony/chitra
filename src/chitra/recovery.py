"""Durable per-pause recovery records for held Chitra sessions."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from ._fsio import write_json_atomic
from .goals import LOAD_SHED_HOLD_REASON_PREFIX, GoalRecord, done_when_with_delta, get_goal
from .rate_limit_state import Transaction
from .state_paths import state_dir

logger = structlog.get_logger(__name__)

SCHEMA = "chitra.pause_recovery.v1"


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """Everything an operator needs to inspect and resume one verified pause."""

    pause_id: str
    session_ref: str
    hold_reason: str
    transcript_path: str
    resume_note: str
    resume_at: str
    paused_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "pause_id": self.pause_id,
            "session_ref": self.session_ref,
            "hold_reason": self.hold_reason,
            "transcript_path": self.transcript_path,
            "resume_note": self.resume_note,
            "resume_at": self.resume_at,
            "paused_at": self.paused_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RecoveryRecord:
        if not isinstance(payload, dict):
            raise ValueError("pause recovery record must be an object")
        fields = ("pause_id", "session_ref", "hold_reason", "transcript_path", "resume_note", "resume_at", "paused_at")
        # ``resume_at`` is a wall-clock resume time only rate-limit holds carry;
        # load-shed holds resume when host pressure clears and persist it empty by
        # design, so it must be allowed empty while every other field stays required.
        optional_empty = {"resume_at"}
        values: dict[str, str] = {}
        for field in fields:
            value = payload.get(field)
            if not isinstance(value, str) or (not value.strip() and field not in optional_empty):
                raise ValueError(f"pause recovery record {field} must be a non-empty string")
            values[field] = value
        return cls(**values)


def recovery_records_path(root: Path | None = None) -> Path:
    """Return the consolidated pause-recovery document path for ``root``."""
    return (state_dir() if root is None else root) / "pause_recovery.json"


@contextlib.contextmanager
def _recovery_lock(root: Path | None) -> Iterator[None]:
    path = recovery_records_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.parent / f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_recovery_records(root: Path | None = None) -> list[RecoveryRecord]:
    """Load every recorded pause in insertion order."""
    path = recovery_records_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("pause_recovery.json is not a chitra.pause_recovery.v1 document")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("pause_recovery.json records must be a list")
    return [RecoveryRecord.from_dict(item) for item in raw_records]


def _write_recovery_records(root: Path | None, records: list[RecoveryRecord]) -> None:
    path = recovery_records_path(root)
    payload = {"schema": SCHEMA, "records": [record.to_dict() for record in records]}
    write_json_atomic(path, payload, fsync=True)


def _resume_note(goal: GoalRecord) -> str:
    current_work = goal.now.strip() or goal.intent.strip() or goal.goal.strip()
    return f"Goal at pause: {goal.goal.strip()} Current work: {current_work} Done when: {done_when_with_delta(goal).strip()}"


def record_pause_recovery(root: Path | None, txn: Transaction, *, paused_at: str) -> RecoveryRecord:
    """Persist one idempotent recovery record as a transaction reaches ``held``."""
    if txn.phase != "held":
        raise ValueError("pause recovery can only be recorded for a held transaction")
    goal = get_goal(root, txn.session_ref)
    if goal is None:
        raise ValueError(f"cannot record pause recovery without a goal for {txn.session_ref}")
    # ``resume_at`` is a wall-clock resume time that only rate-limit holds carry;
    # load-shed holds resume when host pressure clears (load-driven, not timed) and
    # are created with an empty ``resume_at`` by design, so requiring it here would
    # crash the guard sweep every time a load-shed lane reaches ``held``.
    required = [txn.session_ref, txn.hold_reason, txn.transcript_path, txn.created_at, paused_at]
    if not txn.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
        required.append(txn.resume_at)
    if not all(value.strip() for value in required):
        raise ValueError("held transaction is missing required pause recovery data")
    pause_key = "\0".join((txn.session_ref, txn.hold_reason, txn.resume_at, txn.created_at))
    record = RecoveryRecord(
        pause_id=hashlib.sha256(pause_key.encode("utf-8")).hexdigest(),
        session_ref=txn.session_ref,
        hold_reason=txn.hold_reason,
        transcript_path=txn.transcript_path,
        resume_note=_resume_note(goal),
        resume_at=txn.resume_at,
        paused_at=paused_at,
    )
    with _recovery_lock(root):
        records = load_recovery_records(root)
        existing = next((item for item in records if item.pause_id == record.pause_id), None)
        if existing is not None:
            return existing
        records.append(record)
        _write_recovery_records(root, records)
    logger.info("pause_recovery_recorded", session_ref=record.session_ref, pause_id=record.pause_id)
    return record
