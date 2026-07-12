"""rate_limit_guard — one-shot detect/checkpoint/pause/resume sweep for
sessions nearing their provider rate/session limits.

This is deterministic composition of three already-existing, already-tested
chitra primitives -- not a new decision-making layer:

- ``chitra.usage`` (fact): is a session's account near or over its
  rate-limit thresholds? Pure threshold evaluation, unchanged by this module.
- ``chitra.goals`` (bookkeeping): hold/resume/due a lane's per-session goal
  record, preserving the goal itself as the re-arm payload. Unchanged by
  this module.
- ``chitra.dispatchd``'s JSON order queue (mechanism): the two nudges this
  module sends -- a fixed checkpoint instruction, and a resume nudge built
  only from the lane's OWN already-stored ``goal``/``done_when`` fields --
  are canned literal templates, never LLM-authored, and are handed to the
  existing queue for the already-running ``dispatchd`` daemon to deliver,
  exactly like every other caller's orders (see the README's "QUEUE-PRIMARY"
  convention). This module never touches a tmux pane directly.

No LLM call anywhere in this module's own code path, matching the whole
package's scope test (see ``docs/ROADMAP.md``: "chitra makes zero dispatch
decisions"). Every branch below is a pure threshold/rule composition of
already-recorded, already-evaluated data.

Session-ref resolution (a documented assumption, not a verified fact -- see
this repo's PR description for the operator to confirm or correct): a usage
snapshot's ``tmux_session`` field, as written by the deployed
``chitra-usage-snapshot`` sidecar, is the BARE tmux session name (e.g.
``#{session_name}``), not a full ``host:session:pane`` session_ref. This
module assumes the fleet convention documented elsewhere in chitra (one
tracked lane = one dedicated tmux session, its primary work in pane
``0.0``) and resolves ``session_ref = f"{host}:{tmux_session}:0.0"``
directly, with no live tmux enumeration. A deployment where a tracked lane
does not occupy pane ``0.0`` of its own session needs a different resolver;
this module does not attempt to discover that live, to keep a fast sweep
tool free of a tmux/ssh dependency of its own.

Why a one-shot sweep, not a daemon: chitra's own design record (docs/
ROADMAP.md, and the design note this module's PR carries forward) explicitly
rules out a new always-on daemon for this. ``sweep()`` is meant to run under
an external timer (systemd timer / cron), exactly like ``chitra.draft_
scanner``'s periodic-scan shape -- re-running it every few minutes is what
makes "resume it automatically after reset" work, with no long-lived process
of its own.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import structlog

from .dispatch import DispatchOrder
from .goals import RATE_LIMIT_HOLD_REASON_PREFIX, GoalRecord, due_goals, get_goal, hold_goal, resume_goal
from .policy_config import PolicyConfig, UsagePolicy, load_policy_config
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


def _resume_nudge(record: GoalRecord) -> str:
    """Build the re-arm nudge from a lane's OWN stored fields -- no LLM authorship."""
    return f"Rate-limit window has reset -- resuming. Goal: {record.goal} Done when: {record.done_when}"


def _hold_reason_for(verdict: AccountedVerdict) -> str:
    return f"{RATE_LIMIT_HOLD_REASON_PREFIX}{verdict.binding_window}"


def _resume_at_iso(resume_at_epoch: int) -> str:
    return datetime.fromtimestamp(resume_at_epoch, UTC).isoformat()


def _session_ref_for(verdict: AccountedVerdict, *, host: str) -> str | None:
    """Resolve a session_ref from one account-grouped verdict, or None.

    See this module's docstring for the pane-0.0 assumption. A verdict with
    no ``tmux_session`` (the synthetic Codex account-wide probe entry chitra.
    usage.codex_snapshot emits) cannot be mapped to a specific pane and is
    skipped -- per-lane Codex pause requires per-lane Codex usage snapshots,
    which is a known, separately-tracked gap, not something this function can
    infer.
    """
    if not verdict.tmux_session:
        return None
    return f"{host}:{verdict.tmux_session}:0.0"


@dataclass(frozen=True, slots=True)
class PauseOutcome:
    """One session paused this sweep: the hold is always persisted; the
    checkpoint nudge's queue outcome is recorded for visibility, never
    silently swallowed."""

    session_ref: str
    binding_window: Literal["", "5h", "7d"]
    resume_at_epoch: int
    checkpoint_order_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "session_ref": self.session_ref,
            "binding_window": self.binding_window,
            "resume_at_epoch": self.resume_at_epoch,
            "resume_at_iso": _resume_at_iso(self.resume_at_epoch),
            "checkpoint_order_id": self.checkpoint_order_id,
        }


