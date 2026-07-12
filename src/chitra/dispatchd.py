"""dispatchd — deterministic daemon that drains a JSON order queue and
delivers each order via ``chitra.dispatch.dispatch_to_tmux``, enforcing the
single-writer rule via ``LaneLock``.

Queue layout (default ``queue_dir``, overridable per call/CLI):

    queue_dir/orders/*.json      -- DispatchOrder JSON, one file per order
    queue_dir/in_flight/*.json   -- an order file a worker has atomically
                                     claimed and is currently delivering
    queue_dir/deferred/*.json    -- an order parked because its session is
                                     rate-limit-held (see below); no result
                                     file exists for it yet
    queue_dir/results/<id>.json  -- DispatchResult JSON, written after processing
    queue_dir/processed/*.json   -- the order file, moved here after processing

Crash-safety:

- **Idempotent redelivery.** Once a result file exists for an order id, that
  order is never redispatched -- ``process_one_order`` checks for an
  existing result file (both before and again immediately after acquiring
  the lane lock -- see "Lane-lock recheck" below) and, if found, moves the
  order aside without re-dispatching.
- **Atomic claim.** Before anything else, an order file is atomically
  renamed from ``orders/`` into ``in_flight/``. Two dispatchd workers (or
  two overlapping ``run_once`` passes) racing the same ``orders/`` glob can
  each only rename it once; the loser sees ``FileNotFoundError`` and simply
  skips it -- this is a real filesystem-level mutual-exclusion primitive,
  not a check-then-act race. See docs/SOL-ADVERSARIAL-REVIEW finding #5.
- **Send-nonce crash reconciliation.** The one gap atomic claim + lane lock
  cannot close on their own: a worker that dies *after* the pane paste
  actually lands but *before* ``_write_result_atomic`` runs leaves an order
  in ``in_flight/`` with no result. A naive restart would redispatch it --
  a real duplicate paste into a live pane. Before calling
  ``dispatch_to_tmux``, this module writes a small nonce marker file next to
  the claimed order in ``in_flight/``. If a later pass finds that marker
  already present for an order with no result, it does not blindly resend:
  it reconciles by grepping the target session's own transcript for the
  order's nudge marker (the same transcript-grep primitive
  ``dispatch_to_tmux`` itself uses to confirm delivery) -- if the transcript
  confirms delivery already happened, a ``SENT`` result is synthesized with
  no second paste; only if the transcript does NOT confirm delivery does it
  proceed to (re)dispatch.

Rate-limit freeze and deferral (opt-in via ``goals_root``): immediately
before any delivery attempt -- **under the lane lock**, not before it (see
"TOCTOU" below) -- ``process_one_order`` checks whether the order's
``session_ref`` currently has a ``chitra.goals`` record held for a
rate-limit reason (``hold_reason`` starting with
``chitra.goals.RATE_LIMIT_HOLD_REASON_PREFIX``, set by
``chitra.rate_limit_guard``). If so, the order is atomically parked in
``deferred/`` -- no pane I/O, no result file written, so it is neither
delivered nor discarded. ``chitra.rate_limit_guard.apply_resume`` calls
``requeue_deferred_for_session`` once the hold actually clears, which
atomically returns every deferred order for that session to ``orders/`` in
its original FIFO arrival order (renaming a file never changes its mtime,
so ``run_once``'s FIFO-by-mtime glob sort naturally preserves it) --
each is then delivered exactly once by the same crash-safe idempotency
check every other order already relies on.

TOCTOU: the freeze check reads and acts under the SAME lane-lock hold used
for delivery, so a rate-limit hold that lands after the check and before a
paste (the classic time-of-check/time-of-use race) cannot slip an ordinary
order into a newly-frozen lane -- there is no window between "checked" and
"pasted" for the hold to appear in. See docs/SOL-ADVERSARIAL-REVIEW finding #7.

``DispatchOrder.bypass_rate_limit_freeze`` exempts
``chitra.rate_limit_guard``'s own checkpoint/stop/re-arm nudges from this
freeze, since they are the pause/resume mechanism itself. Setting that
boolean is not, by itself, sufficient to bypass the freeze: dispatchd only
honors it when the order's ``task_type`` is also one of its own sealed
internal task types (``_RATE_LIMIT_GUARD_TASK_TYPES``) -- an arbitrary queue
writer cannot invent a new bypass merely by setting the field, because
dispatchd (not the order) owns the allowlist.

No LLM calls in this module's own code path -- it delivers orders to LLM-
driven sessions, but the content/timing/target of every order is decided by
the caller before it reaches this module; this module is deterministic
plumbing only -- including the optional completion-claim audit
(``chitra.completion_gate``) run in ``process_one_order`` before delivery,
which is itself pure keyword/field matching, not reasoning. See
``docs/evasion-taxonomy.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

import structlog

from . import ledger as ledger_mod
from .completion_gate import evaluate_completion_claim
from .dispatch import (
    DISPATCH_VERIFY_WAIT_SECONDS,
    DispatchOrder,
    DispatchResult,
    DispatchStatus,
    DispatchTuning,
    LaneLock,
    LaneLockError,
    TmuxRunner,
    dispatch_to_tmux,
    nudge_confirmation_marker,
    transcript_confirms_nudge,
)
from .goals import RATE_LIMIT_HOLD_REASON_PREFIX, get_goal
from .policy_config import PolicyConfig, load_policy_config
from .routing_config import RoutingConfig, load_routing_config, resolve_route, resolve_routing_hint
from .state_paths import default_ledger_key_path, default_ledger_path, default_queue_dir
from .taxonomy import load_taxonomy

logger = structlog.get_logger(__name__)

DEFAULT_POLL_SECONDS = 1.0

# Sealed allowlist: the only task_types dispatchd itself will honor a
# caller-set bypass_rate_limit_freeze=True for. Owned here, not by the
# order -- see this module's docstring.
_RATE_LIMIT_GUARD_TASK_TYPES = frozenset({"rate-limit-checkpoint", "rate-limit-stop", "rate-limit-resume"})


def _ensure_queue_dirs(queue_dir: Path) -> tuple[Path, Path, Path]:
    orders = queue_dir / "orders"
    results = queue_dir / "results"
    processed = queue_dir / "processed"
    for d in (orders, results, processed, queue_dir / "in_flight", queue_dir / "deferred"):
        d.mkdir(parents=True, exist_ok=True)
    return orders, results, processed


def _write_result_atomic(results_dir: Path, result: DispatchResult) -> Path:
    """Write a result JSON atomically (write to temp, rename)."""
    target = results_dir / f"{result.order_id}.json"
    tmp = results_dir / f".{result.order_id}.json.tmp"
    tmp.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _reclaim_stale_in_flight(queue_dir: Path) -> None:
    """Return an orphaned ``in_flight/`` order to ``orders/`` for reclaiming.

    Mirrors ``chitra.dispatch.LaneLock``'s own stale-lock reclaim: every
    successful claim writes an owner marker (this process's pid) next to the
    claimed order file; a claim whose owner pid is no longer alive was
    abandoned by a crashed worker and is safe to return to ``orders/`` for a
    fresh claim. A claim whose owner is still alive is a real
    currently-in-progress delivery and is never touched -- this must never
    steal a claim out from under a live worker. Called at the top of every
    ``run_once`` pass so a crash between claiming an order and writing its
    result is always eventually retried, never stranded. See
    docs/SOL-ADVERSARIAL-REVIEW findings #2 and #5.
    """
    in_flight_dir = queue_dir / "in_flight"
    orders_dir = queue_dir / "orders"
    if not in_flight_dir.is_dir():
        return
    for claimed in in_flight_dir.glob("*.json"):
        owner_path = in_flight_dir / f".{claimed.stem}.owner"
        try:
            pid = int(owner_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0  # no/corrupt owner marker -- treat as abandoned, safe to reclaim
        if pid and _pid_alive(pid):
            continue
        logger.warning("dispatchd_reclaiming_stale_in_flight_order", path=str(claimed), owner_pid=pid)
        with contextlib.suppress(OSError):
            claimed.replace(orders_dir / claimed.name)
        with contextlib.suppress(OSError):
            owner_path.unlink()


def requeue_deferred_for_session(queue_dir: Path, session_ref: str) -> list[str]:
    """Atomically return one session's deferred backlog to ``orders/`` FIFO.

    Called once a rate-limit hold on ``session_ref`` actually clears (see
    ``chitra.rate_limit_guard.apply_resume``). A deferred order has no
    result file (see ``process_one_order``'s freeze/defer branch), so moving
    it back to ``orders/`` lets the ordinary crash-safe idempotency check
    deliver it exactly once. Returns the requeued order ids in the order
    they are requeued (their original arrival order, oldest first).
    """
    orders_dir, _, _ = _ensure_queue_dirs(queue_dir)
    deferred_dir = queue_dir / "deferred"
    dated: list[tuple[float, Path]] = []
    for path in deferred_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("session_ref") != session_ref:
            continue
        try:
            dated.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue
    dated.sort(key=lambda item: item[0])
    requeued: list[str] = []
    for _, path in dated:
        target = orders_dir / path.name
        try:
            path.replace(target)
        except OSError:
            logger.warning("dispatchd_deferred_requeue_failed", session_ref=session_ref, path=str(path))
            continue
        requeued.append(path.stem)
    if requeued:
        logger.info("dispatchd_deferred_requeued", session_ref=session_ref, order_ids=requeued)
    return requeued


def process_one_order(
    order_path: Path,
    *,
    orders_dir: Path,
    results_dir: Path,
    processed_dir: Path,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    routing_config: RoutingConfig | None = None,
    policy: PolicyConfig | None = None,
    invalid_dir: Path | None = None,
    tuning: DispatchTuning | None = None,
    goals_root: Path | None = None,
    dispatch_runner: TmuxRunner | None = None,
    projects_root: Path | None = None,
    local_extra: set[str] | None = None,
) -> DispatchResult | None:
    """Process a single order file. Returns the result, or None if skipped
    (already processed, claimed elsewhere, or deferred by the rate-limit freeze).

    Crash-safe: if a result file already exists for this order id, the order
    is considered already processed — it is moved to ``processed/`` without
    re-dispatching, and None is returned (no duplicate delivery).

    ``routing_config``, if given, maps ``task_type`` to a routing selection
    (see ``chitra.routing_config``). If the order's ``routing_hint`` is not
    already set AND the order has a ``task_type``, the config is consulted
    before dispatch: a structured ``routes`` entry is RESOLVED to a concrete
    model+harness (+zdr) — recorded, with ``"route"`` provenance, on the
    result and signed ledger entry — otherwise a flat ``defaults`` entry
    fills in the opaque ``routing_hint`` (``"config"`` provenance). An
    explicit ``routing_hint`` from the caller always wins and skips this
    lookup entirely.

    ``goals_root`` selects the ``chitra.goals`` store consulted for the
    rate-limit freeze/defer check documented in this module's docstring
    (``None`` resolves to the default goals store, exactly like every other
    unset path in this function). A session with no goal record, or one
    held for any reason other than a rate-limit pause, is never frozen.

    ``dispatch_runner``/``projects_root``/``local_extra`` are optional test
    seams forwarded to both ``dispatch_to_tmux`` and the send-nonce crash
    reconciliation's transcript check (see this module's docstring);
    production callers leave them unset.

    Invalid orders produce a FAILED result using the source filename stem and
    are moved to ``invalid/`` (or ``invalid_dir``) so they cannot be retried
    as ordinary processed work.
    """
    policy = policy or PolicyConfig()
    tuning = tuning or DispatchTuning()
    deferred_dir = orders_dir.parent / "deferred"
    in_flight_dir = orders_dir.parent / "in_flight"
    in_flight_dir.mkdir(parents=True, exist_ok=True)

    # Atomic claim: only one worker/pass can ever rename this exact file out
    # of orders/. The loser sees FileNotFoundError and simply skips it — see
    # this module's docstring.
    claimed_path = in_flight_dir / order_path.name
    try:
        order_path.rename(claimed_path)
    except FileNotFoundError:
        logger.info("dispatchd_order_claimed_elsewhere", path=str(order_path))
        return None
    except OSError as exc:
        logger.error("dispatchd_order_claim_failed", path=str(order_path), error=str(exc))
        return None

    # Owner marker: records which live process holds this claim, so a
    # crashed worker's abandoned claim can be told apart from one still
    # legitimately in progress (see _reclaim_stale_in_flight). Removed
    # unconditionally once this claim is fully resolved, however it resolves.
    owner_path = in_flight_dir / f".{claimed_path.stem}.owner"
    owner_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _process_claimed_order(
            claimed_path,
            results_dir=results_dir,
            processed_dir=processed_dir,
            deferred_dir=deferred_dir,
            in_flight_dir=in_flight_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            routing_config=routing_config,
            policy=policy,
            invalid_dir=invalid_dir,
            tuning=tuning,
            goals_root=goals_root,
            dispatch_runner=dispatch_runner,
            projects_root=projects_root,
            local_extra=local_extra,
        )
    finally:
        with contextlib.suppress(OSError):
            owner_path.unlink()


def _process_claimed_order(
    claimed_path: Path,
    *,
    results_dir: Path,
    processed_dir: Path,
    deferred_dir: Path,
    in_flight_dir: Path,
    lock_dir: Path | None,
    ledger_path: Path | None,
    ledger_key_path: Path | None,
    routing_config: RoutingConfig | None,
    policy: PolicyConfig,
    invalid_dir: Path | None,
    tuning: DispatchTuning,
    goals_root: Path | None,
    dispatch_runner: TmuxRunner | None,
    projects_root: Path | None,
    local_extra: set[str] | None,
) -> DispatchResult | None:
    """The rest of order processing, once an order file is safely claimed
    (renamed into ``in_flight/`` with a live owner marker). Split out of
    ``process_one_order`` only so the owner-marker cleanup above can wrap it
    in one ``finally`` regardless of which of this function's many return
    points is taken.
    """
    try:
        order = DispatchOrder.model_validate_json(claimed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("dispatchd_order_unreadable", path=str(claimed_path), error=str(exc))
        result = DispatchResult(
            order_id=claimed_path.stem,
            session_ref="",
            status=DispatchStatus.FAILED,
            reason=f"invalid-order: {exc}",
        )
        _write_result_atomic(results_dir, result)
        destination = invalid_dir or processed_dir.parent / "invalid"
        destination.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            claimed_path.replace(destination / claimed_path.name)
        return result

    routing_hint_source = "explicit" if order.routing_hint is not None else "unset"
    resolved_model: str | None = None
    resolved_harness: str | None = None
    resolved_zdr = False
    if order.routing_hint is None and order.task_type is not None:
        # A structured ``routes`` entry wins over a flat ``defaults`` hint:
        # chitra RESOLVES model+harness (+zdr) and records the resolved
        # selection + "route" provenance, closing the ROADMAP line-97 gap.
        route = resolve_route(order.task_type, routing_config)
        if route is not None:
            order.routing_hint = route.routing_hint
            resolved_model = route.model
            resolved_harness = route.harness
            resolved_zdr = route.zdr
            routing_hint_source = "route"
        else:
            resolved_hint = resolve_routing_hint(order.task_type, routing_config)
            if resolved_hint is not None:
                order.routing_hint = resolved_hint
                routing_hint_source = "config"

    existing_result = results_dir / f"{order.order_id}.json"
    if existing_result.exists():
        logger.info("dispatchd_order_already_processed", order_id=order.order_id)
        processed_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            claimed_path.replace(processed_dir / claimed_path.name)
        return None

    # Completion-claim audit: opt-in via completion_todo_items being set (see
    # DispatchOrder's docstring). A disputed claim is never delivered as an
    # ordinary "sent" nudge -- it is surfaced as its own distinct status and
    # the tmux paste never happens. A clean claim proceeds to normal
    # dispatch below; the CLEAN audit itself (logged) is the proof an
    # operator can use to authorize a close -- this daemon never closes
    # anything itself, only classifies and surfaces.
    if order.completion_todo_items is not None:
        audit = evaluate_completion_claim(
            order.completion_todo_items,
            order.nudge,
            order.completion_has_deploy_evidence,
            order.completion_has_live_verify_evidence,
            load_taxonomy(policy.completion_gate.taxonomy_path),
            policy=policy.completion_gate,
        )
        if audit.verdict == "COMPLETION_DISPUTE":
            logger.warning(
                "dispatchd_completion_dispute",
                order_id=order.order_id,
                session_ref=order.session_ref,
                summary=audit.summary,
            )
            result = DispatchResult(
                order_id=order.order_id,
                session_ref=order.session_ref,
                status=DispatchStatus.COMPLETION_DISPUTE,
                reason=audit.summary,
                routing_hint=order.routing_hint,
                task_type=order.task_type,
                routing_hint_source=routing_hint_source,
                resolved_model=resolved_model,
                resolved_harness=resolved_harness,
                resolved_zdr=resolved_zdr,
            )
            _write_result_atomic(results_dir, result)
            processed_dir.mkdir(parents=True, exist_ok=True)
            claimed_path.replace(processed_dir / claimed_path.name)
            return result
        logger.info(
            "dispatchd_completion_clean",
            order_id=order.order_id,
            session_ref=order.session_ref,
            summary=audit.summary,
        )

    lock = LaneLock(order.session_ref, lock_dir=lock_dir)
    try:
        lock.acquire(blocking=True, timeout_seconds=tuning.lane_lock_timeout_seconds)
    except LaneLockError as exc:
        logger.warning("dispatchd_lane_lock_failed", order_id=order.order_id, session_ref=order.session_ref, error=str(exc))
        result = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            task_type=order.task_type,
            routing_hint_source=routing_hint_source,
            resolved_model=resolved_model,
            resolved_harness=resolved_harness,
            resolved_zdr=resolved_zdr,
            status=DispatchStatus.BLOCKED,
            reason=f"lane lock unavailable: {exc}",
        )
        _write_result_atomic(results_dir, result)
        claimed_path.replace(processed_dir / claimed_path.name)
        return result

    try:
        # Lane-lock recheck: a concurrent order for the same session could
        # have completed and written a result while this order waited on
        # the lock. See docs/SOL-ADVERSARIAL-REVIEW finding #5.
        if existing_result.exists():
            logger.info("dispatchd_order_already_processed_under_lock", order_id=order.order_id)
            processed_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                claimed_path.replace(processed_dir / claimed_path.name)
            return None

        # Rate-limit freeze/defer check, UNDER the lane lock (TOCTOU fix --
        # see this module's docstring). bypass_rate_limit_freeze only takes
        # effect for dispatchd's own sealed internal task types.
        allowed_bypass = order.bypass_rate_limit_freeze and order.task_type in _RATE_LIMIT_GUARD_TASK_TYPES
        held = None if allowed_bypass else get_goal(goals_root, order.session_ref)
        if held is not None and held.status == "held" and held.hold_reason.startswith(RATE_LIMIT_HOLD_REASON_PREFIX):
            logger.info(
                "dispatchd_order_deferred_rate_limit_freeze",
                order_id=order.order_id,
                session_ref=order.session_ref,
                hold_reason=held.hold_reason,
                resume_at=held.resume_at,
            )
            deferred_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                claimed_path.replace(deferred_dir / claimed_path.name)
            return DispatchResult(
                order_id=order.order_id,
                session_ref=order.session_ref,
                status=DispatchStatus.DEFERRED,
                reason=f"rate-limit-deferred: {held.hold_reason} (resume_at={held.resume_at})",
                routing_hint=order.routing_hint,
                task_type=order.task_type,
                routing_hint_source=routing_hint_source,
                resolved_model=resolved_model,
                resolved_harness=resolved_harness,
                resolved_zdr=resolved_zdr,
            )

        # Send-nonce crash reconciliation: a marker already present here
        # means a PRIOR attempt got at least as far as (about to) paste
        # before this process/run restarted. Reconcile against the target
        # transcript before ever pasting a second time. See this module's
        # docstring.
        nonce_path = in_flight_dir / f".{order.order_id}.nonce"
        dispatch_result: DispatchResult | None = None
        if nonce_path.exists():
            logger.warning(
                "dispatchd_order_reconciling_after_possible_crash", order_id=order.order_id, session_ref=order.session_ref
            )
            parts = order.session_ref.split(":")
            host = parts[0] if len(parts) == 3 else ""
            confirmed, transcript_path = transcript_confirms_nudge(
                order.nudge,
                host=host,
                projects_root=projects_root,
                recency_seconds=tuning.transcript_recency_seconds,
                runner=dispatch_runner,
                local_extra=local_extra,
            )
            if confirmed:
                dispatch_result = DispatchResult(
                    order_id=order.order_id,
                    session_ref=order.session_ref,
                    status=DispatchStatus.SENT,
                    reason="sent: reconciled from a prior crashed delivery attempt (transcript confirms nudge)",
                    marker=nudge_confirmation_marker(order.nudge),
                    transcript_path=str(transcript_path) if transcript_path is not None else None,
                )
        if dispatch_result is None:
            nonce_path.write_text(uuid.uuid4().hex, encoding="utf-8")
            dispatch_result = dispatch_to_tmux(
                order, policy=policy, tuning=tuning, runner=dispatch_runner, projects_root=projects_root, local_extra=local_extra
            )
        result = dispatch_result
    finally:
        lock.release()

    result.task_type = order.task_type
    result.routing_hint_source = routing_hint_source
    result.routing_hint = order.routing_hint
    result.resolved_model = resolved_model
    result.resolved_harness = resolved_harness
    result.resolved_zdr = resolved_zdr
    logger.info(
        "dispatchd_order_processed",
        order_id=order.order_id,
        session_ref=order.session_ref,
        status=result.status.value,
    )
    if result.status == DispatchStatus.SENT:
        # Sign and log automatically on every successful delivery — no
        # extra step for the caller, no added friction to a normal send.
        #
        # Crash-safety: this MUST NOT be able to cause redelivery. The
        # dispatch already happened and the lock is already released; if
        # the ledger write itself failed here uncaught, the order would
        # still be sitting in in_flight/ with no result file on the next
        # pass, so process_one_order would reconcile via the send-nonce
        # transcript check above rather than blindly redispatching. A
        # ledger failure therefore only costs the proof-of-delivery record
        # for this one message.
        try:
            key = ledger_mod.load_or_create_signing_key(ledger_key_path or default_ledger_key_path())
            ledger_mod.append_entry(
                ledger_path or default_ledger_path(),
                order_id=order.order_id,
                session_ref=order.session_ref,
                tag=order.tag,
                routing_hint=order.routing_hint,
                task_type=order.task_type,
                routing_hint_source=routing_hint_source,
                resolved_model=resolved_model,
                resolved_harness=resolved_harness,
                resolved_zdr=resolved_zdr,
                nudge=order.nudge,
                key=key,
            )
        except Exception as exc:  # noqa: BLE001 -- deliberate, narrow exception to the crash-safety
            # contract above: any failure signing/appending the ledger is logged and swallowed
            # here specifically because letting it propagate would break the tested guarantee
            # that a fully-completed dispatch is never redelivered. This is the one place in the
            # package where fail-loud is overridden, and only for this one documented reason.
            logger.warning("dispatchd_ledger_write_failed", order_id=order.order_id, session_ref=order.session_ref, error=str(exc))
    _write_result_atomic(results_dir, result)
    claimed_path.replace(processed_dir / claimed_path.name)
    with contextlib.suppress(OSError):
        (in_flight_dir / f".{order.order_id}.nonce").unlink()
    return result


def run_once(
    queue_dir: Path | None = None,
    *,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    invalid_dir: Path | None = None,
    tuning: DispatchTuning | None = None,
    goals_root: Path | None = None,
    dispatch_runner: TmuxRunner | None = None,
    projects_root: Path | None = None,
    local_extra: set[str] | None = None,
) -> list[DispatchResult]:
    """Process every pending order in ``queue_dir/orders`` once, FIFO by mtime.

    ``routing_config_path`` (or the ``CHITRA_ROUTING_CONFIG`` env var if
    unset) is loaded once per call and passed to every ``process_one_order``
    invocation — see ``chitra.routing_config`` for the lookup semantics.

    ``goals_root`` is forwarded to ``process_one_order``'s rate-limit
    freeze/defer check on every order (see that function's docstring).
    """
    queue_dir = queue_dir or default_queue_dir()
    orders_dir, results_dir, processed_dir = _ensure_queue_dirs(queue_dir)
    _reclaim_stale_in_flight(queue_dir)
    routing_config = load_routing_config(routing_config_path)
    policy = load_policy_config(policy_config_path)
    dated: list[tuple[float, Path]] = []
    for order_path in orders_dir.glob("*.json"):
        try:
            dated.append((order_path.stat().st_mtime, order_path))
        except FileNotFoundError:
            # Order file vanished between the glob and the stat (e.g. raced
            # by something else touching the queue dir). Skip it rather than
            # letting the stat's exception kill run_forever's loop.
            logger.warning("dispatchd_order_vanished_before_stat", path=str(order_path))
    pending = [p for _, p in sorted(dated, key=lambda t: t[0])]
    out: list[DispatchResult] = []
    for order_path in pending:
        result = process_one_order(
            order_path,
            orders_dir=orders_dir,
            results_dir=results_dir,
            processed_dir=processed_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            routing_config=routing_config,
            policy=policy,
            invalid_dir=invalid_dir,
            tuning=tuning,
            goals_root=goals_root,
            dispatch_runner=dispatch_runner,
            projects_root=projects_root,
            local_extra=local_extra,
        )
        if result is not None:
            out.append(result)
    return out


def run_forever(
    queue_dir: Path | None = None,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    routing_config_path: Path | None = None,
    policy_config_path: Path | None = None,
    invalid_dir: Path | None = None,
    tuning: DispatchTuning | None = None,
    goals_root: Path | None = None,
) -> None:
    """Run the daemon loop: drain the queue, sleep, repeat. Runs until killed."""
    queue_dir = queue_dir or default_queue_dir()
    logger.info("dispatchd_started", queue_dir=str(queue_dir), poll_seconds=poll_seconds)
    while True:
        run_once(
            queue_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            routing_config_path=routing_config_path,
            policy_config_path=policy_config_path,
            invalid_dir=invalid_dir,
            tuning=tuning,
            goals_root=goals_root,
        )
        time.sleep(poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatchd", description="Deterministic tmux dispatch daemon (chitra phase 1).")
    parser.add_argument("--queue-dir", type=Path, default=None, help="Order/result/processed queue root (default: CHITRA_STATE_DIR/queue).")
    parser.add_argument(
        "--lock-dir",
        type=Path,
        default=None,
        help="LaneLock directory (env CHITRA_LANE_LOCK_DIR, else a dir under the system temp dir).",
    )
    parser.add_argument("--ledger-path", type=Path, default=None, help="Delivery ledger JSONL path (default: next to the state dir).")
    parser.add_argument("--ledger-key-path", type=Path, default=None, help="HMAC signing key path (generated on first use if missing).")
    parser.add_argument(
        "--routing-config-path",
        type=Path,
        default=None,
        help="Path to a routing.yaml task_type->routing_hint lookup (env CHITRA_ROUTING_CONFIG, else no config/no-op).",
    )
    parser.add_argument(
        "--policy-config-path",
        type=Path,
        default=None,
        help="Path to policy.yaml (env CHITRA_POLICY_CONFIG, else shipped defaults).",
    )
    parser.add_argument("--invalid-orders-dir", type=Path, default=None, help="Invalid-order directory (default: <queue-dir>/invalid).")
    parser.add_argument(
        "--goals-root",
        type=Path,
        default=None,
        help="chitra.goals store root consulted for the rate-limit freeze check (default: CHITRA_STATE_DIR).",
    )
    parser.add_argument("--capture-lines", type=int, default=12)
    parser.add_argument("--post-paste-wait-seconds", type=float, default=DISPATCH_VERIFY_WAIT_SECONDS)
    parser.add_argument("--transcript-recency-seconds", type=float, default=300.0)
    parser.add_argument("--lane-lock-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Drain the queue once and exit (for tests/cron), instead of looping forever.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    queue_dir = args.queue_dir or default_queue_dir()
    tuning = DispatchTuning(
        capture_lines=args.capture_lines,
        post_paste_wait_seconds=args.post_paste_wait_seconds,
        transcript_recency_seconds=args.transcript_recency_seconds,
        lane_lock_timeout_seconds=args.lane_lock_timeout_seconds,
    )
    if args.once:
        results = run_once(
            queue_dir,
            lock_dir=args.lock_dir,
            ledger_path=args.ledger_path,
            ledger_key_path=args.ledger_key_path,
            routing_config_path=args.routing_config_path,
            policy_config_path=args.policy_config_path,
            invalid_dir=args.invalid_orders_dir,
            tuning=tuning,
            goals_root=args.goals_root,
        )
        print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return 0
    run_forever(
        queue_dir,
        poll_seconds=args.poll_seconds,
        lock_dir=args.lock_dir,
        ledger_path=args.ledger_path,
        ledger_key_path=args.ledger_key_path,
        routing_config_path=args.routing_config_path,
        policy_config_path=args.policy_config_path,
        invalid_dir=args.invalid_orders_dir,
        tuning=tuning,
        goals_root=args.goals_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
