"""board_updater — deterministic facts.json writer: backup, write, validate,
roll back on validator failure. The real board directory and the set of
valid hosts/owner are always parameters supplied by the caller; this module
never hardcodes a deployment's own hostnames or ownership string.

Validator constraints are generic by default: ``selfcheck`` needs string
keys ``solid``/``weak``/``unsure``, every ``log[*].chip_target`` must be
null or match a current session id, and ``state.cls`` must be one of a
known set. ``snapshot_owner`` and per-session ``host`` are validated only
when the caller supplies ``expected_owner``/``valid_hosts`` — deployments
with their own board schema constant (a fixed owner string, a fixed host
allowlist) pass those in rather than this module baking in someone else's
deployment topology.

No LLM calls. Deterministic validate-then-write only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

VALID_STATE_CLS = {"st-you", "st-work", "st-done", "st-stuck", "st-style"}
REQUIRED_SELFCHECK_KEYS = {"solid", "weak", "unsure"}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    errors: list[str]


def validate_facts(
    facts: dict[str, Any],
    *,
    expected_owner: str | None = None,
    valid_hosts: set[str] | None = None,
) -> ValidationResult:
    """Validate a facts.json-shaped dict against the board schema constraints.

    ``expected_owner`` and ``valid_hosts`` are optional deployment-specific
    constraints (e.g. a fixed ``snapshot_owner`` string, an allowlist of
    known host names) — omit either to skip that check entirely.

    Returns ``ok=False`` with the first-found errors (not exhaustive by
    design — callers roll back on any failure, so "first error" is enough
    signal without over-engineering an error-aggregation contract).
    """
    errors: list[str] = []

    if expected_owner is not None and facts.get("snapshot_owner") != expected_owner:
        errors.append(f"snapshot_owner must be exactly {expected_owner!r}")

    selfcheck = facts.get("selfcheck")
    if not isinstance(selfcheck, dict) or not REQUIRED_SELFCHECK_KEYS.issubset(selfcheck.keys()):
        errors.append(f"selfcheck must be a dict with keys {sorted(REQUIRED_SELFCHECK_KEYS)}")
    elif not all(isinstance(selfcheck[k], str) for k in REQUIRED_SELFCHECK_KEYS):
        errors.append("selfcheck values must all be strings")

    sessions = facts.get("sessions")
    session_ids: set[str] = set()
    if isinstance(sessions, list):
        for session in sessions:
            if isinstance(session, dict) and isinstance(session.get("id"), str):
                session_ids.add(session["id"])
                state = session.get("state")
                if isinstance(state, dict):
                    cls = state.get("cls")
                    if cls is not None and cls not in VALID_STATE_CLS:
                        errors.append(f"state.cls '{cls}' not in {sorted(VALID_STATE_CLS)}")
                host = session.get("host")
                if valid_hosts is not None and host is not None and host not in valid_hosts:
                    errors.append(f"session host '{host}' not in {sorted(valid_hosts)}")
                detail = state.get("detail") if isinstance(state, dict) else None
                if detail is not None and detail == "":
                    errors.append(f"session {session.get('id')}: detail must be non-empty when present")

    log_entries = facts.get("log")
    if isinstance(log_entries, list):
        for entry in log_entries:
            if not isinstance(entry, dict):
                continue
            chip_target = entry.get("chip_target")
            if chip_target is not None and chip_target not in session_ids:
                errors.append(f"log entry chip_target '{chip_target}' does not match any current session id")

    return ValidationResult(ok=not errors, errors=errors)


def write_facts(
    facts: dict[str, Any],
    *,
    board_dir: Path,
    facts_filename: str = "facts.json",
    expected_owner: str | None = None,
    valid_hosts: set[str] | None = None,
) -> dict[str, Any]:
    """Validate, backup, write, and (on validator failure) roll back.

    ``expected_owner``/``valid_hosts`` are forwarded to ``validate_facts``.

    Returns a result dict: ``{"ok": bool, "path": str, "backup": str | None,
    "errors": list[str]}``. On failure, the pre-existing facts.json (if any)
    is left untouched — this function never leaves a known-invalid file live.
    """
    board_dir.mkdir(parents=True, exist_ok=True)
    target = board_dir / facts_filename

    validation = validate_facts(facts, expected_owner=expected_owner, valid_hosts=valid_hosts)
    if not validation.ok:
        logger.warning("board_updater_validation_failed", errors=validation.errors)
        return {"ok": False, "path": str(target), "backup": None, "errors": validation.errors}

    backup_path: Path | None = None
    if target.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        backup_path = board_dir / f"{facts_filename}.bak-{stamp}"
        backup_path.write_bytes(target.read_bytes())

    tmp = board_dir / f".{facts_filename}.tmp"
    tmp.write_text(json.dumps(facts, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)

    logger.info("board_updater_written", path=str(target), backup=str(backup_path) if backup_path else None)
    return {"ok": True, "path": str(target), "backup": str(backup_path) if backup_path else None, "errors": []}
