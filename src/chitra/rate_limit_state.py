"""rate_limit_state — the durable outbox/transaction store behind
``chitra.rate_limit_guard``'s pause/resume state machine.

Each tracked session has at most one in-flight ``Transaction`` at a time,
walking through this exact phase sequence (see
docs/SOL-ADVERSARIAL-REVIEW finding #2):

    pause_requested -> checkpoint_sent -> stop_sent -> awaiting_quiescence
        -> held -> resume_requested -> resume_sent -> (removed = working)

Every phase transition is driven by consuming a real ``chitra.dispatchd``
result (never assumed), and every waiting phase is bounded by a deadline:
past the deadline, the sweep retries a bounded number of times, then
escalates for operator visibility -- it never strands a transaction forever
and never silently drops the freeze just because progress stalled. The
freeze itself lives in ``chitra.goals`` (``hold_goal``/``resume_goal``) and
is applied/cleared at specific, documented points in the sequence; this
module only tracks the mechanics of getting there.

No LLM calls anywhere in this module; a pure persisted fact table, using
the same atomic-write-then-``os.replace`` and exclusive-``flock`` pattern as
``chitra.goals``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from chitra.state_paths import state_dir

logger = structlog.get_logger(__name__)

SCHEMA = "chitra.rate_limit_state.v1"

TransactionPhase = Literal[
    "pause_requested",
    "checkpoint_sent",
    "stop_sent",
    "awaiting_quiescence",
    "held",
    "resume_requested",
    "resume_sent",
]
TRANSACTION_PHASES: tuple[TransactionPhase, ...] = (
    "pause_requested",
    "checkpoint_sent",
    "stop_sent",
    "awaiting_quiescence",
    "held",
    "resume_requested",
    "resume_sent",
)


@dataclass(frozen=True, slots=True)
class Transaction:
    """One session's in-flight pause/resume transaction record."""

    session_ref: str
    phase: TransactionPhase
    hold_reason: str = ""
    resume_at: str = ""
    checkpoint_order_id: str = ""
    stop_order_id: str = ""
    resume_order_id: str = ""
    transcript_path: str = ""
    last_transcript_mtime: float | None = None
    quiescent_since: str = ""
    attempts: int = 0
    escalated: bool = False
    deadline_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "session_ref": self.session_ref,
            "phase": self.phase,
            "hold_reason": self.hold_reason,
            "resume_at": self.resume_at,
            "checkpoint_order_id": self.checkpoint_order_id,
            "stop_order_id": self.stop_order_id,
            "resume_order_id": self.resume_order_id,
            "transcript_path": self.transcript_path,
            "last_transcript_mtime": self.last_transcript_mtime,
            "quiescent_since": self.quiescent_since,
            "attempts": self.attempts,
            "escalated": self.escalated,
            "deadline_at": self.deadline_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Transaction:
        if not isinstance(payload, dict):
            raise ValueError("rate-limit transaction record must be an object")
        phase = payload.get("phase")
        if phase not in TRANSACTION_PHASES:
            raise ValueError(f"rate-limit transaction phase must be one of {TRANSACTION_PHASES}")
        str_fields = (
            "session_ref",
            "hold_reason",
            "resume_at",
            "checkpoint_order_id",
            "stop_order_id",
            "resume_order_id",
            "transcript_path",
            "quiescent_since",
            "deadline_at",
            "created_at",
            "updated_at",
        )
        values: dict[str, str] = {}
        for name in str_fields:
            value = payload.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"rate-limit transaction {name} must be a string")
            values[name] = value
        attempts = payload.get("attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise ValueError("rate-limit transaction attempts must be an integer")
        escalated = payload.get("escalated", False)
        if not isinstance(escalated, bool):
            raise ValueError("rate-limit transaction escalated must be a boolean")
        raw_mtime = payload.get("last_transcript_mtime")
        if raw_mtime is not None and not isinstance(raw_mtime, (int, float)):
            raise ValueError("rate-limit transaction last_transcript_mtime must be a number or null")
        return cls(
            session_ref=values["session_ref"],
            phase=cast(TransactionPhase, phase),
            hold_reason=values["hold_reason"],
            resume_at=values["resume_at"],
            checkpoint_order_id=values["checkpoint_order_id"],
            stop_order_id=values["stop_order_id"],
            resume_order_id=values["resume_order_id"],
            transcript_path=values["transcript_path"],
            last_transcript_mtime=float(raw_mtime) if raw_mtime is not None else None,
            quiescent_since=values["quiescent_since"],
            attempts=attempts,
            escalated=escalated,
            deadline_at=values["deadline_at"],
            created_at=values["created_at"],
            updated_at=values["updated_at"],
        )


def transactions_path(root: Path | None = None) -> Path:
    """Return the persistent transaction-store document path for ``root``."""
    return (state_dir() if root is None else root) / "rate_limit_state.json"


@contextlib.contextmanager
def _transaction_lock(root: Path | None) -> Iterator[None]:
    """Serialize one full read-modify-write transaction, mirroring
    ``chitra.goals._goal_store_lock`` (see that function's docstring)."""
    path = transactions_path(root)
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


def load_transactions(root: Path | None = None) -> list[Transaction]:
    """Load stored transactions; a missing store has none in flight."""
    path = transactions_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("rate_limit_state.json is not a chitra.rate_limit_state.v1 document")
    raw = payload.get("transactions")
    if not isinstance(raw, list):
        raise ValueError("rate_limit_state.json transactions must be a list")
    return [Transaction.from_dict(item) for item in raw]


def _write_transactions(root: Path | None, transactions: list[Transaction]) -> None:
    path = transactions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "transactions": [txn.to_dict() for txn in transactions]}
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.replace(tmp.name, path)
            tmp_name = None
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def get_transaction(root: Path | None, session_ref: str) -> Transaction | None:
    """Return the in-flight transaction for ``session_ref``, if any."""
    return next((txn for txn in load_transactions(root) if txn.session_ref == session_ref), None)


def upsert_transaction(root: Path | None, txn: Transaction) -> Transaction:
    """Atomically insert or replace one transaction by ``session_ref``."""
    with _transaction_lock(root):
        records = [t for t in load_transactions(root) if t.session_ref != txn.session_ref]
        records.append(txn)
        _write_transactions(root, records)
    logger.info(
        "rate_limit_transaction_upserted", session_ref=txn.session_ref, phase=txn.phase, attempts=txn.attempts, escalated=txn.escalated
    )
    return txn


def remove_transaction(root: Path | None, session_ref: str) -> None:
    """Remove a completed (or abandoned) transaction. A no-op if absent."""
    with _transaction_lock(root):
        records = [t for t in load_transactions(root) if t.session_ref != session_ref]
        _write_transactions(root, records)
    logger.info("rate_limit_transaction_removed", session_ref=session_ref)
