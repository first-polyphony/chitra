"""Pure merge-queue hygiene decisions plus small durable hold bookkeeping.

The decision engine consumes only a caller-supplied pull-request snapshot.  It
does not invoke ``gh``, start a subprocess, reach the network, merge a pull
request, or create a branch.  A caller may use its returned labels/comment as
an input to a separately authorized control plane.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import structlog

from chitra.capabilities import require_enabled
from chitra.state_paths import default_queue_holds_path, default_queue_hygiene_log_path, state_dir

logger = structlog.get_logger(__name__)

CheckState = Literal["green", "red", "pending", "missing"]
QueueAction = Literal["none", "wait", "hold-red", "release-hold", "repair-labels", "reassert-hold", "escalate"]
QUEUE_ACTIONS: tuple[QueueAction, ...] = (
    "none",
    "wait",
    "hold-red",
    "release-hold",
    "repair-labels",
    "reassert-hold",
    "escalate",
)
QUEUE_HOLDS_SCHEMA = "chitra.queue_holds.v1"
QUEUE_HYGIENE_SCHEMA = "chitra.queue_hygiene.v1"
READY_LABEL = "queue:ready"
HOLD_LABEL = "queue:hold"


class QueueSnapshotError(ValueError):
    """Raised when a queue snapshot, policy, marker, or store is unreadable."""


class QueueInvariantError(ValueError):
    """Raised when a queue decision could violate a non-negotiable invariant."""


class QueueHoldNotFoundError(KeyError):
    """Raised when an explicit close requests a hold absent from the state store."""


def _object(payload: object, *, name: str) -> dict[str, object]:
    """Require a JSON-like object with string keys."""
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise QueueSnapshotError(f"{name} must be an object")
    return cast(dict[str, object], payload)


def _exact_fields(payload: dict[str, object], *, name: str, fields: tuple[str, ...]) -> None:
    """Reject missing or undocumented fields for deterministic snapshot parsing."""
    actual = set(payload)
    expected = set(fields)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown {', '.join(unknown)}")
        raise QueueSnapshotError(f"{name} has " + "; ".join(parts) + " fields")


def _string(value: object, *, field: str, nonempty: bool = True) -> str:
    """Require a string field, optionally rejecting blank values."""
    if not isinstance(value, str) or (nonempty and not value.strip()):
        qualifier = " non-empty" if nonempty else ""
        raise QueueSnapshotError(f"{field} must be a{qualifier} string")
    return value


def _integer(value: object, *, field: str, minimum: int | None = None) -> int:
    """Require an integer field without allowing JSON booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise QueueSnapshotError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise QueueSnapshotError(f"{field} must be at least {minimum}")
    return value


def _boolean(value: object, *, field: str) -> bool:
    """Require a real boolean rather than a truthy JSON value."""
    if not isinstance(value, bool):
        raise QueueSnapshotError(f"{field} must be a boolean")
    return value


def _parse_iso8601(value: str, *, field: str) -> datetime:
    """Parse an aware ISO8601 timestamp in the goals-store style."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueueSnapshotError(f"{field} must be an ISO8601 datetime") from exc
    if parsed.tzinfo is None:
        raise QueueSnapshotError(f"{field} must be an ISO8601 datetime with timezone")
    return parsed


def _timestamps(value: object, *, field: str) -> str:
    """Validate and retain the source spelling of an ISO8601 timestamp."""
    rendered = _string(value, field=field)
    _parse_iso8601(rendered, field=field)
    return rendered


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    """Read a JSON array of non-empty strings into immutable state."""
    if not isinstance(value, (list, tuple)):
        raise QueueSnapshotError(f"{field} must be a list of strings")
    items = tuple(_string(item, field=f"{field} item") for item in value)
    if len(set(items)) != len(items):
        raise QueueSnapshotError(f"{field} must not contain duplicate strings")
    return items


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One required-check observation supplied by the caller's snapshot."""

    name: str
    app_id: int
    status: str
    conclusion: str | None
    started_at: str

    @classmethod
    def from_dict(cls, payload: object) -> CheckResult:
        """Strictly validate one check result from a JSON-like object."""
        values = _object(payload, name="check result")
        _exact_fields(values, name="check result", fields=("name", "app_id", "status", "conclusion", "started_at"))
        conclusion = values["conclusion"]
        if conclusion is not None and not isinstance(conclusion, str):
            raise QueueSnapshotError("check result conclusion must be a string or null")
        return cls(
            name=_string(values["name"], field="check result name"),
            app_id=_integer(values["app_id"], field="check result app_id", minimum=1),
            status=_string(values["status"], field="check result status"),
            conclusion=conclusion,
            started_at=_timestamps(values["started_at"], field="check result started_at"),
        )

    def to_dict(self) -> dict[str, object]:
        """Render this immutable check result as JSON-compatible data."""
        return {
            "name": self.name,
            "app_id": self.app_id,
            "status": self.status,
            "conclusion": self.conclusion,
            "started_at": self.started_at,
        }


