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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pydantic import ConfigDict, TypeAdapter, ValidationInfo, model_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from chitra._fsio import locked_json_store, write_json_atomic
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
PauseBackend = Literal["claude", "codex"]
TRANSACTION_PHASES: tuple[TransactionPhase, ...] = (
    "pause_requested",
    "checkpoint_sent",
    "stop_sent",
    "awaiting_quiescence",
    "held",
    "resume_requested",
    "resume_sent",
)


@pydantic_dataclass(frozen=True, slots=True, config=ConfigDict(strict=True))
class Transaction:
    """One session's in-flight pause/resume transaction record."""

    session_ref: str
    phase: TransactionPhase
    backend: PauseBackend = "claude"
    hold_reason: str = ""
    resume_at: str = ""
    checkpoint_order_id: str = ""
    stop_order_id: str = ""
    resume_order_id: str = ""
    transcript_path: str = ""
    last_transcript_mtime: float | None = None
    last_activity_token: str = ""
    quiescent_since: str = ""
    attempts: int = 0
    escalated: bool = False
    deadline_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return cast(dict[str, object], _TRANSACTION_ADAPTER.dump_python(self, mode="json"))

    @classmethod
    def from_dict(cls, payload: object) -> Transaction:
        return _TRANSACTION_ADAPTER.validate_python(payload, strict=False, context={"persisted": True})

    @model_validator(mode="before")
    @classmethod
    def validate_persisted(cls, payload: object, info: ValidationInfo) -> object:
        """Validate and default the exact legacy transaction-store shape."""
        if not info.context or not info.context.get("persisted"):
            return payload
        if not isinstance(payload, dict):
            raise ValueError("rate-limit transaction record must be an object")
        phase = payload.get("phase")
        if phase not in TRANSACTION_PHASES:
            raise ValueError(f"rate-limit transaction phase must be one of {TRANSACTION_PHASES}")
        backend = payload.get("backend", "claude")
        if backend not in ("claude", "codex"):
            raise ValueError("rate-limit transaction backend must be claude or codex")
        normalized = dict(payload)
        normalized["backend"] = backend
        str_fields = (
            "session_ref",
            "hold_reason",
            "resume_at",
            "checkpoint_order_id",
            "stop_order_id",
            "resume_order_id",
            "transcript_path",
            "last_activity_token",
            "quiescent_since",
            "deadline_at",
            "created_at",
            "updated_at",
        )
        for name in str_fields:
            value = payload.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"rate-limit transaction {name} must be a string")
            normalized[name] = value
        attempts = payload.get("attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise ValueError("rate-limit transaction attempts must be an integer")
        normalized["attempts"] = attempts
        escalated = payload.get("escalated", False)
        if not isinstance(escalated, bool):
            raise ValueError("rate-limit transaction escalated must be a boolean")
        normalized["escalated"] = escalated
        raw_mtime = payload.get("last_transcript_mtime")
        if raw_mtime is not None and not isinstance(raw_mtime, (int, float)):
            raise ValueError("rate-limit transaction last_transcript_mtime must be a number or null")
        normalized["last_transcript_mtime"] = float(raw_mtime) if raw_mtime is not None else None
        return normalized


_TRANSACTION_ADAPTER = TypeAdapter(Transaction)


@dataclass(frozen=True, slots=True)
class LoadHostState:
    """Durable anti-flap and shed-stack state for one sampled host."""

    host: str
    observed_level: int = 0
    breach_sweeps: int = 0
    clear_sweeps: int = 0
    load_level: int = 0
    mem_available_pct: float = 100.0
    memory_some_avg60: float = 0.0
    memory_full_avg60: float = 0.0
    cpu_some_avg60: float = 0.0
    shed_lanes: tuple[str, ...] = ()
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "observed_level": self.observed_level,
            "breach_sweeps": self.breach_sweeps,
            "clear_sweeps": self.clear_sweeps,
            "load_level": self.load_level,
            "mem_available_pct": self.mem_available_pct,
            "memory_some_avg60": self.memory_some_avg60,
            "memory_full_avg60": self.memory_full_avg60,
            "cpu_some_avg60": self.cpu_some_avg60,
            "shed_lanes": list(self.shed_lanes),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> LoadHostState:
        if not isinstance(payload, dict):
            raise ValueError("load host state must be an object")
        host = payload.get("host")
        updated_at = payload.get("updated_at", "")
        if not isinstance(host, str) or not host:
            raise ValueError("load host state host must be a non-empty string")
        if not isinstance(updated_at, str):
            raise ValueError("load host state updated_at must be a string")
        integers: dict[str, int] = {}
        for name in ("observed_level", "breach_sweeps", "clear_sweeps", "load_level"):
            value = payload.get(name, 0)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"load host state {name} must be an integer")
            integers[name] = value
        if integers["observed_level"] not in (0, 1, 2, 3) or integers["load_level"] not in (0, 1, 2, 3):
            raise ValueError("load host state levels must be from 0 through 3")
        if integers["breach_sweeps"] < 0 or integers["clear_sweeps"] < 0:
            raise ValueError("load host state counters must not be negative")
        floats: dict[str, float] = {}
        for name, default in (
            ("mem_available_pct", 100.0),
            ("memory_some_avg60", 0.0),
            ("memory_full_avg60", 0.0),
            ("cpu_some_avg60", 0.0),
        ):
            value = payload.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"load host state {name} must be a number")
            floats[name] = float(value)
        raw_shed_lanes = payload.get("shed_lanes", [])
        if not isinstance(raw_shed_lanes, list) or not all(isinstance(item, str) for item in raw_shed_lanes):
            raise ValueError("load host state shed_lanes must be a list of strings")
        return cls(
            host=host,
            observed_level=integers["observed_level"],
            breach_sweeps=integers["breach_sweeps"],
            clear_sweeps=integers["clear_sweeps"],
            load_level=integers["load_level"],
            mem_available_pct=floats["mem_available_pct"],
            memory_some_avg60=floats["memory_some_avg60"],
            memory_full_avg60=floats["memory_full_avg60"],
            cpu_some_avg60=floats["cpu_some_avg60"],
            shed_lanes=tuple(raw_shed_lanes),
            updated_at=updated_at,
        )


