"""Typed dispatch contracts shared by queue producers and transports."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from chitra.completion_gate import CompletionEvidence, TodoItem
from chitra.reasoning import DecisionAttestation


class DispatchStatus(enum.StrEnum):
    """Outcome of a dispatch attempt."""

    SENT = "sent"
    BLOCKED = "blocked"
    FAILED = "failed"
    # A completion-claim audit (chitra.completion_gate.evaluate_completion_claim)
    # found a gap (todo residue, deferral language, or missing evidence). The
    # order was never delivered -- a disputed completion claim must never
    # silently pass through as "sent". See dispatchd.process_one_order.
    COMPLETION_DISPUTE = "completion_dispute"
    # The order's session is rate-limit- or load-shed-held: parked in the durable
    # deferred/ subqueue (no pane I/O, no result file persisted) rather than
    # discarded. dispatchd.run_once/requeue_deferred_for_session return it
    # to orders/ FIFO once the hold clears, so it is delivered exactly once,
    # never silently dropped. This status is for in-process visibility only
    # (a caller inspecting run_once's return value) -- it is never written
    # to results/, since a persisted terminal result would block the later
    # real delivery's own idempotency check. See
    # docs/SOL-ADVERSARIAL-REVIEW finding #1.
    DEFERRED = "deferred"


class DispatchOrder(BaseModel):
    """A dispatch order consumed by ``dispatchd``.

    ``session_ref`` uses the ``host:session:pane`` convention from the
    source. ``nudge`` is the verbatim text to inject. ``order_id`` is the
    caller-supplied unique id used for result-file naming. ``tag`` marks the
    message's authenticity class in the delivery ledger — ``"[C]"`` (chitra
    relay) is the default; a caller relaying verbatim operator-typed text
    with no relay in between may use a different tag, but the ledger records
    whatever tag is asserted so it can be audited later. ``routing_hint`` is
    an opaque, caller-supplied string recording a routing/model-preference
    decision already made upstream — chitra never reads, validates, or acts
    on its contents; it is only carried through to ``DispatchResult`` and
    the ledger for audit purposes, exactly like ``tag``. ``task_type`` is a
    separate, optional caller-supplied classification string (e.g.
    ``"code-review"``) — chitra does not decide what a task type IS or
    evaluate content to classify one; the caller states it. If the caller
    sets ``task_type`` but leaves ``routing_hint`` unset, ``dispatchd`` may
    fill in ``routing_hint`` from a purely mechanical ``task_type ->
    routing_hint`` lookup table (see ``chitra.routing_config``) — an
    explicit ``routing_hint`` from the caller always wins over this lookup.
    """

    order_id: str
    session_ref: str
    nudge: str
    """Verbatim text to inject. Convention (enforced in practice by
    ``directive_voice_violation``'s regex match on the ``operator`` token):
    chitra must never quote the operator verbatim or speak in the
    operator's voice — no "the operator wants/says", no "chitra
    wants/says/needs/relays", no bare "operator" attribution. A nudge that
    trips the check is rejected by ``dispatch_to_tmux`` (status
    ``BLOCKED``) before anything is pasted."""
    tag: str = "[C]"
    routing_hint: str | None = None
    task_type: str | None = None
    message_kind: Literal["legacy", "operator_relay", "reasoned_answer", "reasoned_nudge", "reasoned_action"] = "legacy"
    decision_attestation: DecisionAttestation | None = None
    input_baseline_hash: str | None = None
    input_seen_hash: str | None = None
    snapshot_tail_hash: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Explicit todo state remains optional because watchd now forces the
    # event-boundary review even when no TodoWrite state is available.
    completion_todo_items: list[TodoItem] | None = None
    completion_evidence: list[CompletionEvidence] = Field(default_factory=list)
    completion_open_asks: list[str] = Field(default_factory=list)
    completion_blockers: list[str] = Field(default_factory=list)

    # Opt-in exemption from dispatchd's guard freeze check (see
    # dispatchd.process_one_order). False for every ordinary order -- the
    # freeze applies and the order is durably deferred, never discarded.
    # Setting this boolean alone is NOT sufficient to bypass the freeze:
    # dispatchd additionally requires task_type to be one of its own sealed
    # internal task types (chitra.rate_limit_guard's checkpoint/stop/re-arm
    # nudges) before honoring it -- an arbitrary queue writer cannot invent a
    # new bypass merely by setting this field, since dispatchd (not the
    # order) controls the allowlist. See docs/SOL-ADVERSARIAL-REVIEW finding #7.
    bypass_rate_limit_freeze: bool = False

    @model_validator(mode="after")
    def validate_reasoning_attestation(self) -> DispatchOrder:
        """Bind autonomous message bytes to one pre-dispatch attestation."""
        reasoned_kinds = {"reasoned_answer", "reasoned_nudge", "reasoned_action"}
        if self.message_kind in reasoned_kinds and self.decision_attestation is None:
            raise ValueError(f"{self.message_kind} dispatch requires decision_attestation")
        if self.message_kind not in reasoned_kinds and self.decision_attestation is not None:
            raise ValueError(f"{self.message_kind} dispatch cannot carry a decision_attestation")
        if self.decision_attestation is not None:
            if self.decision_attestation.outcome != "answer":
                raise ValueError("an abstained decision cannot be dispatched")
            if self.decision_attestation.message_kind != self.message_kind:
                raise ValueError("message_kind must match the decision attestation")
            if self.decision_attestation.approved_text != self.nudge:
                raise ValueError("nudge must exactly match the attested approved_text")
            if self.decision_attestation.operator_confirmation_required and not self.decision_attestation.operator_confirmed:
                raise ValueError("operator-gated decisions require explicit confirmation before dispatch")
        return self


class DispatchResult(BaseModel):
    """Result of processing a dispatch order.

    ``routing_hint`` is copied through unchanged from the originating
    ``DispatchOrder`` when the caller supplied one (opaque pass-through) or
    the ``defaults`` config filled it in. When a structured ``routes`` entry
    resolved the task_type instead, ``routing_hint`` holds the derived
    ``model@harness`` string and ``resolved_zdr`` records its ZDR setting.
    """

    order_id: str
    session_ref: str
    status: DispatchStatus
    reason: str = ""
    marker: str = ""
    tail_hash: str = ""
    transcript_path: str | None = None
    routing_hint: str | None = None
    task_type: str | None = None
    resolved_zdr: bool = False
    decision_attestation_id: str | None = None
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
