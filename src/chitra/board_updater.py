"""Strict validation for facts consumed by the operator board.

No LLM calls in this module's own code path — deterministic validation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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

    Every field interpolated into the HTML must have the expected shape before
    an operator is shown a new board.
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