def transactions_path(root: Path | None = None) -> Path:
    """Return the persistent transaction-store document path for ``root``."""
    return (state_dir() if root is None else root) / "rate_limit_state.json"


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


def load_load_states(root: Path | None = None) -> list[LoadHostState]:
    """Load durable per-host pressure state from the shared guard document."""
    path = transactions_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("rate_limit_state.json is not a chitra.rate_limit_state.v1 document")
    raw = payload.get("load_hosts", [])
    if not isinstance(raw, list):
        raise ValueError("rate_limit_state.json load_hosts must be a list")
    return [LoadHostState.from_dict(item) for item in raw]


def _write_state(root: Path | None, transactions: list[Transaction], load_states: list[LoadHostState]) -> None:
    path = transactions_path(root)
    payload = {
        "schema": SCHEMA,
        "transactions": [txn.to_dict() for txn in transactions],
        "load_hosts": [state.to_dict() for state in load_states],
    }
    write_json_atomic(path, payload)


def get_transaction(root: Path | None, session_ref: str) -> Transaction | None:
    """Return the in-flight transaction for ``session_ref``, if any."""
    return next((txn for txn in load_transactions(root) if txn.session_ref == session_ref), None)


def upsert_transaction(root: Path | None, txn: Transaction) -> Transaction:
    """Atomically insert or replace one transaction by ``session_ref``."""
    with locked_json_store(transactions_path(root)):
        records = [t for t in load_transactions(root) if t.session_ref != txn.session_ref]
        records.append(txn)
        _write_state(root, records, load_load_states(root))
    logger.info(
        "rate_limit_transaction_upserted", session_ref=txn.session_ref, phase=txn.phase, attempts=txn.attempts, escalated=txn.escalated
    )
    return txn


def remove_transaction(root: Path | None, session_ref: str) -> None:
    """Remove a completed (or abandoned) transaction. A no-op if absent."""
    with locked_json_store(transactions_path(root)):
        records = [t for t in load_transactions(root) if t.session_ref != session_ref]
        _write_state(root, records, load_load_states(root))
    logger.info("rate_limit_transaction_removed", session_ref=session_ref)


def get_load_state(root: Path | None, host: str) -> LoadHostState | None:
    """Return the persisted pressure state for ``host``, if sampled before."""
    return next((state for state in load_load_states(root) if state.host == host), None)


def upsert_load_state(root: Path | None, state: LoadHostState) -> LoadHostState:
    """Atomically insert or replace one host's load state without losing transactions."""
    with locked_json_store(transactions_path(root)):
        states = [item for item in load_load_states(root) if item.host != state.host]
        states.append(state)
        _write_state(root, load_transactions(root), sorted(states, key=lambda item: item.host))
    logger.info(
        "load_host_state_upserted",
        host=state.host,
        load_level=state.load_level,
        breach_sweeps=state.breach_sweeps,
        clear_sweeps=state.clear_sweeps,
    )
    return state
