"""board_updater — deterministic facts.json writer: backup, write, validate,
roll back on validator failure. The real board directory and the set of
valid hosts/owner are always parameters supplied by the caller; this module
never hardcodes a deployment's own hostnames or ownership string.

Validator constraints are generic by default: ``selfcheck`` needs string
keys ``solid``/``weak``/``unsure``, and every ``log[*].chip_target`` must be
null or match a current session id. ``snapshot_owner``, per-session
``host``, and the set of valid ``state.cls`` values are validated against
``VALID_STATE_CLS`` by default -- module-level constants matching this
project's own board schema -- but can be overridden per call via
``expected_owner``/``valid_hosts``/``valid_state_cls`` for a deployment with
its own owner string, host allowlist, or state-class set.

No LLM calls in this module's own code path — deterministic validate-then-write only.
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
REQUIRED_SELFCHECK_KEYS = frozenset({"solid", "weak", "unsure"})
BOARD_REQUIRED_STATE_CLASSES = frozenset({"st-you", "st-work", "st-done", "st-stuck", "st-style"})


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    errors: list[str]


def validate_board_facts(
    facts: dict[str, Any], *, expected_owner: str | None = None, valid_hosts: set[str] | None = None
) -> ValidationResult:
    """Strictly validate the public facts schema consumed by ``chitra.board``.

    ``write_facts`` remains a generic validate-and-write primitive for callers
    that use a smaller document.  Rendering is deliberately stricter: every
    field interpolated into the HTML must have the expected shape before an
    operator is shown a new board.
    """
    errors: list[str] = []

    def require(value: Any, expected: type[Any], path: str) -> Any:
        if not isinstance(value, expected):
            errors.append(f"{path} must be {expected.__name__}")
        return value

    require(facts.get("generated_note"), str, "$.generated_note")
    snapshot_owner = facts.get("snapshot_owner")
    require(snapshot_owner, str, "$.snapshot_owner")
    if expected_owner is not None and snapshot_owner != expected_owner:
        errors.append(f"$.snapshot_owner must be exactly {expected_owner!r}")
    sessions = facts.get("sessions")
    if not isinstance(sessions, list):
        errors.append("$.sessions must be list")
        sessions = []
    elif not 1 <= len(sessions) <= 12:
        errors.append("$.sessions must contain between 1 and 12 sessions")
    session_ids: set[str] = set()
    for index, session in enumerate(sessions):
        path = f"$.sessions[{index}]"
        if not isinstance(session, dict):
            errors.append(f"{path} must be dict")
            continue
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id.startswith("row-"):
            errors.append(f"{path}.id must be a string beginning with 'row-'")
        elif session_id in session_ids:
            errors.append(f"{path}.id duplicates {session_id}")
        else:
            session_ids.add(session_id)
        for key in ("name", "sid", "goal", "doing"):
            require(session.get(key), str, f"{path}.{key}")
        require(session.get("wants"), bool, f"{path}.wants")
        if session.get("you") is not None:
            require(session.get("you"), str, f"{path}.you")
        state = session.get("state")
        if not isinstance(state, dict):
            errors.append(f"{path}.state must be dict")
        else:
            require(state.get("word"), str, f"{path}.state.word")
            state_class = state.get("cls")
            if not isinstance(state_class, str) or state_class not in BOARD_REQUIRED_STATE_CLASSES:
                errors.append(f"{path}.state.cls must be one of {sorted(BOARD_REQUIRED_STATE_CLASSES)}")
            require(state.get("extra"), str, f"{path}.state.extra")
        detail = session.get("detail")
        if not isinstance(detail, list) or not detail:
            errors.append(f"{path}.detail must be a non-empty list")
        else:
            for item_index, item in enumerate(detail):
                item_path = f"{path}.detail[{item_index}]"
                if not isinstance(item, dict):
                    errors.append(f"{item_path} must be dict")
                    continue
                if item.get("kv") is not None:
                    require(item.get("kv"), str, f"{item_path}.kv")
                require(item.get("text"), str, f"{item_path}.text")
        tmux = session.get("tmux")
        if not isinstance(tmux, dict):
            errors.append(f"{path}.tmux must be dict")
        else:
            host = tmux.get("host")
            require(host, str, f"{path}.tmux.host")
            if valid_hosts is not None and host not in valid_hosts:
                errors.append(f"{path}.tmux.host must be one of {sorted(valid_hosts)}")
            require(tmux.get("session"), str, f"{path}.tmux.session")
    log = facts.get("log")
    if not isinstance(log, list):
        errors.append("$.log must be list")
    else:
        for index, row in enumerate(log):
            path = f"$.log[{index}]"
            if not isinstance(row, dict):
                errors.append(f"{path} must be dict")
                continue
            for key in ("t", "chip", "text"):
                require(row.get(key), str, f"{path}.{key}")
            target = row.get("chip_target")
            if target is not None and (not isinstance(target, str) or target not in session_ids):
                errors.append(f"{path}.chip_target must match a session id or be null")
    selfcheck = facts.get("selfcheck")
    if not isinstance(selfcheck, dict):
        errors.append("$.selfcheck must be dict")
    else:
        for key in REQUIRED_SELFCHECK_KEYS:
            require(selfcheck.get(key), str, f"$.selfcheck.{key}")
    return ValidationResult(ok=not errors, errors=errors)


def validate_facts(
    facts: dict[str, Any],
    *,
    expected_owner: str | None = None,
    valid_hosts: set[str] | None = None,
    valid_state_cls: set[str] | None = None,
    required_selfcheck_keys: frozenset[str] | None = REQUIRED_SELFCHECK_KEYS,
) -> ValidationResult:
    """Validate a facts.json-shaped dict against the board schema constraints.

    ``expected_owner`` and ``valid_hosts`` are optional deployment-specific
    constraints (e.g. a fixed ``snapshot_owner`` string, an allowlist of
    known host names) — omit either to skip that check entirely.
    ``valid_state_cls`` overrides the module-level ``VALID_STATE_CLS``
    default for a deployment with its own set of ``state.cls`` values.

    Returns ``ok=False`` with the first-found errors (not exhaustive by
    design — callers roll back on any failure, so "first error" is enough
    signal without over-engineering an error-aggregation contract).
    """
    valid_state_cls = valid_state_cls if valid_state_cls is not None else VALID_STATE_CLS
    errors: list[str] = []

    if expected_owner is not None and facts.get("snapshot_owner") != expected_owner:
        errors.append(f"snapshot_owner must be exactly {expected_owner!r}")

    if required_selfcheck_keys is not None:
        selfcheck = facts.get("selfcheck")
        if not isinstance(selfcheck, dict) or not required_selfcheck_keys.issubset(selfcheck.keys()):
            errors.append(f"selfcheck must be a dict with keys {sorted(required_selfcheck_keys)}")
        elif not all(isinstance(selfcheck[k], str) for k in required_selfcheck_keys):
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
                    if cls is not None and cls not in valid_state_cls:
                        errors.append(f"state.cls '{cls}' not in {sorted(valid_state_cls)}")
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
    valid_state_cls: set[str] | None = None,
    required_selfcheck_keys: frozenset[str] | None = REQUIRED_SELFCHECK_KEYS,
) -> dict[str, Any]:
    """Validate, backup, write, and (on validator failure) roll back.

    ``expected_owner``/``valid_hosts``/``valid_state_cls`` are forwarded to
    ``validate_facts``.

    Returns a result dict: ``{"ok": bool, "path": str, "backup": str | None,
    "errors": list[str]}``. On failure, the pre-existing facts.json (if any)
    is left untouched — this function never leaves a known-invalid file live.
    """
    board_dir.mkdir(parents=True, exist_ok=True)
    target = board_dir / facts_filename

    validation = validate_facts(
        facts,
        expected_owner=expected_owner,
        valid_hosts=valid_hosts,
        valid_state_cls=valid_state_cls,
        required_selfcheck_keys=required_selfcheck_keys,
    )
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
