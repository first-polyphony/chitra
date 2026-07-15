"""Deterministic per-family effectiveness rollups over Chitra state.

The decided family default is ``task_type`` from the dispatch ledger. Blank or
missing task types are grouped as ``"untyped"``. Dispatches are the anchor;
review, attestation, defect, and hold records are joined by ``order_id`` when
available, otherwise by ``session_ref`` and the interval between dispatches.

``cycle_time_seconds`` is the arithmetic mean of each completed dispatch's
order-created to terminal-result duration, including recorded hold seconds.
Dispatches without both timestamps do not contribute a cycle-time observation.

# adapter joins cost/quota + renders one board section

There are no model calls or network operations in this module.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

import structlog
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

from chitra.merge_queue import queue_hygiene_log_path
from chitra.rate_limit_state import load_transactions
from chitra.recovery import load_recovery_records
from chitra.state_paths import default_attestation_ledger_path, default_ledger_path, state_dir

logger = structlog.get_logger(__name__)

UNTYPED_FAMILY = "untyped"
_COMPLETION_REVIEWS_FILENAME = "completion_reviews.jsonl"
_DEFECT_ACTION_RE = re.compile(r"(?:^|[^a-z])(?:revert(?:ed)?|rollback|rolled[ _-]?back|superseded)(?:$|[^a-z])")


class OutcomesDataError(ValueError):
    """Raised when a present state input is malformed or internally inconsistent."""


class RatioCounts(BaseModel):
    """A nullable ratio and the exact numerator/denominator behind it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ratio: float | None = Field(default=None, ge=0, le=1)
    accepted_count: int = Field(ge=0)
    completion_claim_review_count: int = Field(ge=0)


class FamilyOutcome(BaseModel):
    """Effectiveness metrics for one ``task_type`` family."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: str = Field(min_length=1)
    dispatch_count: int = Field(ge=0)
    task_success_after_validation: RatioCounts
    human_intervention_rate: float = Field(ge=0, le=1)
    human_intervention_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    retries_per_dispatch: float = Field(ge=0)
    escaped_defects: int = Field(ge=0)
    cycle_time_seconds: float | None = Field(default=None, ge=0)


class OutcomeTotals(BaseModel):
    """Rollup-wide metrics calculated from raw observations, not family means."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dispatch_count: int = Field(ge=0)
    task_success_after_validation: RatioCounts
    human_intervention_rate: float = Field(ge=0, le=1)
    human_intervention_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    retries_per_dispatch: float = Field(ge=0)
    escaped_defects: int = Field(ge=0)
    cycle_time_seconds: float | None = Field(default=None, ge=0)


