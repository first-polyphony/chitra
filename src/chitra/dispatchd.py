"""dispatchd — deterministic daemon that drains a JSON order queue and
delivers each order via ``chitra.dispatch.dispatch_to_tmux``, enforcing the
single-writer rule via ``LaneLock``.

Queue layout (default ``queue_dir``, overridable per call/CLI):

    queue_dir/orders/*.json      -- DispatchOrder JSON, one file per order
    queue_dir/results/<id>.json  -- DispatchResult JSON, written after processing
    queue_dir/processed/*.json   -- the order file, moved here after processing

Crash-safety: once a result file exists for an order id, that order is never
redispatched -- process_one_order checks for an existing result file before
dispatching and, if found, moves the order aside without re-dispatching. The
one real gap this does NOT close: a crash between the paste actually landing
in the target pane and the result file being written leaves the order file
in ``orders/`` with no result file, so the next pass re-dispatches it and the
message is delivered a second time. See ``process_one_order``'s ledger-write
comment below for exactly where that window sits.

No LLM calls in this module's own code path — it delivers orders to LLM-driven
sessions, but the content/timing/target of every order is decided by the
caller before it reaches this module; this module is deterministic plumbing
only -- including the optional completion-claim audit
(``chitra.completion_gate``) run in ``process_one_order`` before delivery,
which is itself pure keyword/field matching, not reasoning. See
``docs/evasion-taxonomy.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import time
from pathlib import Path

import structlog

from . import ledger as ledger_mod
from .completion_gate import evaluate_completion_claim
from .dispatch import (
    DispatchOrder,
    DispatchResult,
    DispatchStatus,
    LaneLock,
    LaneLockError,
    dispatch_to_tmux,
)
from .routing_config import RoutingConfig, load_routing_config, resolve_routing_hint
from .taxonomy import load_taxonomy

logger = structlog.get_logger(__name__)

DEFAULT_QUEUE_DIR = Path("/var/lib/chitra/queue")
DEFAULT_POLL_SECONDS = 1.0


def _ensure_queue_dirs(queue_dir: Path) -> tuple[Path, Path, Path]:
    orders = queue_dir / "orders"
    results = queue_dir / "results"
    processed = queue_dir / "processed"
    for d in (orders, results, processed):
        d.mkdir(parents=True, exist_ok=True)
    return orders, results, processed


def _write_result_atomic(results_dir: Path, result: DispatchResult) -> Path:
    """Write a result JSON atomically (write to temp, rename)."""
    target = results_dir / f"{result.order_id}.json"
    tmp = results_dir / f".{result.order_id}.json.tmp"
    tmp.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


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
) -> DispatchResult | None:
    """Process a single order file. Returns the result, or None if skipped.

    Crash-safe: if a result file already exists for this order id, the order
    is considered already processed — it is moved to ``processed/`` without
    re-dispatching, and None is returned (no duplicate delivery).

    ``routing_config``, if given, is a purely mechanical ``task_type ->
    routing_hint`` lookup table (see ``chitra.routing_config``). If the
    order's ``routing_hint`` is not already set AND the order has a
    ``task_type``, the config is consulted to fill in a default
    ``routing_hint`` before dispatch — an explicit ``routing_hint`` from the
    caller always wins and skips this lookup entirely.

    Known limitation: an order file that is valid-looking JSON but was left
    incomplete by a crashed/killed writer (as opposed to outright malformed
    JSON, e.g. a truncated write) is not distinguishable here from a
    genuinely malformed order -- both raise on ``model_validate_json`` and
    are moved to ``processed/`` with no result file, i.e. the order is
    silently dropped with no redelivery and no retry. This is logged at
    ERROR level specifically so it is not lost among routine warnings.
    """
    try:
        order = DispatchOrder.model_validate_json(order_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # ERROR, not warning: an order is being silently lost here (see the
        # "Known limitation" note above) and this must not blend into
        # routine warning-level noise.
        logger.error("dispatchd_order_unreadable", path=str(order_path), error=str(exc))
        # Move aside so a malformed file doesn't spin the loop forever.
        processed_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            order_path.replace(processed_dir / order_path.name)
        return None

    if order.routing_hint is None and order.task_type is not None:
        order.routing_hint = resolve_routing_hint(order.task_type, routing_config)

    existing_result = results_dir / f"{order.order_id}.json"
    if existing_result.exists():
        logger.info("dispatchd_order_already_processed", order_id=order.order_id)
        processed_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            order_path.replace(processed_dir / order_path.name)
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
            load_taxonomy(),
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
            )
            _write_result_atomic(results_dir, result)
            processed_dir.mkdir(parents=True, exist_ok=True)
            order_path.replace(processed_dir / order_path.name)
            return result
        logger.info(
            "dispatchd_completion_clean",
            order_id=order.order_id,
            session_ref=order.session_ref,
            summary=audit.summary,
        )

    lock = LaneLock(order.session_ref, lock_dir=lock_dir)
    try:
        lock.acquire(blocking=True, timeout_seconds=5.0)
    except LaneLockError as exc:
        logger.warning("dispatchd_lane_lock_failed", order_id=order.order_id, session_ref=order.session_ref, error=str(exc))
        result = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            status=DispatchStatus.BLOCKED,
            reason=f"lane lock unavailable: {exc}",
        )
        _write_result_atomic(results_dir, result)
        order_path.replace(processed_dir / order_path.name)
        return result

    try:
        result = dispatch_to_tmux(order)
    finally:
        lock.release()

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
        # still be sitting in orders/ with no result file on the next pass,
        # so process_one_order would re-run dispatch_to_tmux and paste the
        # same nudge into the live pane a second time. A ledger failure
        # therefore only costs the proof-of-delivery record for this one
        # message -- it can never cause a duplicate send.
        try:
            key = ledger_mod.load_or_create_signing_key(ledger_key_path or ledger_mod.DEFAULT_KEY_PATH)
            ledger_mod.append_entry(
                ledger_path or ledger_mod.DEFAULT_LEDGER_PATH,
                order_id=order.order_id,
                session_ref=order.session_ref,
                tag=order.tag,
                routing_hint=order.routing_hint,
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
    order_path.replace(processed_dir / order_path.name)
    return result


def run_once(
    queue_dir: Path,
    *,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    routing_config_path: Path | None = None,
) -> list[DispatchResult]:
    """Process every pending order in ``queue_dir/orders`` once, FIFO by mtime.

    ``routing_config_path`` (or the ``CHITRA_ROUTING_CONFIG`` env var if
    unset) is loaded once per call and passed to every ``process_one_order``
    invocation — see ``chitra.routing_config`` for the lookup semantics.
    """
    orders_dir, results_dir, processed_dir = _ensure_queue_dirs(queue_dir)
    routing_config = load_routing_config(routing_config_path)
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
        )
        if result is not None:
            out.append(result)
    return out


def run_forever(
    queue_dir: Path,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    lock_dir: Path | None = None,
    ledger_path: Path | None = None,
    ledger_key_path: Path | None = None,
    routing_config_path: Path | None = None,
) -> None:
    """Run the daemon loop: drain the queue, sleep, repeat. Runs until killed."""
    logger.info("dispatchd_started", queue_dir=str(queue_dir), poll_seconds=poll_seconds)
    while True:
        run_once(
            queue_dir,
            lock_dir=lock_dir,
            ledger_path=ledger_path,
            ledger_key_path=ledger_key_path,
            routing_config_path=routing_config_path,
        )
        time.sleep(poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatchd", description="Deterministic tmux dispatch daemon (chitra phase 1).")
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR, help="Order/result/processed queue root.")
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
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Drain the queue once and exit (for tests/cron), instead of looping forever.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.once:
        results = run_once(
            args.queue_dir,
            lock_dir=args.lock_dir,
            ledger_path=args.ledger_path,
            ledger_key_path=args.ledger_key_path,
            routing_config_path=args.routing_config_path,
        )
        print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return 0
    run_forever(
        args.queue_dir,
        poll_seconds=args.poll_seconds,
        lock_dir=args.lock_dir,
        ledger_path=args.ledger_path,
        ledger_key_path=args.ledger_key_path,
        routing_config_path=args.routing_config_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
