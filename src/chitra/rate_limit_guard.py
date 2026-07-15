"""rate_limit_guard — a durable transactional pause/resume state machine for
sessions nearing their provider rate/session limit.

This composes several already-existing, already-tested chitra primitives
under one durable, crash-safe transaction ledger (``chitra.rate_limit_state``)
-- it makes no new judgment about WHETHER to pause; that fact still comes
from ``chitra.usage.evaluate_grouped``. What this module owns is proving
HOW a pause/resume actually happens, verifiably, with no silent stalls:

- ``chitra.usage`` (fact): is a session's account near or over its
  rate-limit thresholds? Pure threshold evaluation, unchanged by this module.
- ``chitra.goals`` (freeze/bookkeeping): ``hold_goal``/``resume_goal``/
  ``due_goals`` records the monitor's hold and preserves the goal itself as
  the re-arm payload. The freeze dispatchd enforces is a plain read of this
  state (see ``chitra.dispatchd``'s module docstring).
- ``chitra.rate_limit_state`` (the durable outbox): every session this
  module is actively pausing or resuming has AT MOST ONE in-flight
  ``Transaction`` walking this exact phase sequence (see
  docs/SOL-ADVERSARIAL-REVIEW finding #2)::

      pause_requested -> checkpoint_sent -> stop_sent -> awaiting_quiescence
          -> held -> resume_requested -> resume_sent -> (removed = working)

  Every phase transition CONSUMES a real ``chitra.dispatchd`` result --
  never assumed. Every waiting phase is bounded by
  ``PolicyConfig.pause``'s deadlines: past the deadline, the sweep retries a
  bounded number of times, then marks the transaction ``escalated`` for
  operator visibility. Escalating NEVER clears the freeze -- the hold only
  ever lifts once a resume is actually confirmed delivered. A crash between
  sweeps (or between any two phases) is not a data-loss event: the next
  sweep re-reads the transaction and continues from wherever it stopped.
- ``chitra.dispatchd``'s JSON order queue (mechanism): every nudge this
  module sends -- the checkpoint instruction, the deterministic ``/goal
  clear`` stop command, and the resume nudge built only from the lane's OWN
  already-stored ``goal``/``done_when`` fields -- are canned literal
  templates, never LLM-authored, handed to the existing queue for the
  already-running ``dispatchd`` daemon to deliver. This module never
  touches a tmux pane directly. Delivery of the checkpoint/stop/resume
  nudges bypasses dispatchd's guard freeze via
  ``DispatchOrder.bypass_rate_limit_freeze`` + a sealed ``task_type`` --
  see ``chitra.dispatchd``'s module docstring; dispatchd, not this module,
  enforces that only the guard's sealed internal task types may use the bypass.
- ``chitra.account_registry`` (freshness-bounded identity): tracks which
  account each tracked ``tmux_session`` was last observed under, so a
  session whose usage snapshot goes missing mid-cycle, or whose account
  identity changes between sweeps, is surfaced as an escalation rather than
  silently ignored or silently merged with an unrelated session (see
  docs/SOL-ADVERSARIAL-REVIEW finding #6).

Verifying the stop, not just labeling it: a checkpoint nudge alone does not
prove anything stopped. For Claude Code, after the checkpoint is CONFIRMED
delivered, this module enqueues the deterministic ``/goal clear`` slash
command and watches the target transcript mtime across sweeps. For Codex,
there is no invented internal stop API: the fixed checkpoint asks the lane to
stop cleanly and the transaction watches Watchd's pane-change timestamp.
Either evidence source must remain unchanged for
``PolicyConfig.pause.quiescence_quiet_seconds`` before the transaction reaches
``held``. Missing backend-appropriate evidence escalates rather than falsely
claiming a verified stop.

Session-ref resolution (a documented assumption, not a verified fact -- see
this repo's PR description for the operator to confirm or correct): a usage
snapshot's ``tmux_session`` field, as written by the deployed
``chitra-usage-snapshot`` sidecar, is the BARE tmux session name (e.g.
``#{session_name}``), not a full ``host:session:pane`` session_ref. This
module assumes the fleet convention documented elsewhere in chitra (one
tracked lane = one dedicated tmux session, its primary work in pane
``0.0``) and resolves ``session_ref = f"{host}:{tmux_session}:0.0"``
directly, with no live tmux enumeration.

Codex host-wide fan-out -- TODO, explicitly not implemented, fails closed:
``chitra.usage.codex_snapshot()`` emits a synthetic account-wide probe with
``tmux_session=""``. It cannot be mapped to a specific pane, so it is
skipped by ``_session_ref_for`` (returns ``None``) and reported in
``SweepReport.skipped`` -- this module never claims, and never silently
attempts, a Codex-wide pause fan-out to per-lane sessions. Implementing that
requires per-lane Codex usage snapshots, which do not currently exist
anywhere in chitra; this is a real, tracked gap, not a wrapped one.

Why a one-shot sweep, not a daemon: chitra's own design record explicitly
rules out a new always-on daemon for this. ``sweep()`` is meant to run under
an external timer (systemd timer / cron), exactly like ``chitra.draft_
scanner``'s periodic-scan shape -- re-running it every few minutes is what
lets the transaction state machine make forward progress and is what makes
"resume it automatically after reset" work, with no long-lived process of
its own. Example two-minute systemd units ship under ``packaging/systemd``.

The same sweep also samples local MemAvailable and Linux memory/CPU PSI. Its
per-host two-sweep anti-flap state and last-shed-first stack live beside the
transaction ledger. Load holds use ``load-shed:<host>:<level>`` and reuse this
exact machine; L3 only tightens graceful deadlines and never kills a lane.
Claude lanes retain the checkpoint plus ``/goal clear`` sequence. Codex lanes
receive a fixed checkpoint-and-stop order, skip that Claude-specific slash
command, and prove quiescence from Watchd's backend-neutral pane-change state.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import structlog

from .account_registry import load_registry, update_registry
from .dispatch import DispatchOrder, DispatchResult, DispatchStatus, transcript_mtime
from .dispatchd import requeue_deferred_for_session
from .goals import (
    LOAD_SHED_HOLD_REASON_PREFIX,
    RATE_LIMIT_HOLD_REASON_PREFIX,
    GoalRecord,
    close_goal,
    done_when_with_delta,
    due_goals,
    get_goal,
    hold_goal,
    list_goals,
    resume_goal,
)
from .lane_activity import load_lane_activity
from .load_shed import (
    PressureSample,
    advance_load_state,
    build_shed_candidates,
    effective_max_running,
    load_shed_reason,
    pause_policy_for_load,
    rank_shed_candidates,
    sample_pressure,
)
from .policy_config import PausePolicy, PolicyConfig, UsagePolicy, load_policy_config
from .rate_limit_state import (
    LoadHostState,
    PauseBackend,
    Transaction,
    get_load_state,
    get_transaction,
    load_transactions,
    remove_transaction,
    upsert_load_state,
    upsert_transaction,
)
from .recovery import record_pause_recovery
from .state_paths import default_queue_dir
from .usage import AccountedVerdict, CodexSnapshotError, codex_snapshot, evaluate_grouped, read_snapshots

logger = structlog.get_logger(__name__)

# Fixed, non-operator-voice checkpoint instruction (see chitra.dispatch's
# directive-voice guard -- this text is checked against that same banned-
# phrase regex before it can ever be pasted into a pane). Never edited per
# lane, never LLM-drafted: a canned literal, like every other chitra nudge
# template.
CHECKPOINT_NUDGE = (
    "Rate limit approaching for this session. Checkpoint now: finish or cleanly abandon the "
    "current step so no file or pull request is left half-written, then write a short resume "
    "note (what's done, what's next) so work can pick up cleanly once the rate-limit window "
    "resets."
)

# A real, deterministic Claude Code slash command -- not prose asking the
# agent to stop. This is what actually clears the session's active /goal
# loop; the guard then verifies via transcript quiescence (see this
# module's docstring) that the turn actually stopped, rather than trusting
# the checkpoint nudge alone.
STOP_NUDGE = "/goal clear"

# Sealed internal task types: dispatchd only honors bypass_rate_limit_freeze
# for orders carrying one of these (see chitra.dispatchd's module docstring).
CHECKPOINT_TASK_TYPE = "rate-limit-checkpoint"
STOP_TASK_TYPE = "rate-limit-stop"
RESUME_TASK_TYPE = "rate-limit-resume"
LOAD_SHED_CHECKPOINT_TASK_TYPE = "load-shed-checkpoint"
LOAD_SHED_STOP_TASK_TYPE = "load-shed-stop"
LOAD_SHED_RESUME_TASK_TYPE = "load-shed-resume"

LOAD_SHED_CHECKPOINT_NUDGE = (
    "Host load pressure requires this lane to yield capacity. Checkpoint now: finish or cleanly abandon the current step, "
    "write a short resume note, and stop producing work until Chitra re-arms the stored goal."
)
CODEX_CHECKPOINT_NUDGE = (
    "Capacity protection requires this Codex lane to checkpoint and stop cleanly. Finish or cleanly abandon the current step, "
    "record what is done and what should happen next, then become quiescent until the stored goal is re-armed."
)


class LanePauseStrategy(Protocol):
    """Backend boundary for the fixed orders used by the shared pause machine."""

    def checkpoint(self, txn: Transaction) -> tuple[str, str]: ...

    def stop(self, txn: Transaction) -> tuple[str, str] | None: ...


@dataclass(frozen=True, slots=True)
class ClaudeLanePauseStrategy:
    """Existing Claude Code checkpoint plus deterministic ``/goal clear``."""

    def checkpoint(self, txn: Transaction) -> tuple[str, str]:
        if txn.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
            return LOAD_SHED_CHECKPOINT_TASK_TYPE, LOAD_SHED_CHECKPOINT_NUDGE
        return CHECKPOINT_TASK_TYPE, CHECKPOINT_NUDGE

    def stop(self, txn: Transaction) -> tuple[str, str]:
        task_type = LOAD_SHED_STOP_TASK_TYPE if txn.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX) else STOP_TASK_TYPE
        return task_type, STOP_NUDGE


@dataclass(frozen=True, slots=True)
class CodexLanePauseStrategy:
    """Codex checkpoint boundary; pane quiescence is the only verified stop signal."""

    def checkpoint(self, txn: Transaction) -> tuple[str, str]:
        task_type = LOAD_SHED_CHECKPOINT_TASK_TYPE if txn.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX) else CHECKPOINT_TASK_TYPE
        return task_type, CODEX_CHECKPOINT_NUDGE

    def stop(self, txn: Transaction) -> None:
        return None


def _pause_strategy(backend: PauseBackend) -> LanePauseStrategy:
    return CodexLanePauseStrategy() if backend == "codex" else ClaudeLanePauseStrategy()


NEVER_PAUSE_SESSION_PREFIXES = ("trailhead:monitor:", "trailhead:boomtown:")


def _resume_nudge(record: GoalRecord) -> str:
    """Build the re-arm nudge from a lane's OWN stored fields -- no LLM authorship."""
    done_when = done_when_with_delta(record)
    if record.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
        return f"Host pressure has cleared -- resuming. Goal: {record.goal} Done when: {done_when}"
    return f"Rate-limit window has reset -- resuming. Goal: {record.goal} Done when: {done_when}"