class OutcomesRollup(BaseModel):
    """Typed result returned by :func:`compute_outcomes`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    families: list[FamilyOutcome]
    totals: OutcomeTotals


class _DispatchRecord(BaseModel):
    """Fields from either the current delivery ledger or an enriched ledger row."""

    model_config = ConfigDict(extra="ignore")

    order_id: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    task_type: str | None = None
    status: str | None = None
    created_at: str | None = Field(
        default=None,
        validation_alias=AliasChoices("created_at", "order_created_at", "ordered_at", "queued_at"),
    )
    terminal_at: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "terminal_at",
            "terminal_result_at",
            "completed_at",
            "result_at",
            "finished_at",
            "sent_at",
        ),
    )


class _CompletionReviewRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_ref: str = Field(min_length=1)
    condition: Literal["completion_claim", "turn_end_without_completion_claim"]
    review_verdict: Literal["accept", "reject", "unavailable"]
    recorded_at: str = Field(validation_alias=AliasChoices("recorded_at", "logged_at", "reviewed_at"))


class _DecisionRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    operator_confirmation_required: bool
    autonomy: Literal["autonomous", "operator_required"]


class _AttestationRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: str | None = None
    session_ref: str = Field(min_length=1)
    attestation: _DecisionRecord | None = None
    operator_confirmation_required: bool | None = None
    autonomy: Literal["autonomous", "operator_required"] | None = None
    logged_at: str = Field(validation_alias=AliasChoices("logged_at", "recorded_at", "created_at"))

    @model_validator(mode="after")
    def has_decision_fields(self) -> Self:
        if self.attestation is None and self.operator_confirmation_required is None and self.autonomy is None:
            raise ValueError("attestation row lacks operator-confirmation and autonomy fields")
        return self

    def requires_operator(self) -> bool:
        decision = self.attestation
        required = self.operator_confirmation_required if decision is None else decision.operator_confirmation_required
        autonomy = self.autonomy if decision is None else decision.autonomy
        return required is True or autonomy == "operator_required"


class _QueueHygieneRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = Field(min_length=1)
    session_ref: str | None = None
    order_id: str | None = None
    logged_at: str = Field(validation_alias=AliasChoices("logged_at", "recorded_at", "created_at"))


@dataclass(frozen=True, slots=True)
class _DispatchFact:
    order_id: str
    session_ref: str
    family: str
    created_at: datetime | None
    terminal_at: datetime | None
    anchor_at: datetime | None
    sequence: int


@dataclass(frozen=True, slots=True)
class _Hold:
    session_ref: str
    started_at: datetime
    ended_at: datetime


@dataclass(slots=True)
class _FamilyAccumulator:
    dispatches: list[_DispatchFact]
    accepted_reviews: int = 0
    completion_claim_reviews: int = 0
    interventions: set[str] | None = None
    escaped_defects: int = 0

    def __post_init__(self) -> None:
        if self.interventions is None:
            self.interventions = set()


def _state_path(root: Path | None, default_path: Path) -> Path:
    return default_path if root is None else root / default_path.name


def _completion_reviews_path(root: Path | None) -> Path:
    return (state_dir() if root is None else root) / _COMPLETION_REVIEWS_FILENAME


def _load_jsonl[RecordT: BaseModel](path: Path, record_type: type[RecordT]) -> list[RecordT]:
    """Load a JSONL file strictly; only absence and blank content are cold starts."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise OutcomesDataError(f"cannot read outcomes input {path}: {exc}") from exc

    records: list[RecordT] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(record_type.model_validate_json(line))
        except (ValidationError, ValueError) as exc:
            raise OutcomesDataError(f"invalid outcomes input {path}:{line_number}: {exc}") from exc
    return records