@dataclass(frozen=True, slots=True)
class HoldMarker:
    """The chitra-owned provenance needed to safely manage one queue hold."""

    pr: int
    head_sha: str
    red_since: str
    reassert_count: int

    @classmethod
    def from_dict(cls, payload: object) -> HoldMarker:
        """Strictly validate a durable hold marker."""
        values = _object(payload, name="hold marker")
        _exact_fields(values, name="hold marker", fields=("pr", "head_sha", "red_since", "reassert_count"))
        return cls(
            pr=_integer(values["pr"], field="hold marker pr", minimum=1),
            head_sha=_string(values["head_sha"], field="hold marker head_sha"),
            red_since=_timestamps(values["red_since"], field="hold marker red_since"),
            reassert_count=_integer(values["reassert_count"], field="hold marker reassert_count", minimum=0),
        )

    def to_dict(self) -> dict[str, object]:
        """Render this durable marker as JSON-compatible data."""
        return {
            "pr": self.pr,
            "head_sha": self.head_sha,
            "red_since": self.red_since,
            "reassert_count": self.reassert_count,
        }


@dataclass(frozen=True, slots=True)
class QueueHeadSnapshot:
    """The complete caller-supplied authority boundary for one queue head."""

    number: int
    head_sha: str
    created_at: str
    is_draft: bool
    is_cross_repo: bool
    mergeable: bool | None
    merge_state: str
    labels: tuple[str, ...]
    checks: tuple[CheckResult, ...]
    chitra_hold_marker: HoldMarker | None
    control_plane_paths: bool
    observed_at: str

    @classmethod
    def from_dict(cls, payload: object) -> QueueHeadSnapshot:
        """Strictly validate a queue-head snapshot and all nested objects."""
        values = _object(payload, name="queue head snapshot")
        _exact_fields(
            values,
            name="queue head snapshot",
            fields=(
                "number",
                "head_sha",
                "created_at",
                "is_draft",
                "is_cross_repo",
                "mergeable",
                "merge_state",
                "labels",
                "checks",
                "chitra_hold_marker",
                "control_plane_paths",
                "observed_at",
            ),
        )
        raw_mergeable = values["mergeable"]
        if raw_mergeable is not None and not isinstance(raw_mergeable, bool):
            raise QueueSnapshotError("queue head snapshot mergeable must be a boolean or null")
        raw_checks = values["checks"]
        if not isinstance(raw_checks, (list, tuple)):
            raise QueueSnapshotError("queue head snapshot checks must be a list")
        raw_marker = values["chitra_hold_marker"]
        if raw_marker is not None and not isinstance(raw_marker, dict):
            raise QueueSnapshotError("queue head snapshot chitra_hold_marker must be an object or null")
        marker = HoldMarker.from_dict(raw_marker) if raw_marker is not None else None
        number = _integer(values["number"], field="queue head snapshot number", minimum=1)
        if marker is not None and marker.pr != number:
            raise QueueSnapshotError("queue head snapshot marker pr must match snapshot number")
        return cls(
            number=number,
            head_sha=_string(values["head_sha"], field="queue head snapshot head_sha"),
            created_at=_timestamps(values["created_at"], field="queue head snapshot created_at"),
            is_draft=_boolean(values["is_draft"], field="queue head snapshot is_draft"),
            is_cross_repo=_boolean(values["is_cross_repo"], field="queue head snapshot is_cross_repo"),
            mergeable=raw_mergeable,
            merge_state=_string(values["merge_state"], field="queue head snapshot merge_state"),
            labels=_string_tuple(values["labels"], field="queue head snapshot labels"),
            checks=tuple(CheckResult.from_dict(item) for item in raw_checks),
            chitra_hold_marker=marker,
            control_plane_paths=_boolean(values["control_plane_paths"], field="queue head snapshot control_plane_paths"),
            observed_at=_timestamps(values["observed_at"], field="queue head snapshot observed_at"),
        )

    def to_dict(self) -> dict[str, object]:
        """Render this snapshot as a strict JSON-compatible object."""
        return {
            "number": self.number,
            "head_sha": self.head_sha,
            "created_at": self.created_at,
            "is_draft": self.is_draft,
            "is_cross_repo": self.is_cross_repo,
            "mergeable": self.mergeable,
            "merge_state": self.merge_state,
            "labels": list(self.labels),
            "checks": [check.to_dict() for check in self.checks],
            "chitra_hold_marker": None if self.chitra_hold_marker is None else self.chitra_hold_marker.to_dict(),
            "control_plane_paths": self.control_plane_paths,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class QueueDecision:
    """A narrow, non-executing result for an external queue control plane."""

    action: QueueAction
    reason: str
    comment_body: str
    labels_to_add: tuple[str, ...]
    labels_to_remove: tuple[str, ...]

    def __post_init__(self) -> None:
        """Make merge, branching, and approval literally unrepresentable actions."""
        if self.action not in QUEUE_ACTIONS:
            raise QueueInvariantError("INV-1: action must be a declared non-merge queue action")
        forbidden = ("merge", "branch", "approve")
        if any(token in self.action for token in forbidden):
            raise QueueInvariantError("INV-1: merge, branch, and approve actions are unrepresentable")
        if len(set(self.labels_to_add)) != len(self.labels_to_add) or len(set(self.labels_to_remove)) != len(self.labels_to_remove):
            raise QueueInvariantError("queue decision labels must not repeat")
        if set(self.labels_to_add) & set(self.labels_to_remove):
            raise QueueInvariantError("queue decision cannot add and remove the same label")
        if HOLD_LABEL in self.labels_to_add and READY_LABEL in self.labels_to_add:
            raise QueueInvariantError("INV-3: queue:hold implies not queue:ready")

    @classmethod
    def from_dict(cls, payload: object) -> QueueDecision:
        """Strictly validate a persisted or CLI-supplied queue decision."""
        values = _object(payload, name="queue decision")
        _exact_fields(
            values,
            name="queue decision",
            fields=("action", "reason", "comment_body", "labels_to_add", "labels_to_remove"),
        )
        action = _string(values["action"], field="queue decision action")
        if action not in QUEUE_ACTIONS:
            raise QueueInvariantError("INV-1: action must be a declared non-merge queue action")
        return cls(
            action=action,
            reason=_string(values["reason"], field="queue decision reason", nonempty=False),
            comment_body=_string(values["comment_body"], field="queue decision comment_body", nonempty=False),
            labels_to_add=_string_tuple(values["labels_to_add"], field="queue decision labels_to_add"),
            labels_to_remove=_string_tuple(values["labels_to_remove"], field="queue decision labels_to_remove"),
        )

    def to_dict(self) -> dict[str, object]:
        """Render this decision as a JSON-compatible verdict."""
        return {
            "action": self.action,
            "reason": self.reason,
            "comment_body": self.comment_body,
            "labels_to_add": list(self.labels_to_add),
            "labels_to_remove": list(self.labels_to_remove),
        }


@dataclass(frozen=True, slots=True)
class RequiredCheck:
    """The exact name/application pair required by a queue policy."""

    name: str
    app_id: int

    @classmethod
    def from_dict(cls, payload: object) -> RequiredCheck:
        """Strictly validate one policy check selector."""
        values = _object(payload, name="required check")
        _exact_fields(values, name="required check", fields=("name", "app_id"))
        return cls(
            name=_string(values["name"], field="required check name"),
            app_id=_integer(values["app_id"], field="required check app_id", minimum=1),
        )


def _coerce_required_checks(value: object) -> tuple[RequiredCheck, ...]:
    """Accept JSON selectors and concise ``(name, app_id)`` test inputs."""
    if not isinstance(value, (list, tuple)):
        raise QueueSnapshotError("policy required checks must be a list")
    selectors: list[RequiredCheck] = []
    for item in value:
        if isinstance(item, RequiredCheck):
            selectors.append(item)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            selectors.append(
                RequiredCheck(
                    name=_string(item[0], field="required check name"),
                    app_id=_integer(item[1], field="required check app_id", minimum=1),
                )
            )
        else:
            selectors.append(RequiredCheck.from_dict(item))
    keys = [(selector.name, selector.app_id) for selector in selectors]
    if len(set(keys)) != len(keys):
        raise QueueSnapshotError("policy required checks must not repeat")
    return tuple(selectors)


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """The small caller-supplied policy used by the pure decision engine."""

    required: tuple[RequiredCheck, ...] = ()
    red_threshold_minutes: int = 15

    def __post_init__(self) -> None:
        """Reject impossible red thresholds before a queue decision runs."""
        if self.red_threshold_minutes < 0:
            raise QueueSnapshotError("policy red_threshold_minutes must be non-negative")
        keys = [(selector.name, selector.app_id) for selector in self.required]
        if len(set(keys)) != len(keys):
            raise QueueSnapshotError("policy required checks must not repeat")

    @classmethod
    def from_dict(cls, payload: object) -> QueuePolicy:
        """Strictly validate JSON policy with a useful legacy key alias."""
        values = _object(payload, name="queue policy")
        unknown = set(values) - {"required", "required_checks", "red_threshold_minutes"}
        if unknown:
            raise QueueSnapshotError(f"queue policy has unknown fields: {', '.join(sorted(unknown))}")
        if "required" in values and "required_checks" in values:
            raise QueueSnapshotError("queue policy must use only one of required or required_checks")
        required_value = values.get("required", values.get("required_checks", []))
        threshold = values.get("red_threshold_minutes", 15)
        return cls(
            required=_coerce_required_checks(required_value),
            red_threshold_minutes=_integer(threshold, field="policy red_threshold_minutes", minimum=0),
        )


def _coerce_policy(policy: QueuePolicy | Mapping[str, object] | None) -> QueuePolicy:
    """Normalize documented policy inputs without silently accepting other types."""
    if policy is None:
        return QueuePolicy()
    if isinstance(policy, QueuePolicy):
        return policy
    return QueuePolicy.from_dict(policy)


def _latest_required_checks(
    checks: Iterable[CheckResult], required: tuple[RequiredCheck, ...]
) -> dict[tuple[str, int], CheckResult]:
    """Select the latest check by ``started_at`` for every exact required pair."""
    required_keys = {(selector.name, selector.app_id) for selector in required}
    latest: dict[tuple[str, int], CheckResult] = {}
    for check in checks:
        key = (check.name, check.app_id)
        if key not in required_keys:
            continue
        existing = latest.get(key)
        if existing is None or _parse_iso8601(check.started_at, field="check result started_at") >= _parse_iso8601(
            existing.started_at, field="check result started_at"
        ):
            latest[key] = check
    return latest


def _check_state(check: CheckResult) -> CheckState:
    """Classify a latest check result without treating a wrong app as relevant."""
    status = check.status.casefold()
    conclusion = "" if check.conclusion is None else check.conclusion.casefold()
    if status in {"queued", "pending", "in_progress", "waiting", "requested"} or status != "completed":
        return "pending"
    if conclusion in {"success", "neutral", "skipped"}:
        return "green"
    if conclusion in {"failure", "failed", "timed_out", "timed-out", "cancelled", "canceled", "action_required"}:
        return "red"
    return "pending"


def classify_checks(checks: Iterable[CheckResult], required: object) -> CheckState:
    """Classify the latest exact-app required checks as green/red/pending/missing.

    Pending takes precedence over red because a newer queued or running
    required check can still replace an earlier failure.  A missing selector is
    also a wait condition rather than an implicit success.
    """
    selectors = _coerce_required_checks(required)
    latest = _latest_required_checks(checks, selectors)
    states: list[CheckState] = []
    for selector in selectors:
        check = latest.get((selector.name, selector.app_id))
        states.append("missing" if check is None else _check_state(check))
    if "pending" in states:
        return "pending"
    if "missing" in states:
        return "missing"
    if "red" in states:
        return "red"
    return "green"


_HOLD_MARKER_RE = re.compile(
    r"^<!-- chitra-queue-hold v1 pr=(?P<pr>[1-9][0-9]*) head_sha=(?P<head_sha>[^\s]+) "
    r"red_since=(?P<red_since>[^\s]+) holder=chitra -->$"
)
_REASSERT_MARKER_RE = re.compile(r"^<!-- chitra-queue-reassert-count v1 count=(?P<count>[0-9]+) -->$")


def render_hold_comment(marker: HoldMarker) -> str:
    """Render a stable, parseable chitra hold comment for an external PR body."""
    lines = [
        (
            f"<!-- chitra-queue-hold v1 pr={marker.pr} head_sha={marker.head_sha} "
            f"red_since={marker.red_since} holder=chitra -->"
        )
    ]
    if marker.reassert_count:
        lines.append(f"<!-- chitra-queue-reassert-count v1 count={marker.reassert_count} -->")
    lines.extend(
        (
            "Chitra placed a queue hold after required checks stayed red past the configured threshold.",
            "This marker authorizes only chitra-owned hold hygiene; it does not authorize a merge, approval, or branch change.",
        )
    )
    return "\n".join(lines)


def parse_hold_marker(comment: str) -> HoldMarker | None:
    """Parse a rendered hold comment, returning ``None`` for unrelated comments."""
    lines = comment.splitlines()
    if not lines:
        return None
    matched = _HOLD_MARKER_RE.fullmatch(lines[0])
    if matched is None:
        if lines[0].startswith("<!-- chitra-queue-hold"):
            raise QueueSnapshotError("malformed chitra queue hold marker")
        return None
    reassert_count = 0
    if len(lines) > 1:
        count_match = _REASSERT_MARKER_RE.fullmatch(lines[1])
        if count_match is not None:
            reassert_count = int(count_match.group("count"))
    return HoldMarker(
        pr=int(matched.group("pr")),
        head_sha=matched.group("head_sha"),
        red_since=_timestamps(matched.group("red_since"), field="hold marker red_since"),
        reassert_count=reassert_count,
    )


def _current_time(now: datetime | None) -> datetime:
    """Resolve a supplied or live timestamp and require timezone awareness."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise QueueSnapshotError("now must be timezone-aware")
    return current


def _red_since(snapshot: QueueHeadSnapshot, policy: QueuePolicy, *, conflict: bool) -> datetime:
    """Derive the red-start bound from the latest failing check or PR creation."""
    if not conflict:
        latest = _latest_required_checks(snapshot.checks, policy.required)
        failures = [
            _parse_iso8601(check.started_at, field="check result started_at")
            for check in latest.values()
            if _check_state(check) == "red"
        ]
        if failures:
            return max(failures)
    return _parse_iso8601(snapshot.created_at, field="queue head snapshot created_at")


def validate_decision(
    decision: QueueDecision,
    snapshot: QueueHeadSnapshot | None,
    *,
    check_state: CheckState | None,
) -> None:
    """Enforce all queue invariants against a candidate decision and snapshot."""
    if decision.action not in QUEUE_ACTIONS or any(token in decision.action for token in ("merge", "branch", "approve")):
        raise QueueInvariantError("INV-1: merge, branch, and approve actions are unrepresentable")
    if snapshot is None:
        if HOLD_LABEL in decision.labels_to_add and READY_LABEL in decision.labels_to_add:
            raise QueueInvariantError("INV-3: queue:hold implies not queue:ready")
        return
    if READY_LABEL in decision.labels_to_add and check_state != "green":
        raise QueueInvariantError("INV-2: never add queue:ready to a non-green head")
    final_labels = set(snapshot.labels)
    final_labels.difference_update(decision.labels_to_remove)
    final_labels.update(decision.labels_to_add)
    if HOLD_LABEL in final_labels and READY_LABEL in final_labels:
        raise QueueInvariantError("INV-3: queue:hold implies not queue:ready")
    if HOLD_LABEL in decision.labels_to_remove and snapshot.chitra_hold_marker is None:
        raise QueueInvariantError("INV-4: never remove queue:hold lacking a chitra marker")


def assert_invariants(
    decision: QueueDecision,
    snapshot: QueueHeadSnapshot | None,
    *,
    check_state: CheckState | None,
) -> None:
    """Alias with an imperative name for callers checking a manually built decision."""
    validate_decision(decision, snapshot, check_state=check_state)


def _validated(
    action: QueueAction,
    reason: str,
    snapshot: QueueHeadSnapshot | None,
    check_state: CheckState | None,
    *,
    comment_body: str = "",
    labels_to_add: tuple[str, ...] = (),
    labels_to_remove: tuple[str, ...] = (),
) -> QueueDecision:
    """Create and invariant-check one decision before exposing it to a caller."""
    decision = QueueDecision(
        action=action,
        reason=reason,
        comment_body=comment_body,
        labels_to_add=labels_to_add,
        labels_to_remove=labels_to_remove,
    )
    validate_decision(decision, snapshot, check_state=check_state)
    return decision


def decide(
    snapshot: QueueHeadSnapshot | None,
    policy: QueuePolicy | Mapping[str, object] | None,
    now: datetime,
) -> QueueDecision:
    """Return the complete pure queue-hygiene decision for one snapshot.

    ``None`` represents an unreadable snapshot boundary and is deliberately an
    escalation rather than a best-effort inference from partial input.
    """
    current = _current_time(now)
    configured = _coerce_policy(policy)
    if snapshot is None:
        return _validated("escalate", "queue head snapshot is unreadable", None, None)

    labels = set(snapshot.labels)
    if HOLD_LABEL in labels and READY_LABEL in labels:
        return _validated(
            "repair-labels",
            "queue:hold and queue:ready are both present; remove queue:ready unconditionally",
            snapshot,
            None,
            labels_to_remove=(READY_LABEL,),
        )
    if snapshot.is_draft:
        return _validated("escalate", "draft queue head requires operator review", snapshot, None)
    if snapshot.is_cross_repo:
        return _validated("escalate", "cross-repository queue head requires operator review", snapshot, None)
    if snapshot.control_plane_paths:
        return _validated("escalate", "control-plane paths require operator review", snapshot, None)
    if HOLD_LABEL in labels and snapshot.chitra_hold_marker is None:
        return _validated("escalate", "queue hold lacks a chitra marker", snapshot, None)

    check_state = classify_checks(snapshot.checks, configured.required)
    conflict = snapshot.merge_state.casefold() == "conflicting"
    behind = snapshot.merge_state.casefold() == "behind"

    if check_state == "green" and not conflict:
        marker = snapshot.chitra_hold_marker
        if marker is not None or HOLD_LABEL in labels:
            assert marker is not None
            release_comment = (
                f"{render_hold_comment(marker)}\n\nRequired checks are green; release this chitra-owned queue hold."
            )
            return _validated(
                "release-hold",
                "all required checks are green; release the chitra-owned hold",
                snapshot,
                check_state,
                comment_body=release_comment,
                labels_to_add=(READY_LABEL,),
                labels_to_remove=(HOLD_LABEL,),
            )
        return _validated("none", "all required checks are green", snapshot, check_state)

    marker = snapshot.chitra_hold_marker
    if marker is not None and marker.head_sha == snapshot.head_sha and READY_LABEL in labels:
        if marker.reassert_count == 0:
            reasserted = replace(marker, reassert_count=1)
            return _validated(
                "reassert-hold",
                "queue:ready was re-added over the active chitra hold; reasserting once",
                snapshot,
                check_state,
                comment_body=render_hold_comment(reasserted),
                labels_to_add=(HOLD_LABEL,),
                labels_to_remove=(READY_LABEL,),
            )
        return _validated(
            "escalate",
            "queue:ready was re-added after chitra already reasserted this hold",
            snapshot,
            check_state,
        )

    if behind:
        return _validated("wait", "merge state is BEHIND", snapshot, check_state)
    if check_state == "pending":
        return _validated("wait", "at least one required check is pending or queued", snapshot, check_state)
    if check_state == "missing":
        return _validated("wait", "at least one required check is missing", snapshot, check_state)

    if check_state == "red" or conflict:
        red_since = _red_since(snapshot, configured, conflict=conflict)
        threshold = timedelta(minutes=configured.red_threshold_minutes)
        if current - red_since < threshold:
            return _validated(
                "wait",
                f"required checks have been red for less than {configured.red_threshold_minutes} minutes",
                snapshot,
                check_state,
            )
        if marker is not None and marker.head_sha == snapshot.head_sha and HOLD_LABEL in labels:
            return _validated("none", "active chitra hold already covers this red head", snapshot, check_state)
        new_marker = HoldMarker(
            pr=snapshot.number,
            head_sha=snapshot.head_sha,
            red_since=red_since.isoformat(),
            reassert_count=0,
        )
        return _validated(
            "hold-red",
            f"required checks have been red for at least {configured.red_threshold_minutes} minutes",
            snapshot,
            check_state,
            comment_body=render_hold_comment(new_marker),
            labels_to_add=(HOLD_LABEL,),
            labels_to_remove=(READY_LABEL,),
        )

    return _validated("wait", "queue head has no actionable deterministic verdict", snapshot, check_state)


def queue_holds_path(root: Path | None = None) -> Path:
    """Return the atomic current-state store for chitra-owned queue holds."""
    return default_queue_holds_path() if root is None else root / "queue_holds.json"


def queue_hygiene_log_path(root: Path | None = None) -> Path:
    """Return the append-only audit log for queue hygiene decisions."""
    return default_queue_hygiene_log_path() if root is None else root / "queue_hygiene.jsonl"


def _load_holds(root: Path | None = None) -> list[HoldMarker]:
    """Read the strict hold store, treating a missing document as no active holds."""
    path = queue_holds_path(root)
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueSnapshotError(f"invalid queue hold store {path}: {exc}") from exc
    values = _object(raw, name="queue hold store")
    _exact_fields(values, name="queue hold store", fields=("schema", "updated_at", "active_holds"))
    if values["schema"] != QUEUE_HOLDS_SCHEMA:
        raise QueueSnapshotError("queue_holds.json is not a chitra.queue_holds.v1 document")
    _timestamps(values["updated_at"], field="queue hold store updated_at")
    raw_holds = values["active_holds"]
    if not isinstance(raw_holds, list):
        raise QueueSnapshotError("queue hold store active_holds must be a list")
    holds = [HoldMarker.from_dict(item) for item in raw_holds]
    if len({marker.pr for marker in holds}) != len(holds):
        raise QueueSnapshotError("queue hold store must not contain duplicate PR holds")
    return holds


def active_holds(root: Path | None = None) -> list[HoldMarker]:
    """Return active chitra-owned holds in deterministic PR-number order."""
    return sorted(_load_holds(root), key=lambda marker: marker.pr)


def _write_holds(root: Path | None, holds: list[HoldMarker]) -> None:
    """Atomically replace the small hold store using the goals-store pattern."""
    path = queue_holds_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": QUEUE_HOLDS_SCHEMA,
        "updated_at": datetime.now(UTC).isoformat(),
        "active_holds": [marker.to_dict() for marker in sorted(holds, key=lambda item: item.pr)],
    }
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


def record_hold(root: Path | None, marker: HoldMarker) -> HoldMarker:
    """Atomically insert or replace one active hold by pull-request number."""
    holds = [existing for existing in _load_holds(root) if existing.pr != marker.pr]
    holds.append(marker)
    _write_holds(root, holds)
    logger.info("queue_hold_recorded", pr=marker.pr, head_sha=marker.head_sha, reassert_count=marker.reassert_count)
    return marker


def close_hold(root: Path | None, pr: int) -> HoldMarker:
    """Atomically remove one active hold, returning the exact closed marker."""
    holds = _load_holds(root)
    found = next((marker for marker in holds if marker.pr == pr), None)
    if found is None:
        raise QueueHoldNotFoundError(pr)
    _write_holds(root, [marker for marker in holds if marker.pr != pr])
    logger.info("queue_hold_closed", pr=pr, head_sha=found.head_sha)
    return found


def log_action(
    root: Path | None,
    decision: QueueDecision,
    *,
    marker: HoldMarker | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Append one queue-hygiene event without ever rewriting existing entries."""
    recorded_marker = marker if marker is not None else parse_hold_marker(decision.comment_body)
    entry: dict[str, object] = {
        "schema": QUEUE_HYGIENE_SCHEMA,
        "logged_at": _current_time(now).isoformat(),
        "action": decision.action,
        "reason": decision.reason,
        "comment_body": decision.comment_body,
        "labels_to_add": list(decision.labels_to_add),
        "labels_to_remove": list(decision.labels_to_remove),
        "pr": None if recorded_marker is None else recorded_marker.pr,
        "head_sha": None if recorded_marker is None else recorded_marker.head_sha,
    }
    path = queue_hygiene_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    logger.info("queue_hygiene_logged", action=decision.action, pr=entry["pr"])
    return entry


def record_decision(root: Path | None, decision: QueueDecision, *, now: datetime | None = None) -> dict[str, object]:
    """Persist the hold transition implied by a decision and append its audit row."""
    marker = parse_hold_marker(decision.comment_body)
    if decision.action in ("hold-red", "reassert-hold"):
        if marker is None:
            raise QueueSnapshotError(f"{decision.action} decision lacks a chitra hold marker")
        record_hold(root, marker)
    elif decision.action == "release-hold":
        if marker is None:
            raise QueueSnapshotError("release-hold decision lacks a chitra hold marker")
        try:
            close_hold(root, marker.pr)
        except QueueHoldNotFoundError:
            logger.info("queue_hold_close_already_absent", pr=marker.pr)
    return log_action(root, decision, marker=marker, now=now)


def _read_json_source(source: str) -> object:
    """Read one JSON document from a path or standard input marker ``-``."""
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return json.loads(raw)


def _snapshot_and_policy(source: str) -> tuple[QueueHeadSnapshot, QueuePolicy]:
    """Read either a bare snapshot or a ``{snapshot, policy}`` CLI envelope."""
    raw = _read_json_source(source)
    values = _object(raw, name="queue decide input")
    if "snapshot" in values:
        _exact_fields(values, name="queue decide input", fields=("snapshot", "policy"))
        return QueueHeadSnapshot.from_dict(values["snapshot"]), QueuePolicy.from_dict(values["policy"])
    if "policy" in values:
        snapshot_values = dict(values)
        policy_raw = snapshot_values.pop("policy")
        return QueueHeadSnapshot.from_dict(snapshot_values), QueuePolicy.from_dict(policy_raw)
    return QueueHeadSnapshot.from_dict(values), QueuePolicy()


def _parse_required_option(value: str) -> RequiredCheck:
    """Parse one CLI ``NAME:APP_ID`` selector without guessing app identity."""
    name, separator, raw_app_id = value.rpartition(":")
    if not separator:
        raise QueueSnapshotError("--required must be NAME:APP_ID")
    try:
        app_id = int(raw_app_id)
    except ValueError as exc:
        raise QueueSnapshotError("--required APP_ID must be an integer") from exc
    return RequiredCheck(name=_string(name, field="required check name"), app_id=_integer(app_id, field="required check app_id", minimum=1))


def _read_log(root: Path, *, tail: int) -> list[str]:
    """Read the final N raw append-only log lines without changing the log."""
    if tail < 0:
        raise QueueSnapshotError("--tail must be non-negative")
    path = queue_hygiene_log_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return lines[-tail:] if tail else []


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``chitra-queue`` command-line interface."""
    parser = argparse.ArgumentParser(
        prog="chitra-queue", description="Evaluate pure queue hygiene and persist only chitra-owned local hold bookkeeping."
    )
    parser.add_argument("--root", type=Path, default=state_dir())
    commands = parser.add_subparsers(dest="command", required=True)

    def add_root(command: argparse.ArgumentParser) -> None:
        command.add_argument("--root", type=Path, default=argparse.SUPPRESS)

    decide_command = commands.add_parser("decide", help="Print a pure queue decision from a snapshot JSON document.")
    add_root(decide_command)
    decide_command.add_argument("--snapshot", required=True, help="Snapshot JSON path, or - for stdin.")
    decide_command.add_argument("--required", action="append", default=[], help="Required check selector NAME:APP_ID (repeatable).")
    decide_command.add_argument("--red-threshold-minutes", type=int, default=None)

    record_command = commands.add_parser("record", help="Persist a decision's local hold state and append the hygiene log.")
    add_root(record_command)
    record_command.add_argument("--decision", required=True, help="Decision JSON path, or - for stdin.")

    holds_command = commands.add_parser("holds", help="List active chitra-owned queue holds.")
    add_root(holds_command)
    holds_command.add_argument("--json", action="store_true")

    log_command = commands.add_parser("log", help="Print tail entries from the append-only queue hygiene log.")
    add_root(log_command)
    log_command.add_argument("--tail", type=int, default=20)

    dequeue_command = commands.add_parser("dequeue-hold", help="Explicitly close one local chitra hold after capability authorization.")
    add_root(dequeue_command)
    dequeue_command.add_argument("--pr", type=int, required=True)
    dequeue_command.add_argument("--reason", required=True)

    requeue_command = commands.add_parser("requeue", help="Record an explicitly authorized local requeue request for one active hold.")
    add_root(requeue_command)
    requeue_command.add_argument("--pr", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the queue CLI and return a shell-friendly exit status."""
    args = build_arg_parser().parse_args(argv)
    try:
        if args.command == "decide":
            try:
                snapshot, policy = _snapshot_and_policy(args.snapshot)
                if args.required:
                    policy = QueuePolicy(
                        required=tuple(_parse_required_option(value) for value in args.required),
                        red_threshold_minutes=(
                            policy.red_threshold_minutes if args.red_threshold_minutes is None else args.red_threshold_minutes
                        ),
                    )
                elif args.red_threshold_minutes is not None:
                    policy = QueuePolicy(required=policy.required, red_threshold_minutes=args.red_threshold_minutes)
                decision = decide(snapshot, policy, datetime.now(UTC))
            except (OSError, json.JSONDecodeError, QueueSnapshotError, ValueError) as exc:
                logger.warning("queue_snapshot_unreadable", error=str(exc))
                decision = decide(None, QueuePolicy(), datetime.now(UTC))
            print(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
        elif args.command == "record":
            raw = _read_json_source(args.decision)
            values = _object(raw, name="queue decision input")
            decision = QueueDecision.from_dict(values.get("decision", values))
            print(json.dumps(record_decision(args.root, decision), indent=2, sort_keys=True))
        elif args.command == "holds":
            holds = active_holds(args.root)
            if args.json:
                print(json.dumps([marker.to_dict() for marker in holds], indent=2, sort_keys=True))
            else:
                for marker in holds:
                    print(f"{marker.pr}\t{marker.head_sha}\t{marker.red_since}\t{marker.reassert_count}")
        elif args.command == "log":
            for line in _read_log(args.root, tail=args.tail):
                print(line)
        elif args.command == "dequeue-hold":
            require_enabled("queue-management", args.root)
            marker = close_hold(args.root, args.pr)
            decision = QueueDecision(
                action="none",
                reason=f"operator dequeued local hold: {args.reason}",
                comment_body=render_hold_comment(marker),
                labels_to_add=(),
                labels_to_remove=(),
            )
            print(json.dumps(log_action(args.root, decision, marker=marker), indent=2, sort_keys=True))
        else:
            require_enabled("queue-management", args.root)
            requeue_marker = next((item for item in active_holds(args.root) if item.pr == args.pr), None)
            if requeue_marker is None:
                raise QueueHoldNotFoundError(args.pr)
            record_hold(args.root, requeue_marker)
            decision = QueueDecision(
                action="none",
                reason="operator requeue request recorded; external control plane remains responsible for labels",
                comment_body=render_hold_comment(requeue_marker),
                labels_to_add=(),
                labels_to_remove=(),
            )
            print(json.dumps(log_action(args.root, decision, marker=requeue_marker), indent=2, sort_keys=True))
    except (
        QueueSnapshotError,
        QueueInvariantError,
        QueueHoldNotFoundError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        PermissionError,
    ) as exc:
        print(f"chitra-queue: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
