"""Deterministic close-time inventory checks for operator-stated goals.

This module only reads a lane's existing ``done_when`` and caller-supplied
delivery facts.  It never generates, expands, or rewrites done conditions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from chitra.completion_gate import CompletionEvidence

AGGREGATE_DONE_WHEN_TOKENS: tuple[str, ...] = (
    "representative",
    "some",
    "several",
    "various",
    "a number of",
)
BARE_DELIVERABLE_PLURALS: frozenset[str] = frozenset({"clients", "consumers", "integrations"})
RECLASSIFICATION_PHRASES: tuple[str, ...] = (
    "follow-on",
    "follow on",
    "out of scope",
    "out-of-scope",
    "deferred",
    "future work",
)
DONE_WHEN_OPERATOR_FLAG = "This session's done conditions are missing or vague — flag for the operator."

_COUNT_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_COUNT_TOKEN = rf"(?:\d+|{'|'.join(_COUNT_WORDS)})"
_EXPLICIT_COUNT_RE = re.compile(rf"\b(?:both|{_COUNT_TOKEN})\b", re.IGNORECASE)
_COUNTED_DELIVERABLE_RE = re.compile(
    rf"\b(?P<count>{_COUNT_TOKEN})\s+(?:[a-z][a-z0-9_-]*\s+){{0,3}}(?P<noun>{'|'.join(sorted(BARE_DELIVERABLE_PLURALS))})\b",
    re.IGNORECASE,
)
_ENUMERATOR_RE = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_ITEM_SEPARATOR_RE = re.compile(r"\s*(?:\n+|;+|,+|\band\b)\s*", re.IGNORECASE)
_CLAUSE_SEPARATOR_RE = re.compile(r"[\n.;]+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+#./-]*", re.IGNORECASE)
_IDENTITY_NOISE = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "both",
        "by",
        "complete",
        "completed",
        "defer",
        "deferred",
        "descoped",
        "done",
        "for",
        "follow",
        "following",
        "future",
        "in",
        "is",
        "item",
        "items",
        "live",
        "of",
        "on",
        "operator",
        "out",
        "pass",
        "passed",
        "passes",
        "required",
        "scope",
        "the",
        "to",
        "updated",
        "validation",
        "was",
        "were",
        "work",
    }
)
_GENERIC_SINGLE_TOKENS = BARE_DELIVERABLE_PLURALS | frozenset(
    {"client", "consumer", "integration", "check", "checks", "documentation", "docs", "test", "tests"}
)


@dataclass(frozen=True, slots=True)
class DoneWhenFlag:
    """A missing/vague finding for operator surfacing, never a replacement."""

    code: Literal["missing_done_when", "vague_done_when"]
    message: str
    matches: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequiredItem:
    """One literal condition fragment and any explicit quantity it states."""

    text: str
    quantity: int = 1
    counted_noun: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryGap:
    required_item: RequiredItem
    missing_count: int


@dataclass(frozen=True, slots=True)
class CloseGateVerdict:
    verdict: Literal["PASS", "FAIL"]
    required_items: tuple[RequiredItem, ...]
    delivered_items: tuple[str, ...]
    missing: tuple[InventoryGap, ...]
    reclassified: tuple[RequiredItem, ...]
    acknowledged: tuple[RequiredItem, ...]
    recorded_descopes: tuple[RequiredItem, ...]
    summary: str


class CloseGateError(ValueError):
    """Raised before goal deletion when the close inventory does not balance."""

    def __init__(self, verdict: CloseGateVerdict) -> None:
        self.verdict = verdict
        super().__init__(verdict.summary)


def lint_done_when(done_when: str) -> DoneWhenFlag | None:
    """Return a surfacing-only finding without changing ``done_when``."""
    if not done_when.strip():
        return DoneWhenFlag(code="missing_done_when", message=DONE_WHEN_OPERATOR_FLAG, matches=())

    lowered = done_when.casefold()
    has_any_explicit_count = _EXPLICIT_COUNT_RE.search(lowered) is not None
    matches = [
        token
        for token in AGGREGATE_DONE_WHEN_TOKENS
        if not has_any_explicit_count and re.search(rf"\b{re.escape(token)}\b", lowered)
    ]
    for noun in sorted(BARE_DELIVERABLE_PLURALS):
        if not re.search(rf"\b{re.escape(noun)}\b", lowered):
            continue
        counted_noun = re.compile(
            rf"\b(?:both|{_COUNT_TOKEN})(?:\s+[a-z][a-z0-9_-]*){{0,3}}\s+{re.escape(noun)}\b",
            re.IGNORECASE,
        )
        if counted_noun.search(lowered) is None:
            matches.append(noun)
    if not matches:
        return None
    return DoneWhenFlag(code="vague_done_when", message=DONE_WHEN_OPERATOR_FLAG, matches=tuple(matches))


def parse_required_items(done_when: str) -> tuple[RequiredItem, ...]:
    """Split explicit enumerators in an existing done condition conservatively."""
    enumerator_stripped = _ENUMERATOR_RE.sub("\n", done_when.strip())
    fragments = [fragment.strip(" \t\r\n:.-") for fragment in _ITEM_SEPARATOR_RE.split(enumerator_stripped)]
    items: list[RequiredItem] = []
    for fragment in fragments:
        if not fragment:
            continue
        if fragment.casefold().startswith("both "):
            fragment = fragment[5:].strip()
        counted = _COUNTED_DELIVERABLE_RE.search(fragment)
        if counted is None:
            items.append(RequiredItem(text=fragment))
            continue
        count_token = counted.group("count").casefold()
        quantity = int(count_token) if count_token.isdigit() else _COUNT_WORDS[count_token]
        plural_noun = counted.group("noun").casefold()
        items.append(RequiredItem(text=fragment, quantity=quantity, counted_noun=plural_noun.removesuffix("s")))
    return tuple(items)


def delivered_items_from_evidence(evidence: Sequence[CompletionEvidence]) -> tuple[str, ...]:
    """Read only explicit evidence-to-item bindings; never infer from citations."""
    return _deduplicate(item.todo_item for item in evidence if item.todo_item is not None)


def evaluate_close_inventory(
    done_when: str,
    delivered_items: Sequence[str],
    *,
    evidence: Sequence[CompletionEvidence] = (),
    close_notes: Sequence[str] = (),
    operator_acknowledged_items: Sequence[str] = (),
    goal_version: int = 1,
    goal_history: Sequence[Mapping[str, str]] = (),
) -> CloseGateVerdict:
    """Diff caller-stated delivery facts against operator-stated conditions."""
    required = parse_required_items(done_when)
    delivered = _deduplicate((*delivered_items, *delivered_items_from_evidence(evidence)))
    acknowledgements = _deduplicate(operator_acknowledged_items)
    acknowledged = tuple(item for item in required if any(_item_matches(item, ack) for ack in acknowledgements))
    recorded_descopes = _recorded_descopes(required, goal_version=goal_version, goal_history=goal_history)

    unused_deliveries = set(range(len(delivered)))
    missing: list[InventoryGap] = []
    for item in required:
        if item in acknowledged:
            continue
        matches = [index for index in sorted(unused_deliveries) if _item_matches(item, delivered[index])]
        used = matches[: item.quantity]
        unused_deliveries.difference_update(used)
        if len(used) < item.quantity:
            missing.append(InventoryGap(required_item=item, missing_count=item.quantity - len(used)))

    reclassified = tuple(
        item
        for item in required
        if item not in acknowledged and any(_clause_reclassifies(item, clause) for clause in _close_clauses(close_notes))
    )
    if not required:
        summary = "FAIL: close-time inventory diff cannot verify a lane with missing done conditions."
        return CloseGateVerdict(
            verdict="FAIL",
            required_items=required,
            delivered_items=delivered,
            missing=(),
            reclassified=(),
            acknowledged=acknowledged,
            recorded_descopes=recorded_descopes,
            summary=summary,
        )
    if missing or reclassified:
        details: list[str] = []
        if missing:
            details.append(
                "undelivered stated-required items: "
                + ", ".join(f"{gap.required_item.text!r} (missing {gap.missing_count})" for gap in missing)
            )
        if reclassified:
            details.append(
                "F8 close tell: stated-required item reclassified as follow-on/out of scope/deferred/future work without "
                "an operator-recorded descope or explicit acknowledgement: "
                + ", ".join(repr(item.text) for item in reclassified)
            )
        return CloseGateVerdict(
            verdict="FAIL",
            required_items=required,
            delivered_items=delivered,
            missing=tuple(missing),
            reclassified=reclassified,
            acknowledged=acknowledged,
            recorded_descopes=recorded_descopes,
            summary="FAIL: " + "; ".join(details),
        )
    return CloseGateVerdict(
        verdict="PASS",
        required_items=required,
        delivered_items=delivered,
        missing=(),
        reclassified=(),
        acknowledged=acknowledged,
        recorded_descopes=recorded_descopes,
        summary="PASS: every currently stated required item is delivered, explicitly acknowledged, or operator-descoped.",
    )


def require_close_inventory(
    done_when: str,
    delivered_items: Sequence[str],
    *,
    evidence: Sequence[CompletionEvidence] = (),
    close_notes: Sequence[str] = (),
    operator_acknowledged_items: Sequence[str] = (),
    goal_version: int = 1,
    goal_history: Sequence[Mapping[str, str]] = (),
) -> CloseGateVerdict:
    """Return a passing verdict or raise a typed blocking error."""
    verdict = evaluate_close_inventory(
        done_when,
        delivered_items,
        evidence=evidence,
        close_notes=close_notes,
        operator_acknowledged_items=operator_acknowledged_items,
        goal_version=goal_version,
        goal_history=goal_history,
    )
    if verdict.verdict == "FAIL":
        raise CloseGateError(verdict)
    return verdict


def _deduplicate(items: Iterable[str | None]) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None or not item.strip():
            continue
        stripped = item.strip()
        key = stripped.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(stripped)
    return tuple(unique)


def _identity_tokens(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in (match.group(0).casefold().strip("-./") for match in _WORD_RE.finditer(text))
        if token and token not in _IDENTITY_NOISE and not token.isdigit()
    )


def _token_sets_match(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    if not smaller.issubset(larger):
        return False
    if len(smaller) >= 2:
        return True
    token = next(iter(smaller))
    return token not in _GENERIC_SINGLE_TOKENS and len(token) >= 3


def _item_matches(required: RequiredItem, candidate: str) -> bool:
    candidate_tokens = _identity_tokens(candidate)
    if required.counted_noun is not None:
        return required.counted_noun in candidate_tokens or f"{required.counted_noun}s" in candidate_tokens
    return _token_sets_match(_identity_tokens(required.text), candidate_tokens)


def _close_clauses(close_notes: Sequence[str]) -> tuple[str, ...]:
    return tuple(clause.strip() for note in close_notes for clause in _CLAUSE_SEPARATOR_RE.split(note) if clause.strip())


def _clause_reclassifies(required: RequiredItem, clause: str) -> bool:
    lowered = clause.casefold()
    marker = next((phrase for phrase in RECLASSIFICATION_PHRASES if phrase in lowered), None)
    if marker is None:
        return False
    negated_marker = re.compile(rf"\b(?:no|not|without)\b(?:\s+\w+){{0,3}}\s+{re.escape(marker)}\b")
    if negated_marker.search(lowered):
        return False
    return _item_matches(required, clause)


def _recorded_descopes(
    current: Sequence[RequiredItem],
    *,
    goal_version: int,
    goal_history: Sequence[Mapping[str, str]],
) -> tuple[RequiredItem, ...]:
    if goal_version <= 1:
        return ()
    descoped: list[RequiredItem] = []
    for entry in goal_history:
        previous_done_when = entry.get("done_when")
        if previous_done_when is None:
            continue
        for previous_item in parse_required_items(previous_done_when):
            if any(_item_matches(item, previous_item.text) for item in current):
                continue
            if previous_item not in descoped:
                descoped.append(previous_item)
    return tuple(descoped)