@dataclass(frozen=True, slots=True)
class ResumeOutcome:
    """One session resumed this sweep: the hold is cleared before the re-arm
    nudge is enqueued, so the freeze this same lane was under is already
    lifted by the time dispatchd drains that nudge."""

    session_ref: str
    resume_order_id: str

    def to_dict(self) -> dict[str, object]:
        return {"session_ref": self.session_ref, "resume_order_id": self.resume_order_id}


@dataclass(slots=True)
class SweepReport:
    """The full result of one sweep pass, in the order this module's
    docstring lists the lifecycle: detect (implicit, via chitra.usage) ->
    paused -> resumed -> skipped/escalations for visibility."""

    paused: list[PauseOutcome] = field(default_factory=list)
    resumed: list[ResumeOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "paused": [item.to_dict() for item in self.paused],
            "resumed": [item.to_dict() for item in self.resumed],
            "skipped": self.skipped,
            "escalations": self.escalations,
        }


def plan_pauses(
    verdicts: list[AccountedVerdict], *, host: str, goals_root: Path | None
) -> tuple[list[AccountedVerdict], list[str]]:
    """Pure planning pass: which fresh 'pause'-level verdicts need a NEW
    checkpoint+hold action right now. Returns ``(to_pause, skip_reasons)``.

    A session with no chitra goal record is not tracked by the goal store --
    skipped, not an error. A session already held for THIS exact rate-limit
    window is idempotently skipped (no repeat checkpoint spam every sweep).
    A session held for any OTHER reason (operator, throttle) is never
    silently overridden by a rate-limit hold.
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
        existing = get_goal(goals_root, session_ref)
        if existing is None:
            skipped.append(f"{session_ref}: no chitra goal record -- not tracked, nothing to pause")
            continue
        hold_reason = _hold_reason_for(verdict)
        resume_at_iso = _resume_at_iso(verdict.resume_at_epoch)
        if existing.status == "held" and existing.hold_reason == hold_reason and existing.resume_at == resume_at_iso:
            continue  # already paused for this exact window -- idempotent no-op
        if existing.status == "held" and not existing.hold_reason.startswith(RATE_LIMIT_HOLD_REASON_PREFIX):
            skipped.append(f"{session_ref}: already held for a non-rate-limit reason ({existing.hold_reason!r}) -- not overriding")
            continue
        to_pause.append(verdict)
    return to_pause, skipped


def apply_pause(
    verdict: AccountedVerdict,
    *,
    host: str,
    goals_root: Path | None,
    queue_dir: Path,
    now: datetime,
) -> PauseOutcome:
    """Enqueue the checkpoint nudge and persist the hold for one session
    already selected by ``plan_pauses``.

    The hold always lands, unconditionally -- the rate-limit fact driving
    this is external and real regardless of whether the checkpoint nudge is
    ever confirmed delivered (dispatchd, not this function, owns delivery
    verification and will record that outcome on the order's own result
    file). Order matters: the hold is persisted FIRST so the freeze is active
    immediately, then the checkpoint nudge is enqueued with
    ``bypass_rate_limit_freeze=True`` so it is never blocked by the freeze it
    just created.
    """
    session_ref = _session_ref_for(verdict, host=host)
    assert session_ref is not None  # plan_pauses already filtered
    hold_reason = _hold_reason_for(verdict)
    resume_at_iso = _resume_at_iso(verdict.resume_at_epoch)
    hold_goal(goals_root, session_ref, reason=hold_reason, resume_at=resume_at_iso)
    order_id = f"rate-limit-checkpoint-{uuid.uuid4().hex[:12]}"
    _enqueue(
        queue_dir,
        DispatchOrder(
            order_id=order_id,
            session_ref=session_ref,
            nudge=CHECKPOINT_NUDGE,
            task_type="rate-limit-checkpoint",
            bypass_rate_limit_freeze=True,
            created_at=now.isoformat(),
        ),
    )
    logger.info(
        "rate_limit_guard_paused",
        session_ref=session_ref,
        hold_reason=hold_reason,
        resume_at=resume_at_iso,
        checkpoint_order_id=order_id,
    )
    return PauseOutcome(
        session_ref=session_ref,
        binding_window=verdict.binding_window,
        resume_at_epoch=verdict.resume_at_epoch,
        checkpoint_order_id=order_id,
    )


def plan_resumes(
    *, goals_root: Path | None, verdicts: list[AccountedVerdict], policy: UsagePolicy, now: datetime
) -> tuple[list[GoalRecord], list[str]]:
    """Pure planning pass over due rate-limit holds.

    Confirms the fresh verdict is back to ``ok`` before resuming -- never
    resume into a still-hot window. A due hold with no matching fresh verdict
    (session no longer emitting usage snapshots, or a transient read gap) is
    left held for a later sweep to retry -- fail quiet, matching chitra.
    usage's own "stale/missing = unknown -> no action" convention. Respects
    ``policy.auto_resume``: when False, a due lane is surfaced as an
    escalation instead of being auto-resumed, with no goal-state mutation.
    """
    by_ref = {verdict.tmux_session: verdict for verdict in verdicts if verdict.tmux_session}
    to_resume: list[GoalRecord] = []
    escalations: list[str] = []
    for record in due_goals(goals_root, now=now):
        if not record.hold_reason.startswith(RATE_LIMIT_HOLD_REASON_PREFIX):
            continue  # operator/throttle holds are never auto-resumed by this module
        # record.session_ref is host:session:pane; the verdict lookup key is
        # the bare tmux_session this module derived it from (host:X:0.0 -> X).
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


def apply_resume(record: GoalRecord, *, goals_root: Path | None, queue_dir: Path, now: datetime) -> ResumeOutcome:
    """Clear the hold FIRST (so this lane's own freeze lifts before its
    re-arm nudge is attempted), then enqueue a re-arm nudge built only from
    the lane's own stored ``goal``/``done_when`` fields."""
    resume_goal(goals_root, record.session_ref)
    order_id = f"rate-limit-resume-{uuid.uuid4().hex[:12]}"
    _enqueue(
        queue_dir,
        DispatchOrder(
            order_id=order_id,
            session_ref=record.session_ref,
            nudge=_resume_nudge(record),
            task_type="rate-limit-resume",
            bypass_rate_limit_freeze=True,
            created_at=now.isoformat(),
        ),
    )
    logger.info("rate_limit_guard_resumed", session_ref=record.session_ref, resume_order_id=order_id)
    return ResumeOutcome(session_ref=record.session_ref, resume_order_id=order_id)


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
    now: datetime | None = None,
) -> SweepReport:
    """Run one detect -> checkpoint -> pause -> resume pass.

    ``usage_dir`` and ``host`` are one host's worth of usage snapshots at a
    time -- exactly like ``chitra-usage evaluate``'s own single-``--dir``
    shape; a multi-host fleet runs this once per host (see this module's
    docstring on why session_ref resolution needs an explicit ``host``).
    """
    resolved_policy = policy or PolicyConfig()
    current = datetime.now(UTC) if now is None else now
    resolved_queue_dir = queue_dir or default_queue_dir()

    snapshots = read_snapshots(usage_dir, staleness_seconds=staleness_seconds, now=current)
    if include_codex:
        snapshots.append((codex_snapshot(codex_bin=codex_bin, now=current), True))
    verdicts = evaluate_grouped(snapshots, policy=resolved_policy.usage)

    report = SweepReport()

    to_pause, skip_reasons = plan_pauses(verdicts, host=host, goals_root=goals_root)
    report.skipped.extend(skip_reasons)
    for verdict in to_pause:
        report.paused.append(apply_pause(verdict, host=host, goals_root=goals_root, queue_dir=resolved_queue_dir, now=current))

    to_resume, escalations = plan_resumes(goals_root=goals_root, verdicts=verdicts, policy=resolved_policy.usage, now=current)
    report.escalations.extend(escalations)
    for record in to_resume:
        report.resumed.append(apply_resume(record, goals_root=goals_root, queue_dir=resolved_queue_dir, now=current))

    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-rate-limit-guard",
        description="One-shot detect/checkpoint/pause/resume sweep for sessions nearing their provider rate/session limits.",
    )
    parser.add_argument("--usage-dir", type=Path, required=True, help="Directory of chitra.usage.v1 snapshot files for one host.")
    parser.add_argument("--host", required=True, help="The host these snapshots' sessions run on (used to build each session_ref).")
    parser.add_argument("--staleness-seconds", type=int, default=1200)
    parser.add_argument("--codex", action="store_true", help="Also read this host's local Codex account usage.")
    parser.add_argument("--codex-bin", type=Path, default=Path("codex"))
    parser.add_argument("--goals-root", type=Path, default=None, help="chitra.goals store root (default: CHITRA_STATE_DIR).")
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