def _timestamp(value: str | None, *, source: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomesDataError(f"{source} must be an ISO8601 datetime") from exc
    if parsed.tzinfo is None:
        raise OutcomesDataError(f"{source} must include a timezone")
    return parsed.astimezone(UTC)


def _family(task_type: str | None) -> str:
    if task_type is None or not task_type.strip():
        return UNTYPED_FAMILY
    return task_type.strip()


def _load_dispatches(root: Path | None) -> list[_DispatchFact]:
    path = _state_path(root, default_ledger_path())
    records = _load_jsonl(path, _DispatchRecord)
    dispatches: list[_DispatchFact] = []
    seen_order_ids: set[str] = set()
    for sequence, record in enumerate(records):
        if record.order_id in seen_order_ids:
            raise OutcomesDataError(f"duplicate order_id {record.order_id!r} in {path}")
        seen_order_ids.add(record.order_id)
        created_at = _timestamp(record.created_at, source=f"{path} order {record.order_id} created_at")
        terminal_at = _timestamp(record.terminal_at, source=f"{path} order {record.order_id} terminal_at")
        if created_at is not None and terminal_at is not None and terminal_at < created_at:
            raise OutcomesDataError(f"{path} order {record.order_id} terminal_at precedes created_at")
        dispatches.append(
            _DispatchFact(
                order_id=record.order_id,
                session_ref=record.session_ref,
                family=_family(record.task_type),
                created_at=created_at,
                terminal_at=terminal_at,
                anchor_at=created_at or terminal_at,
                sequence=sequence,
            )
        )
    return dispatches


def _dispatches_by_session(dispatches: Sequence[_DispatchFact]) -> dict[str, list[_DispatchFact]]:
    grouped: dict[str, list[_DispatchFact]] = {}
    for dispatch in dispatches:
        grouped.setdefault(dispatch.session_ref, []).append(dispatch)
    for session_dispatches in grouped.values():
        session_dispatches.sort(
            key=lambda item: (
                item.anchor_at is None,
                item.anchor_at or datetime.max.replace(tzinfo=UTC),
                item.sequence,
            )
        )
    return grouped


def _assign_event(
    *,
    order_id: str | None,
    session_ref: str | None,
    occurred_at: datetime | None,
    by_order: dict[str, _DispatchFact],
    by_session: dict[str, list[_DispatchFact]],
) -> _DispatchFact | None:
    if order_id is not None:
        dispatch = by_order.get(order_id)
        if dispatch is not None:
            if session_ref is not None and session_ref != dispatch.session_ref:
                raise OutcomesDataError(f"event order {order_id!r} has session_ref inconsistent with the dispatch ledger")
            return dispatch
    if session_ref is None:
        return None
    candidates = by_session.get(session_ref, [])
    if not candidates:
        return None
    if occurred_at is not None:
        preceding = [item for item in candidates if item.anchor_at is not None and item.anchor_at <= occurred_at]
        if preceding:
            return max(preceding, key=lambda item: (item.anchor_at or datetime.min.replace(tzinfo=UTC), item.sequence))
    if len({item.family for item in candidates}) == 1:
        return max(candidates, key=lambda item: item.sequence)
    return None


def _load_holds(root: Path | None, *, now: datetime) -> list[_Hold]:
    """Load completed recovery holds plus non-duplicated active transaction holds."""
    holds: list[_Hold] = []
    try:
        recovery_records = load_recovery_records(root)
    except (OSError, ValueError) as exc:
        raise OutcomesDataError(f"invalid pause recovery state: {exc}") from exc
    for record in recovery_records:
        started_at = _timestamp(record.paused_at, source=f"pause recovery {record.pause_id} paused_at")
        resumed_at = _timestamp(record.resume_at, source=f"pause recovery {record.pause_id} resume_at") if record.resume_at else now
        if started_at is None or resumed_at is None:
            raise OutcomesDataError(f"pause recovery {record.pause_id} lacks a hold boundary")
        ended_at = min(resumed_at, now)
        if ended_at < started_at:
            raise OutcomesDataError(f"pause recovery {record.pause_id} ends before it starts")
        holds.append(_Hold(record.session_ref, started_at, ended_at))

    try:
        transactions = load_transactions(root)
    except (OSError, ValueError) as exc:
        raise OutcomesDataError(f"invalid rate-limit transaction state: {exc}") from exc
    for transaction in transactions:
        if transaction.phase not in ("held", "resume_requested", "resume_sent"):
            continue
        started_at = _timestamp(transaction.created_at, source=f"transaction {transaction.session_ref} created_at")
        if started_at is None:
            raise OutcomesDataError(f"held transaction {transaction.session_ref} lacks created_at")
        resume_at = (
            _timestamp(transaction.resume_at, source=f"transaction {transaction.session_ref} resume_at") if transaction.resume_at else now
        )
        if resume_at is None:
            raise OutcomesDataError(f"held transaction {transaction.session_ref} lacks a hold boundary")
        ended_at = min(resume_at, now)
        if ended_at < started_at:
            raise OutcomesDataError(f"held transaction {transaction.session_ref} ends before it starts")
        candidate = _Hold(transaction.session_ref, started_at, ended_at)
        if not any(_holds_overlap(candidate, existing) for existing in holds):
            holds.append(candidate)
    return holds


def _holds_overlap(left: _Hold, right: _Hold) -> bool:
    return left.session_ref == right.session_ref and left.started_at < right.ended_at and right.started_at < left.ended_at


def _next_anchor(
    dispatch: _DispatchFact,
    by_session: dict[str, list[_DispatchFact]],
    *,
    now: datetime,
) -> datetime:
    if dispatch.anchor_at is None:
        return now
    later = [
        item.anchor_at
        for item in by_session.get(dispatch.session_ref, [])
        if item.anchor_at is not None
        and (item.anchor_at > dispatch.anchor_at or (item.anchor_at == dispatch.anchor_at and item.sequence > dispatch.sequence))
    ]
    return min(later) if later else now


def _hold_intersects_dispatch(
    hold: _Hold,
    dispatch: _DispatchFact,
    by_session: dict[str, list[_DispatchFact]],
    *,
    now: datetime,
) -> bool:
    if hold.session_ref != dispatch.session_ref:
        return False
    start = dispatch.created_at or dispatch.anchor_at
    if start is None:
        return len(by_session.get(dispatch.session_ref, [])) == 1
    end = _next_anchor(dispatch, by_session, now=now)
    if end < start:
        end = start
    return hold.started_at < end and start < hold.ended_at


def _hold_seconds(dispatch: _DispatchFact, holds: Sequence[_Hold]) -> float:
    """Return the union of hold intervals within one completed dispatch span."""
    if dispatch.created_at is None or dispatch.terminal_at is None:
        return 0.0
    intervals = sorted(
        (
            max(dispatch.created_at, hold.started_at),
            min(dispatch.terminal_at, hold.ended_at),
        )
        for hold in holds
        if hold.session_ref == dispatch.session_ref and hold.started_at < dispatch.terminal_at and dispatch.created_at < hold.ended_at
    )
    if not intervals:
        return 0.0
    merged_seconds = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            merged_seconds += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    return merged_seconds + (current_end - current_start).total_seconds()


def _cycle_seconds(dispatch: _DispatchFact, holds: Sequence[_Hold]) -> float | None:
    if dispatch.created_at is None or dispatch.terminal_at is None:
        return None
    return (dispatch.terminal_at - dispatch.created_at).total_seconds() + _hold_seconds(dispatch, holds)


def _ratio_counts(accepted: int, reviewed: int) -> RatioCounts:
    return RatioCounts(
        ratio=accepted / reviewed if reviewed else None,
        accepted_count=accepted,
        completion_claim_review_count=reviewed,
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _retry_count(dispatches: Sequence[_DispatchFact]) -> int:
    counts = Counter((dispatch.session_ref, dispatch.family) for dispatch in dispatches)
    return sum(count - 1 for count in counts.values())


def _build_family_outcome(
    family: str,
    accumulator: _FamilyAccumulator,
    *,
    holds: Sequence[_Hold],
) -> FamilyOutcome:
    dispatch_count = len(accumulator.dispatches)
    retry_count = _retry_count(accumulator.dispatches)
    interventions = accumulator.interventions or set()
    cycle_times = [value for dispatch in accumulator.dispatches if (value := _cycle_seconds(dispatch, holds)) is not None]
    return FamilyOutcome(
        family=family,
        dispatch_count=dispatch_count,
        task_success_after_validation=_ratio_counts(accumulator.accepted_reviews, accumulator.completion_claim_reviews),
        human_intervention_rate=len(interventions) / dispatch_count if dispatch_count else 0.0,
        human_intervention_count=len(interventions),
        retry_count=retry_count,
        retries_per_dispatch=retry_count / dispatch_count if dispatch_count else 0.0,
        escaped_defects=accumulator.escaped_defects,
        cycle_time_seconds=_mean(cycle_times),
    )


def compute_outcomes(root: Path | None = None, *, now: datetime | None = None) -> OutcomesRollup:
    """Load local Chitra state and compute stable per-``task_type`` metrics."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(UTC)

    dispatches = _load_dispatches(root)
    by_order = {dispatch.order_id: dispatch for dispatch in dispatches}
    by_session = _dispatches_by_session(dispatches)
    accumulators = {
        family: _FamilyAccumulator([dispatch for dispatch in dispatches if dispatch.family == family])
        for family in sorted({dispatch.family for dispatch in dispatches})
    }

    reviews_path = _completion_reviews_path(root)
    for review in _load_jsonl(reviews_path, _CompletionReviewRecord):
        if review.condition != "completion_claim":
            continue
        occurred_at = _timestamp(review.recorded_at, source=f"{reviews_path} review recorded_at")
        dispatch = _assign_event(
            order_id=None,
            session_ref=review.session_ref,
            occurred_at=occurred_at,
            by_order=by_order,
            by_session=by_session,
        )
        if dispatch is None:
            continue
        accumulator = accumulators[dispatch.family]
        accumulator.completion_claim_reviews += 1
        accumulator.accepted_reviews += review.review_verdict == "accept"

    attestations_path = _state_path(root, default_attestation_ledger_path())
    for attestation in _load_jsonl(attestations_path, _AttestationRecord):
        if not attestation.requires_operator():
            continue
        occurred_at = _timestamp(attestation.logged_at, source=f"{attestations_path} attestation logged_at")
        dispatch = _assign_event(
            order_id=attestation.order_id,
            session_ref=attestation.session_ref,
            occurred_at=occurred_at,
            by_order=by_order,
            by_session=by_session,
        )
        if dispatch is not None:
            interventions = accumulators[dispatch.family].interventions
            if interventions is not None:
                interventions.add(dispatch.order_id)

    hygiene_path = queue_hygiene_log_path(root)
    for event in _load_jsonl(hygiene_path, _QueueHygieneRecord):
        if _DEFECT_ACTION_RE.search(event.action.lower()) is None:
            continue
        occurred_at = _timestamp(event.logged_at, source=f"{hygiene_path} event logged_at")
        dispatch = _assign_event(
            order_id=event.order_id,
            session_ref=event.session_ref,
            occurred_at=occurred_at,
            by_order=by_order,
            by_session=by_session,
        )
        if dispatch is not None:
            accumulators[dispatch.family].escaped_defects += 1

    holds = _load_holds(root, now=current)
    for hold in holds:
        for dispatch in dispatches:
            if _hold_intersects_dispatch(hold, dispatch, by_session, now=current):
                interventions = accumulators[dispatch.family].interventions
                if interventions is not None:
                    interventions.add(dispatch.order_id)

    families = [_build_family_outcome(family, accumulators[family], holds=holds) for family in sorted(accumulators)]
    accepted_total = sum(item.task_success_after_validation.accepted_count for item in families)
    reviewed_total = sum(item.task_success_after_validation.completion_claim_review_count for item in families)
    intervention_total = sum(item.human_intervention_count for item in families)
    dispatch_total = len(dispatches)
    retry_total = _retry_count(dispatches)
    all_cycle_times = [value for dispatch in dispatches if (value := _cycle_seconds(dispatch, holds)) is not None]
    totals = OutcomeTotals(
        dispatch_count=dispatch_total,
        task_success_after_validation=_ratio_counts(accepted_total, reviewed_total),
        human_intervention_rate=intervention_total / dispatch_total if dispatch_total else 0.0,
        human_intervention_count=intervention_total,
        retry_count=retry_total,
        retries_per_dispatch=retry_total / dispatch_total if dispatch_total else 0.0,
        escaped_defects=sum(item.escaped_defects for item in families),
        cycle_time_seconds=_mean(all_cycle_times),
    )
    return OutcomesRollup(generated_at=current, families=families, totals=totals)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the current outcomes rollup as indented, key-sorted JSON."""
    parser = argparse.ArgumentParser(description="Compute deterministic Chitra outcomes by task_type.")
    parser.add_argument("--root", type=Path, help="State directory (defaults to CHITRA_STATE_DIR or /var/lib/chitra).")
    args = parser.parse_args(argv)
    rollup = compute_outcomes(args.root)
    print(json.dumps(rollup.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
