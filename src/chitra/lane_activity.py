"""Durable backend-neutral pane activity facts emitted by ``chitra.watchd``.

The load-shed selector is a one-shot process, while watchd is the component
that already observes pane changes.  This small state file bridges those two
lifetimes without teaching the load ladder to inspect conversation content.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from chitra.state_paths import state_dir

SCHEMA = "chitra.lane-activity.v1"
LaneBackend = Literal["claude", "codex", "unknown"]


@dataclass(frozen=True, slots=True)
class LaneActivity:
    """Last pane-change and attachment facts for one tracked session."""

    session_ref: str
    pane_id: str
    last_change_at: str
    last_seen_at: str
    attached: bool
    backend: LaneBackend = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "session_ref": self.session_ref,
            "pane_id": self.pane_id,
            "last_change_at": self.last_change_at,
            "last_seen_at": self.last_seen_at,
            "attached": self.attached,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, payload: object) -> LaneActivity:
        if not isinstance(payload, dict):
            raise ValueError("lane activity record must be an object")
        strings: dict[str, str] = {}
        for name in ("session_ref", "pane_id", "last_change_at", "last_seen_at"):
            value = payload.get(name)
            if not isinstance(value, str):
                raise ValueError(f"lane activity {name} must be a string")
            strings[name] = value
        attached = payload.get("attached")
        if not isinstance(attached, bool):
            raise ValueError("lane activity attached must be a boolean")
        backend = payload.get("backend", "unknown")
        if backend not in ("claude", "codex", "unknown"):
            raise ValueError("lane activity backend must be claude, codex, or unknown")
        return cls(**strings, attached=attached, backend=cast(LaneBackend, backend))


def activity_path(root: Path | None = None) -> Path:
    """Return the watchd activity-state path beneath ``root``."""
    return (state_dir() if root is None else root) / "lane_activity.json"


@contextlib.contextmanager
def _activity_lock(root: Path | None) -> Iterator[None]:
    path = activity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(f".{path.name}.lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_lane_activity(root: Path | None = None) -> list[LaneActivity]:
    """Load current activity facts; a missing file means none observed yet."""
    path = activity_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("lane_activity.json is not a chitra.lane-activity.v1 document")
    raw = payload.get("lanes")
    if not isinstance(raw, list):
        raise ValueError("lane_activity.json lanes must be a list")
    return [LaneActivity.from_dict(item) for item in raw]


def _write_activity(root: Path | None, records: list[LaneActivity]) -> None:
    path = activity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "lanes": [record.to_dict() for record in records]}
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.replace(tmp.name, path)
            tmp_name = None
    finally:
        if tmp_name is not None and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def upsert_lane_activity(root: Path | None, records: Iterable[LaneActivity]) -> None:
    """Atomically merge a watchd poll's activity facts by session reference."""
    incoming = list(records)
    if not incoming:
        return
    with _activity_lock(root):
        merged = {record.session_ref: record for record in load_lane_activity(root)}
        merged.update((record.session_ref, record) for record in incoming)
        _write_activity(root, sorted(merged.values(), key=lambda record: record.session_ref))