def _resume_task_type(record: GoalRecord) -> str:
    return LOAD_SHED_RESUME_TASK_TYPE if record.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX) else RESUME_TASK_TYPE


def _hold_reason_for(verdict: AccountedVerdict) -> str:
    return f"{RATE_LIMIT_HOLD_REASON_PREFIX}{verdict.binding_window}"


def _resume_at_iso(resume_at_epoch: int) -> str:
    return datetime.fromtimestamp(resume_at_epoch, UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _session_ref_for(verdict: AccountedVerdict, *, host: str) -> str | None:
    """Resolve a session_ref from one account-grouped verdict, or None.

    See this module's docstring for the pane-0.0 assumption and for why a
    verdict with no ``tmux_session`` (the synthetic Codex account-wide probe)
    is always skipped here -- Codex host-wide fan-out is a documented,
    explicitly unimplemented gap, not something this function can infer.
    """
    if not verdict.tmux_session:
        return None
    return f"{host}:{verdict.tmux_session}:0.0"


def _enqueue(queue_dir: Path, order: DispatchOrder) -> Path:
    """Write one DispatchOrder JSON file into ``queue_dir/orders`` for the
    already-running ``dispatchd`` to drain -- QUEUE-PRIMARY, matching every
    other caller's convention; this module never touches a tmux pane."""
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / f"{order.order_id}.json"
    tmp = orders_dir / f".{order.order_id}.json.tmp"
    tmp.write_text(order.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _read_result(queue_dir: Path, order_id: str) -> DispatchResult | None:
    """Read a dispatchd result for ``order_id``, or None if not (yet) resulted."""
    path = queue_dir / "results" / f"{order_id}.json"
    try:
        return DispatchResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PauseOutcome:
    """One transaction that reached ``held`` (a verified graceful pause) this sweep."""

    session_ref: str
    hold_reason: str
    resume_at: str

    def to_dict(self) -> dict[str, object]:
        return {"session_ref": self.session_ref, "hold_reason": self.hold_reason, "resume_at": self.resume_at}


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """One transaction that reached ``working`` (a confirmed resume) this sweep."""

    session_ref: str
    resume_order_id: str

    def to_dict(self) -> dict[str, object]:
        return {"session_ref": self.session_ref, "resume_order_id": self.resume_order_id}


@dataclass(slots=True)
class SweepReport:
    """The full result of one sweep pass."""

    paused: list[PauseOutcome] = field(default_factory=list)
    resumed: list[ResumeOutcome] = field(default_factory=list)
    advanced: list[str] = field(default_factory=list)  # interim phase-transition notes, for operator visibility
    cleared: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    load_level: int = 0
    shed_lanes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "paused": [item.to_dict() for item in self.paused],
            "resumed": [item.to_dict() for item in self.resumed],
            "advanced": self.advanced,
            "cleared": self.cleared,
            "skipped": self.skipped,
            "escalations": self.escalations,
            "load_level": self.load_level,
            "shed_lanes": self.shed_lanes,
        }


@dataclass(frozen=True, slots=True)
class _Advance:
    """The result of progressing one transaction by (at most) one phase."""

    txn: Transaction
    note: str | None = None
    escalation: str | None = None
    finished: bool = False  # resume-side only: True once a resume nudge is CONFIRMED sent


def _bounded_wait(txn: Transaction, *, deadline_seconds: int, max_attempts: int, now: datetime, waiting_for: str) -> _Advance:
    """No evidence yet: wait quietly until the phase's own deadline, then
    hand off to ``_escalate_or_retry``. Never strands: bounded by
    ``max_attempts``, see that function."""
    deadline = _parse_iso(txn.deadline_at) if txn.deadline_at else now
    if now < deadline:
        return _Advance(txn)
    return _escalate_or_retry(txn, deadline_seconds=deadline_seconds, max_attempts=max_attempts, now=now, waiting_for=waiting_for)


def _escalate_or_retry(txn: Transaction, *, deadline_seconds: int, max_attempts: int, now: datetime, waiting_for: str) -> _Advance:
    """A phase's deadline has passed (or a terminal non-SENT result already
    proves the last attempt failed): retry up to ``max_attempts``, extending
    the deadline each time, then permanently mark ``escalated``. An escalated
    transaction is reported every sweep for visibility but is not retried
    again automatically -- and the freeze it is holding is never lifted by
    this path. See docs/SOL-ADVERSARIAL-REVIEW finding #2 ("no strand-forever")."""
    if txn.escalated:
        return _Advance(txn, escalation=f"{txn.session_ref}: still waiting on {waiting_for} (escalated earlier; freeze remains)")
    if txn.attempts + 1 >= max_attempts:
        escalated = replace(txn, escalated=True, updated_at=now.isoformat())
        return _Advance(
            escalated,
            escalation=(
                f"{txn.session_ref}: {waiting_for} exceeded {max_attempts} attempts -- "
                "escalating; freeze remains, operator attention needed"
            ),
        )
    retried = replace(
        txn, attempts=txn.attempts + 1, deadline_at=(now + timedelta(seconds=deadline_seconds)).isoformat(), updated_at=now.isoformat()
    )
    return _Advance(retried, note=f"{txn.session_ref}: {waiting_for} -- retrying (attempt {retried.attempts}/{max_attempts})")


def plan_pauses(verdicts: list[AccountedVerdict], *, host: str, goals_root: Path | None) -> tuple[list[AccountedVerdict], list[str]]:
    """Pure planning pass: which fresh 'pause'-level verdicts need a NEW
    transaction started right now. Returns ``(to_pause, skip_reasons)``.

    A session with no chitra goal record is not tracked -- skipped, not an
    error. A session already held for THIS exact rate-limit window is
    idempotently skipped (the transaction machine, not this function, is
    what makes further progress on it). A session held for any OTHER reason
    (operator, throttle) is never silently overridden.
    """
    to_pause: list[AccountedVerdict] = []
    skipped: list[str] = []
    for verdict in verdicts:
        if verdict.level != "pause":
            continue
        session_ref = _session_ref_for(verdict, host=host)
        if session_ref is None:
            skipped.append(f"{verdict.session_id}: no tmux_session on this snapshot -- cannot resolve a dispatch target")
            continue
        if session_ref.startswith(NEVER_PAUSE_SESSION_PREFIXES):
            skipped.append(f"{session_ref}: Chitra's own monitor/harness session is never paused")
            continue
        existing = get_goal(goals_root, session_ref)
        if existing is None:
            skipped.append(f"{session_ref}: no chitra goal record -- not tracked, nothing to pause")
            continue
        hold_reason = _hold_reason_for(verdict)
        resume_at_iso = _resume_at_iso(verdict.resume_at_epoch)
        if existing.status == "held" and existing.hold_reason == hold_reason and existing.resume_at == resume_at_iso:
            continue  # already paused for this exact window -- the transaction machine drives the rest
        if existing.status == "held" and not existing.hold_reason.startswith(RATE_LIMIT_HOLD_REASON_PREFIX):
            skipped.append(f"{session_ref}: already held for a non-rate-limit reason ({existing.hold_reason!r}) -- not overriding")
            continue
        to_pause.append(verdict)
    return to_pause, skipped


def apply_pause(
    subject: AccountedVerdict | GoalRecord,
    *,
    host: str,
    goals_root: Path | None,
    now: datetime,
    hold_reason: str | None = None,
    backend: PauseBackend | None = None,
) -> Transaction:
    """Start a new pause transaction for one session already selected by
    ``plan_pauses``. The hold is applied FIRST, unconditionally -- the
    rate-limit fact driving this is external and real regardless of what
    happens next -- and is the freeze dispatchd enforces from this instant.
    Everything after this (checkpoint, stop, quiescence verification) is
    the transaction machine's job, driven by later sweeps.
    """
    if isinstance(subject, AccountedVerdict):
        session_ref = _session_ref_for(subject, host=host)
        assert session_ref is not None  # plan_pauses already filtered
        resolved_reason = _hold_reason_for(subject)
        resume_at_iso = _resume_at_iso(subject.resume_at_epoch)
        resolved_backend: PauseBackend = subject.kind
    else:
        session_ref = subject.session_ref
        if hold_reason is None or not hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
            raise ValueError("load-shed apply_pause requires a load-shed hold reason")
        resolved_reason = hold_reason
        resume_at_iso = ""
        resolved_backend = backend or "claude"
    hold_goal(goals_root, session_ref, reason=resolved_reason, resume_at=resume_at_iso)
    txn = Transaction(
        session_ref=session_ref,
        phase="pause_requested",
        backend=resolved_backend,
        hold_reason=resolved_reason,
        resume_at=resume_at_iso,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    logger.info(
        "rate_limit_guard_pause_started",
        session_ref=session_ref,
        hold_reason=resolved_reason,
        resume_at=resume_at_iso,
        backend=resolved_backend,
    )
    return upsert_transaction(goals_root, txn)


def _progress_pause_transaction(
    txn: Transaction,
    *,
    queue_dir: Path,
    pause_policy: PausePolicy,
    activity_root: Path | None,
    now: datetime,
) -> _Advance:
    """Advance a pause-side transaction by at most one phase this sweep."""
    strategy = _pause_strategy(txn.backend)
    if txn.phase == "pause_requested":
        task_type, nudge = strategy.checkpoint(txn)
        order_id = f"{task_type}-{uuid.uuid4().hex[:12]}"
        _enqueue(
            queue_dir,
            DispatchOrder(
                order_id=order_id,
                session_ref=txn.session_ref,
                nudge=nudge,
                task_type=task_type,
                bypass_rate_limit_freeze=True,
                created_at=now.isoformat(),
            ),
        )
        advanced = replace(
            txn,
            phase="checkpoint_sent",
            checkpoint_order_id=order_id,
            attempts=0,
            escalated=False,
            deadline_at=(now + timedelta(seconds=pause_policy.checkpoint_deadline_seconds)).isoformat(),
            updated_at=now.isoformat(),
        )
        return _Advance(advanced, note=f"{txn.session_ref}: checkpoint nudge enqueued ({order_id})")

    if txn.phase == "checkpoint_sent":
        result = _read_result(queue_dir, txn.checkpoint_order_id)
        if result is None:
            return _bounded_wait(
                txn,
                deadline_seconds=pause_policy.checkpoint_deadline_seconds,
                max_attempts=pause_policy.max_retry_attempts,
                now=now,
                waiting_for="checkpoint delivery",
            )
        if result.status != DispatchStatus.SENT:
            advance = _escalate_or_retry(
                txn,
                deadline_seconds=pause_policy.checkpoint_deadline_seconds,
                max_attempts=pause_policy.max_retry_attempts,
                now=now,
                waiting_for=f"checkpoint delivery (last result: {result.status.value})",
            )
            return _retry_with_fresh_checkpoint(advance, txn, queue_dir=queue_dir, now=now)
        stop = strategy.stop(txn)
        if stop is None:
            advanced = replace(
                txn,
                phase="awaiting_quiescence",
                transcript_path=result.transcript_path or "",
                last_transcript_mtime=None,
                last_activity_token="",
                quiescent_since="",
                attempts=0,
                escalated=False,
                deadline_at=(now + timedelta(seconds=pause_policy.quiescence_timeout_seconds)).isoformat(),
                updated_at=now.isoformat(),
            )
            return _Advance(advanced, note=f"{txn.session_ref}: Codex checkpoint confirmed sent; verifying pane quiescence")
        stop_task_type, stop_nudge = stop
        order_id = f"{stop_task_type}-{uuid.uuid4().hex[:12]}"
        _enqueue(
            queue_dir,
            DispatchOrder(
                order_id=order_id,
                session_ref=txn.session_ref,
                nudge=stop_nudge,
                task_type=stop_task_type,
                bypass_rate_limit_freeze=True,
                created_at=now.isoformat(),
            ),
        )
        advanced = replace(
            txn,
            phase="stop_sent",
            stop_order_id=order_id,
            transcript_path=result.transcript_path or "",
            attempts=0,
            escalated=False,
            deadline_at=(now + timedelta(seconds=pause_policy.stop_deadline_seconds)).isoformat(),
            updated_at=now.isoformat(),
        )
        return _Advance(advanced, note=f"{txn.session_ref}: checkpoint confirmed sent; stop-clear order enqueued ({order_id})")

    if txn.phase == "stop_sent":
        result = _read_result(queue_dir, txn.stop_order_id)
        if result is None:
            return _bounded_wait(
                txn,
                deadline_seconds=pause_policy.stop_deadline_seconds,
                max_attempts=pause_policy.max_retry_attempts,
                now=now,
                waiting_for="stop-clear delivery",
            )
        if result.status != DispatchStatus.SENT:
            advance = _escalate_or_retry(
                txn,
                deadline_seconds=pause_policy.stop_deadline_seconds,
                max_attempts=pause_policy.max_retry_attempts,
                now=now,
                waiting_for=f"stop-clear delivery (last result: {result.status.value})",
            )
            return _retry_with_fresh_stop(advance, txn, queue_dir=queue_dir, now=now)
        transcript_path = result.transcript_path or txn.transcript_path
        advanced = replace(
            txn,
            phase="awaiting_quiescence",
            transcript_path=transcript_path,
            last_transcript_mtime=None,
            last_activity_token="",
            quiescent_since="",
            attempts=0,
            escalated=False,
            deadline_at=(now + timedelta(seconds=pause_policy.quiescence_timeout_seconds)).isoformat(),
            updated_at=now.isoformat(),
        )
        return _Advance(advanced, note=f"{txn.session_ref}: stop-clear confirmed sent; verifying the turn actually stopped")

    if txn.phase == "awaiting_quiescence":
        advance = _advance_quiescence(txn, pause_policy=pause_policy, activity_root=activity_root, now=now)
        if advance.txn.phase == "held":
            record_pause_recovery(activity_root, advance.txn, paused_at=now.isoformat())
        return advance

    return _Advance(txn)  # "held": nothing left for the pause side to do


def _retry_with_fresh_checkpoint(advance: _Advance, original: Transaction, *, queue_dir: Path, now: datetime) -> _Advance:
    """If ``_escalate_or_retry`` decided to retry (not escalate), the prior
    checkpoint attempt is presumed genuinely failed (a terminal non-SENT
    result already proved it) -- re-enqueue a fresh checkpoint order under a
    new order id so the retry is a real new delivery attempt, not a replay
    of a dead one."""
    if advance.txn.escalated or advance.txn.attempts == original.attempts:
        return advance
    task_type, nudge = _pause_strategy(original.backend).checkpoint(original)
    order_id = f"{task_type}-{uuid.uuid4().hex[:12]}"
    _enqueue(
        queue_dir,
        DispatchOrder(
            order_id=order_id,
            session_ref=original.session_ref,
            nudge=nudge,
            task_type=task_type,
            bypass_rate_limit_freeze=True,
            created_at=now.isoformat(),
        ),
    )
    return _Advance(replace(advance.txn, checkpoint_order_id=order_id), note=advance.note, escalation=advance.escalation)


def _retry_with_fresh_stop(advance: _Advance, original: Transaction, *, queue_dir: Path, now: datetime) -> _Advance:
    """The stop-side counterpart of ``_retry_with_fresh_checkpoint``."""
    if advance.txn.escalated or advance.txn.attempts == original.attempts:
        return advance
    stop = _pause_strategy(original.backend).stop(original)
    if stop is None:
        return advance
    task_type, nudge = stop
    order_id = f"{task_type}-{uuid.uuid4().hex[:12]}"
    _enqueue(
        queue_dir,
        DispatchOrder(
            order_id=order_id,
            session_ref=original.session_ref,
            nudge=nudge,
            task_type=task_type,
            bypass_rate_limit_freeze=True,
            created_at=now.isoformat(),
        ),
    )
    return _Advance(replace(advance.txn, stop_order_id=order_id), note=advance.note, escalation=advance.escalation)


def _advance_quiescence(txn: Transaction, *, pause_policy: PausePolicy, activity_root: Path | None, now: datetime) -> _Advance:
    """Poll (no sleeping -- see this module's docstring) whether the target
    transcript has gone quiet for ``quiescence_quiet_seconds``. Only a
    confirmed quiet window marks the transaction ``held`` -- a timeout with
    no confirmation escalates; it never assumes the turn stopped just
    because time ran out."""
    if not txn.transcript_path and txn.backend == "codex":
        activity = next((item for item in load_lane_activity(activity_root) if item.session_ref == txn.session_ref), None)
        if activity is None:
            return _bounded_wait(
                txn,
                deadline_seconds=pause_policy.quiescence_timeout_seconds,
                max_attempts=pause_policy.max_retry_attempts,
                now=now,
                waiting_for="Codex turn-stopped verification (watchd has no pane activity fact for this lane)",
            )
        token = activity.last_change_at
        if not txn.last_activity_token or token != txn.last_activity_token:
            return _Advance(replace(txn, last_activity_token=token, quiescent_since=now.isoformat(), updated_at=now.isoformat()))
        quiescent_since = _parse_iso(txn.quiescent_since) if txn.quiescent_since else now
        if (now - quiescent_since).total_seconds() >= pause_policy.quiescence_quiet_seconds:
            held = replace(txn, phase="held", updated_at=now.isoformat())
            return _Advance(held, note=f"{txn.session_ref}: pane confirmed quiet -- hold verified")
        deadline = _parse_iso(txn.deadline_at) if txn.deadline_at else now
        if now < deadline:
            return _Advance(txn)
        return _escalate_or_retry(
            txn,
            deadline_seconds=pause_policy.quiescence_timeout_seconds,
            max_attempts=pause_policy.max_retry_attempts,
            now=now,
            waiting_for="Codex turn-stopped verification (pane quiet, but below the required quiet window)",
        )
    if not txn.transcript_path:
        return _bounded_wait(
            txn,
            deadline_seconds=pause_policy.quiescence_timeout_seconds,
            max_attempts=1,
            now=now,
            waiting_for="turn-stopped verification (delivery was only confirmed via the weaker pane-capture "
            "fallback, so no transcript is available to verify quiescence against)",
        )
    parts = txn.session_ref.split(":")
    host = parts[0] if len(parts) == 3 else ""
    mtime = transcript_mtime(txn.transcript_path, host=host)
    if mtime is None:
        return _bounded_wait(
            txn,
            deadline_seconds=pause_policy.quiescence_timeout_seconds,
            max_attempts=pause_policy.max_retry_attempts,
            now=now,
            waiting_for="turn-stopped verification (transcript unreadable)",
        )
    if txn.last_transcript_mtime is None or mtime != txn.last_transcript_mtime:
        # Still active (or this is the first observation) -- keep watching.
        # No note emitted every sweep to avoid log spam for ordinary waiting.
        return _Advance(replace(txn, last_transcript_mtime=mtime, quiescent_since=now.isoformat(), updated_at=now.isoformat()))
    quiescent_since = _parse_iso(txn.quiescent_since) if txn.quiescent_since else now
    if (now - quiescent_since).total_seconds() >= pause_policy.quiescence_quiet_seconds:
        held = replace(txn, phase="held", updated_at=now.isoformat())
        return _Advance(held, note=f"{txn.session_ref}: turn confirmed stopped -- hold verified")
    deadline = _parse_iso(txn.deadline_at) if txn.deadline_at else now
    if now < deadline:
        return _Advance(txn)
    return _escalate_or_retry(
        txn,
        deadline_seconds=pause_policy.quiescence_timeout_seconds,
        max_attempts=pause_policy.max_retry_attempts,
        now=now,
        waiting_for="turn-stopped verification (transcript quiet, but below the required quiet window)",
    )


def plan_resumes(
    *, goals_root: Path | None, verdicts: list[AccountedVerdict], policy: UsagePolicy, now: datetime
) -> tuple[list[GoalRecord], list[str]]:
    """Pure planning pass over due rate-limit holds whose pause transaction
    has fully reached ``held`` (or has no transaction at all -- an
    operator- or legacy-applied hold with no guard-managed transaction is
    treated as already-paused and resumable). A hold whose pause sequence
    is still in flight (checkpoint/stop/quiescence not yet verified) is
    left for the pause-side progression to finish first, even if its window
    has technically reset. Confirms the fresh verdict is back to ``ok``
    before resuming -- never resume into a still-hot window. Respects
    ``policy.auto_resume``.
    """
    by_ref = {verdict.tmux_session: verdict for verdict in verdicts if verdict.tmux_session}
    to_resume: list[GoalRecord] = []
    escalations: list[str] = []
    for record in due_goals(goals_root, now=now):
        if not record.hold_reason.startswith(RATE_LIMIT_HOLD_REASON_PREFIX):
            continue  # operator/throttle holds are never auto-resumed by this module
        existing_txn = get_transaction(goals_root, record.session_ref)
        if existing_txn is not None and existing_txn.phase != "held":
            continue  # still finishing (or already resuming) -- let progression handle it
        parts = record.session_ref.split(":")
        bare_tmux_session = parts[1] if len(parts) == 3 else record.session_ref
        fresh = by_ref.get(bare_tmux_session)
        if fresh is None:
            continue  # no usage signal this sweep -- retry next sweep, never guess
        if fresh.level != "ok":
            continue  # still hot (or unknown) -- never resume into a still-hot window
        if not policy.auto_resume:
            escalations.append(f"{record.session_ref}: due for resume but auto_resume is False -- operator confirmation required")
            continue
        to_resume.append(record)
    return to_resume, escalations


def select_next_resume(eligible: list[GoalRecord], *, priority_session_refs: tuple[str, ...] = ()) -> GoalRecord | None:
    """Select exactly one resume candidate in a documented stable order.

    Rate-limit resumes use ``session_ref`` ascending.  A caller with a
    durable semantic order (load shedding's last-shed-first stack) supplies
    that order explicitly; unknown refs remain stable by ``session_ref``.
    """
    if not eligible:
        return None
    priority = {session_ref: index for index, session_ref in enumerate(priority_session_refs)}
    fallback = len(priority)
    return min(eligible, key=lambda record: (priority.get(record.session_ref, fallback), record.session_ref))


def apply_resume(record: GoalRecord, *, goals_root: Path | None, now: datetime) -> Transaction:
    """Start (or continue) a resume transaction for one lane already
    selected by ``plan_resumes``. Does NOT clear the hold -- that only
    happens once the resume nudge is CONFIRMED delivered (see
    ``_progress_resume_transaction``), fixing the original unconditional
    hold-then-enqueue ordering bug (docs/SOL-ADVERSARIAL-REVIEW finding #2,
    item 4)."""
    existing = get_transaction(goals_root, record.session_ref)
    base = existing or Transaction(
        session_ref=record.session_ref,
        phase="held",
        hold_reason=record.hold_reason,
        resume_at=record.resume_at,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    txn = replace(base, phase="resume_requested", attempts=0, escalated=False, deadline_at="", updated_at=now.isoformat())
    logger.info("rate_limit_guard_resume_started", session_ref=record.session_ref)
    return upsert_transaction(goals_root, txn)


def _progress_resume_transaction(
    txn: Transaction, record: GoalRecord, *, queue_dir: Path, pause_policy: PausePolicy, now: datetime
) -> _Advance:
    """Advance a resume-side transaction by at most one phase this sweep."""
    if txn.phase == "resume_requested":
        task_type = _resume_task_type(record)
        order_id = f"{task_type}-{uuid.uuid4().hex[:12]}"
        _enqueue(
            queue_dir,
            DispatchOrder(
                order_id=order_id,
                session_ref=txn.session_ref,
                nudge=_resume_nudge(record),
                task_type=task_type,
                bypass_rate_limit_freeze=True,
                created_at=now.isoformat(),
            ),
        )
        advanced = replace(
            txn,
            phase="resume_sent",
            resume_order_id=order_id,
            attempts=0,
            escalated=False,
            deadline_at=(now + timedelta(seconds=pause_policy.resume_deadline_seconds)).isoformat(),
            updated_at=now.isoformat(),
        )
        return _Advance(advanced, note=f"{txn.session_ref}: re-arm nudge enqueued ({order_id})")

    if txn.phase == "resume_sent":
        result = _read_result(queue_dir, txn.resume_order_id)
        if result is None:
            return _bounded_wait(
                txn,
                deadline_seconds=pause_policy.resume_deadline_seconds,
                max_attempts=pause_policy.max_retry_attempts,
                now=now,
                waiting_for="resume delivery",
            )
        if result.status != DispatchStatus.SENT:
            advance = _escalate_or_retry(
                txn,
                deadline_seconds=pause_policy.resume_deadline_seconds,
                max_attempts=pause_policy.max_retry_attempts,
                now=now,
                waiting_for=f"resume delivery (last result: {result.status.value})",
            )
            if not advance.txn.escalated and advance.txn.attempts != txn.attempts:
                task_type = _resume_task_type(record)
                order_id = f"{task_type}-{uuid.uuid4().hex[:12]}"
                _enqueue(
                    queue_dir,
                    DispatchOrder(
                        order_id=order_id,
                        session_ref=txn.session_ref,
                        nudge=_resume_nudge(record),
                        task_type=task_type,
                        bypass_rate_limit_freeze=True,
                        created_at=now.isoformat(),
                    ),
                )
                advance = _Advance(replace(advance.txn, resume_order_id=order_id), note=advance.note, escalation=advance.escalation)
            return advance
        return _Advance(txn, finished=True)

    return _Advance(txn)


def clear_superseded_holds(*, goals_root: Path | None) -> list[str]:
    """Close dead superseded goals so no resume path can ever re-arm them."""
    cleared: list[str] = []
    for record in list_goals(goals_root):
        if record.status != "held" or not record.hold_reason.startswith("superseded-by:"):
            continue
        close_goal(goals_root, record.session_ref, administrative=True)
        remove_transaction(goals_root, record.session_ref)
        cleared.append(record.session_ref)
        logger.info("rate_limit_guard_superseded_hold_cleared", session_ref=record.session_ref)
    return sorted(cleared)


def _load_level_from_reason(hold_reason: str) -> int:
    if not hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
        return 0
    try:
        level = int(hold_reason.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0
    return level if level in (1, 2, 3) else 0


def _pause_policy_for_transaction(txn: Transaction, policy: PolicyConfig) -> PausePolicy:
    level = _load_level_from_reason(txn.hold_reason)
    return pause_policy_for_load(policy.pause, policy.load, level) if level else policy.pause


def _plan_load_resumes(
    *,
    goals_root: Path | None,
    verdicts: list[AccountedVerdict],
    load_state: LoadHostState,
) -> list[GoalRecord]:
    """Return cleared-pressure load holds, preserving the durable shed stack."""
    if load_state.load_level != 0:
        return []
    verdict_by_session = {verdict.tmux_session: verdict for verdict in verdicts if verdict.tmux_session}
    eligible: list[GoalRecord] = []
    for session_ref in reversed(load_state.shed_lanes):
        record = get_goal(goals_root, session_ref)
        if record is None or record.status != "held" or not record.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
            continue
        txn = get_transaction(goals_root, session_ref)
        if txn is not None and txn.phase != "held":
            continue
        parts = session_ref.split(":")
        tmux_session = parts[1] if len(parts) == 3 else session_ref
        usage = verdict_by_session.get(tmux_session)
        if usage is not None and usage.level != "ok":
            continue
        eligible.append(record)
    return eligible


def sweep(
    *,
    usage_dir: Path,
    host: str,
    staleness_seconds: int = 1200,
    include_codex: bool = False,
    codex_bin: Path | str = "codex",
    goals_root: Path | None = None,
    queue_dir: Path | None = None,
    policy: PolicyConfig | None = None,
    pressure_sample: PressureSample | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """Run one sweep pass: fold fresh usage into the account registry,
    progress every in-flight pause/resume transaction by as much as current
    evidence allows, then detect brand-new pauses/resumes to start.

    ``usage_dir`` and ``host`` are one host's worth of usage snapshots at a
    time -- exactly like ``chitra-usage evaluate``'s own single-``--dir``
    shape; a multi-host fleet runs this once per host. ``goals_root`` is the
    single state root for ``chitra.goals``, ``chitra.rate_limit_state``, and
    ``chitra.account_registry`` alike (all "this host's chitra state").
    """
    resolved_policy = policy or PolicyConfig()
    current = datetime.now(UTC) if now is None else now
    resolved_queue_dir = queue_dir or default_queue_dir()

    snapshots = read_snapshots(usage_dir, staleness_seconds=staleness_seconds, now=current)
    if include_codex:
        snapshots.append((codex_snapshot(codex_bin=codex_bin, now=current), True))
    verdicts = evaluate_grouped(snapshots, policy=resolved_policy.usage)

    report = SweepReport()

    sampled_pressure = pressure_sample or sample_pressure()
    load_state = advance_load_state(
        get_load_state(goals_root, host),
        host=host,
        sample=sampled_pressure,
        policy=resolved_policy.load,
        now=current,
    )
    load_state = upsert_load_state(goals_root, load_state)
    report.load_level = load_state.load_level
    report.shed_lanes = list(load_state.shed_lanes)

    registry_update = update_registry(goals_root, verdicts, now=current)
    for entry in registry_update.disappeared:
        report.escalations.append(
            f"{entry.tmux_session}: usage snapshot missing this sweep (last known account {entry.account!r}, "
            f"last seen {entry.updated_at}) -- cannot safely pause or resume without a live signal"
        )
    for tmux_session, old_account, new_account in registry_update.account_changed:
        report.escalations.append(
            f"{tmux_session}: account identity changed ({old_account!r} -> {new_account!r}) -- "
            "not carrying prior hold/pause state forward under the new identity"
        )

    report.cleared.extend(clear_superseded_holds(goals_root=goals_root))
    goal_by_ref = {record.session_ref: record for record in list_goals(goals_root)}
    current_shed_lanes = tuple(
        session_ref
        for session_ref in load_state.shed_lanes
        if (record := goal_by_ref.get(session_ref)) is not None
        and record.status == "held"
        and record.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX)
    )
    if current_shed_lanes != load_state.shed_lanes:
        load_state = upsert_load_state(
            goals_root,
            replace(load_state, shed_lanes=current_shed_lanes, updated_at=current.isoformat()),
        )
        report.shed_lanes = list(load_state.shed_lanes)

    # Reconcile any hold with no transaction record (e.g. an operator-applied
    # rate-limit: hold, or a crash between hold_goal and the transaction's
    # own first write) -- recovered as pause_requested so the machine picks
    # it up, instead of being silently invisible forever. See
    # docs/SOL-ADVERSARIAL-REVIEW finding #2, item 1.
    for record in goal_by_ref.values():
        if record.status != "held" or not record.hold_reason.startswith((RATE_LIMIT_HOLD_REASON_PREFIX, LOAD_SHED_HOLD_REASON_PREFIX)):
            continue
        if get_transaction(goals_root, record.session_ref) is not None:
            continue
        logger.warning("rate_limit_guard_reconciling_orphaned_hold", session_ref=record.session_ref, hold_reason=record.hold_reason)
        upsert_transaction(
            goals_root,
            Transaction(
                session_ref=record.session_ref,
                phase="pause_requested",
                hold_reason=record.hold_reason,
                resume_at=record.resume_at,
                created_at=current.isoformat(),
                updated_at=current.isoformat(),
            ),
        )
        report.advanced.append(f"{record.session_ref}: reconciled a hold with no transaction record (recovering as pause_requested)")

    # --- progress every pause-side transaction already in flight ----------
    for txn in load_transactions(goals_root):
        if txn.phase in ("held", "resume_requested", "resume_sent"):
            continue
        if txn.session_ref not in goal_by_ref:
            remove_transaction(goals_root, txn.session_ref)  # goal was closed out from under the guard
            continue
        advance = _progress_pause_transaction(
            txn,
            queue_dir=resolved_queue_dir,
            pause_policy=_pause_policy_for_transaction(txn, resolved_policy),
            activity_root=goals_root,
            now=current,
        )
        if advance.txn != txn:
            upsert_transaction(goals_root, advance.txn)
        if advance.note:
            report.advanced.append(advance.note)
        if advance.escalation:
            report.escalations.append(advance.escalation)
        if advance.txn.phase == "held" and txn.phase != "held":
            report.paused.append(
                PauseOutcome(session_ref=advance.txn.session_ref, hold_reason=advance.txn.hold_reason, resume_at=advance.txn.resume_at)
            )

    # --- detect brand-new pauses -------------------------------------------
    to_pause, skip_reasons = plan_pauses(verdicts, host=host, goals_root=goals_root)
    report.skipped.extend(skip_reasons)
    for verdict in to_pause:
        new_txn = apply_pause(verdict, host=host, goals_root=goals_root, now=current)
        report.advanced.append(f"{new_txn.session_ref}: rate-limit hold applied, pause sequence started")

    # --- shed enough newly selected lanes to reach the active load cap -----
    if load_state.load_level > 0:
        current_goals = list_goals(goals_root)
        candidates = build_shed_candidates(
            current_goals,
            activities=load_lane_activity(goals_root),
            registry=load_registry(goals_root),
            host=host,
        )
        cap = effective_max_running(resolved_policy.usage, resolved_policy.load, load_state.load_level)
        excess = max(0, len(candidates) - cap)
        for candidate in rank_shed_candidates(candidates)[:excess]:
            if candidate.goal is None:
                report.skipped.append(f"{candidate.session_ref}: no goal record -- cannot create a durable load-shed hold")
                continue
            reason = load_shed_reason(host, load_state.load_level)
            new_txn = apply_pause(
                candidate.goal,
                host=host,
                goals_root=goals_root,
                now=current,
                hold_reason=reason,
                backend=candidate.backend,
            )
            if new_txn.session_ref not in load_state.shed_lanes:
                load_state = replace(
                    load_state,
                    shed_lanes=(*load_state.shed_lanes, new_txn.session_ref),
                    updated_at=current.isoformat(),
                )
            report.advanced.append(f"{new_txn.session_ref}: {reason} hold applied, shared pause sequence started")
        load_state = upsert_load_state(goals_root, load_state)
        report.shed_lanes = list(load_state.shed_lanes)

    # --- progress every resume-side transaction already in flight ---------
    for txn in load_transactions(goals_root):
        if txn.phase not in ("resume_requested", "resume_sent"):
            continue
        resume_record = goal_by_ref.get(txn.session_ref)
        if resume_record is None:
            remove_transaction(goals_root, txn.session_ref)
            continue
        advance = _progress_resume_transaction(
            txn,
            resume_record,
            queue_dir=resolved_queue_dir,
            pause_policy=_pause_policy_for_transaction(txn, resolved_policy),
            now=current,
        )
        if advance.finished:
            resume_goal(goals_root, txn.session_ref)
            requeued = requeue_deferred_for_session(resolved_queue_dir, txn.session_ref)
            remove_transaction(goals_root, txn.session_ref)
            if txn.hold_reason.startswith(LOAD_SHED_HOLD_REASON_PREFIX):
                load_state = replace(
                    load_state,
                    shed_lanes=tuple(session_ref for session_ref in load_state.shed_lanes if session_ref != txn.session_ref),
                    updated_at=current.isoformat(),
                )
                upsert_load_state(goals_root, load_state)
                report.shed_lanes = list(load_state.shed_lanes)
            report.resumed.append(ResumeOutcome(session_ref=txn.session_ref, resume_order_id=advance.txn.resume_order_id))
            report.advanced.append(f"{txn.session_ref}: resume confirmed sent; hold cleared; {len(requeued)} deferred order(s) requeued")
            continue
        if advance.txn != txn:
            upsert_transaction(goals_root, advance.txn)
        if advance.note:
            report.advanced.append(advance.note)
        if advance.escalation:
            report.escalations.append(advance.escalation)

    # --- detect brand-new resumes ------------------------------------------
    to_resume, resume_escalations = plan_resumes(goals_root=goals_root, verdicts=verdicts, policy=resolved_policy.usage, now=current)
    report.escalations.extend(resume_escalations)
    load_resumes = _plan_load_resumes(goals_root=goals_root, verdicts=verdicts, load_state=load_state)
    priority = tuple(reversed(load_state.shed_lanes))
    next_resume = select_next_resume([*load_resumes, *to_resume], priority_session_refs=priority)
    if next_resume is not None:
        new_txn = apply_resume(next_resume, goals_root=goals_root, now=current)
        report.advanced.append(f"{new_txn.session_ref}: resume sequence started")

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-rate-limit-guard",
        description="Advance one detect/checkpoint/stop-verify/hold/resume sweep pass for sessions nearing their rate/session limit.",
    )
    parser.add_argument("--usage-dir", type=Path, required=True, help="Directory of chitra.usage.v1 snapshot files for one host.")
    parser.add_argument("--host", required=True, help="The host these snapshots' sessions run on (used to build each session_ref).")
    parser.add_argument("--staleness-seconds", type=int, default=1200)
    parser.add_argument("--codex", action="store_true", help="Also read this host's local Codex account usage.")
    parser.add_argument("--codex-bin", type=Path, default=Path("codex"))
    parser.add_argument(
        "--goals-root",
        type=Path,
        default=None,
        help="chitra state root (goals, transactions, account registry); default: CHITRA_STATE_DIR.",
    )
    parser.add_argument("--queue-dir", type=Path, default=None, help="dispatchd order queue root (default: CHITRA_STATE_DIR/queue).")
    parser.add_argument(
        "--policy-config", type=Path, default=None, help="Path to policy.yaml (env CHITRA_POLICY_CONFIG, else shipped defaults)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        policy = load_policy_config(args.policy_config)
        report = sweep(
            usage_dir=args.usage_dir,
            host=args.host,
            staleness_seconds=args.staleness_seconds,
            include_codex=args.codex,
            codex_bin=args.codex_bin,
            goals_root=args.goals_root,
            queue_dir=args.queue_dir,
            policy=policy,
        )
    except (CodexSnapshotError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-rate-limit-guard: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
