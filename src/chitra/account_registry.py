"""account_registry — freshness-bounded lane -> account identity tracking.

Deterministic bookkeeping only: records which account a tracked
``tmux_session`` was last observed under, and when. Used by
``chitra.rate_limit_guard`` to detect two situations a single sweep's
usage-snapshot batch cannot see on its own (see
docs/SOL-ADVERSARIAL-REVIEW finding #6):

- **A previously-tracked lane's snapshot goes missing.** Its sidecar
  stopped writing, or is between writes, so this sweep's ``usage_dir`` has
  no file for it. The lane is not silently forgotten: its last-known
  account is retained for a bounded freshness window, and its absence is
  surfaced as an escalation for operator visibility -- never auto-acted on,
  since there is no live ``session_ref`` in this sweep to pause or resume.
- **A lane's account identity changes between sweeps** (a mid-session
  account swap, e.g. a re-auth to a different subscription). The registry
  detects the change and surfaces it rather than silently carrying the old
  identity's hold/pause state forward under the new one.

No LLM calls anywhere in this module; a pure persisted fact table, using the
same atomic-write-then-``os.replace`` and exclusive-``flock`` pattern as
``chitra.goals``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from chitra.state_paths import state_dir
from chitra.usage import AccountedVerdict

logger = structlog.get_logger(__name__)

SCHEMA = "chitra.account_registry.v1"
DEFAULT_FRESHNESS_SECONDS = 3600  # 1 hour: long enough to survive one missed sweep, short enough to not act on ancient data


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """The last-observed account identity for one tracked lane."""

    tmux_session: str
    session_id: str
    kind: str
    account: str
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "tmux_session": self.tmux_session,
            "session_id": self.session_id,
            "kind": self.kind,
            "account": self.account,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RegistryEntry:
        if not isinstance(payload, dict):
            raise ValueError("account registry entry must be an object")
        fields = ("tmux_session", "session_id", "kind", "account", "updated_at")
        values: dict[str, str] = {}
        for name in fields:
            value = payload.get(name)
            if not isinstance(value, str):
                raise ValueError(f"account registry entry {name} must be a string")
            values[name] = value
        return cls(**values)


@dataclass(slots=True)
class RegistryUpdate:
    """What changed when a sweep's verdicts were folded into the registry."""

    account_changed: list[tuple[str, str, str]] = field(default_factory=list)  # (tmux_session, old_account, new_account)
    disappeared: list[RegistryEntry] = field(default_factory=list)  # previously-fresh lanes absent from this sweep


def registry_path(root: Path | None = None) -> Path:
    """Return the persistent account-registry document path for ``root``."""
    return (state_dir() if root is None else root) / "account_registry.json"


@contextlib.contextmanager
def _registry_lock(root: Path | None) -> Iterator[None]:
    """Serialize one full read-modify-write transaction, mirroring
    ``chitra.goals._goal_store_lock`` (see that function's docstring for the
    lost-update rationale)."""
    path = registry_path(root)
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


def load_registry(root: Path | None = None) -> list[RegistryEntry]:
    """Load stored entries; a missing store has no recorded lanes."""
    path = registry_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("account_registry.json is not a chitra.account_registry.v1 document")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("account_registry.json entries must be a list")
    return [RegistryEntry.from_dict(item) for item in raw_entries]


def _write_registry(root: Path | None, entries: list[RegistryEntry]) -> None:
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "entries": [entry.to_dict() for entry in entries]}
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


def get_entry(root: Path | None, tmux_session: str) -> RegistryEntry | None:
    """Return the last-observed entry for ``tmux_session``, if any."""
    return next((entry for entry in load_registry(root) if entry.tmux_session == tmux_session), None)


def update_registry(
    root: Path | None,
    verdicts: list[AccountedVerdict],
    *,
    now: datetime,
    freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS,
) -> RegistryUpdate:
    """Fold one sweep's verdicts into the registry; return what changed.

    Only verdicts with a non-empty ``tmux_session`` are tracked (the
    synthetic Codex account-wide probe has none and is intentionally
    excluded -- see ``chitra.rate_limit_guard``'s module docstring). Entries
    older than ``freshness_seconds`` with no corresponding verdict this
    sweep are pruned silently (too stale to act on); entries still within
    the freshness window but missing this sweep are reported as
    ``disappeared`` instead of being dropped, so the caller can escalate
    rather than silently doing nothing.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    update = RegistryUpdate()
    with _registry_lock(root):
        existing_by_session = {entry.tmux_session: entry for entry in load_registry(root)}
        seen_this_sweep: set[str] = set()
        merged: dict[str, RegistryEntry] = {}
        for verdict in verdicts:
            if not verdict.tmux_session:
                continue  # Codex's synthetic account-wide probe: no per-lane identity to track
            seen_this_sweep.add(verdict.tmux_session)
            prior = existing_by_session.get(verdict.tmux_session)
            if prior is not None and prior.account and verdict.account and prior.account != verdict.account:
                update.account_changed.append((verdict.tmux_session, prior.account, verdict.account))
            merged[verdict.tmux_session] = RegistryEntry(
                tmux_session=verdict.tmux_session,
                session_id=verdict.session_id,
                kind=verdict.kind,
                account=verdict.account,
                updated_at=now.isoformat(),
            )
        for tmux_session, prior in existing_by_session.items():
            if tmux_session in seen_this_sweep:
                continue
            age_seconds = (now - datetime.fromisoformat(prior.updated_at.replace("Z", "+00:00"))).total_seconds()
            if age_seconds <= freshness_seconds:
                update.disappeared.append(prior)
                merged[tmux_session] = prior  # retained, not yet stale enough to prune
            # else: silently pruned -- too stale to matter, and retaining it
            # forever would let a resolved/renamed lane linger indefinitely.
        _write_registry(root, sorted(merged.values(), key=lambda entry: entry.tmux_session))
    if update.account_changed or update.disappeared:
        logger.warning(
            "account_registry_changed",
            account_changed=update.account_changed,
            disappeared=[entry.tmux_session for entry in update.disappeared],
        )
    return update
