"""dispatchd — deterministic daemon that drains a JSON order queue and
delivers each order via ``chitra.dispatch.dispatch_to_tmux``, enforcing the
single-writer rule via ``LaneLock``.

Queue layout (default ``queue_dir``, overridable per call/CLI):

    queue_dir/orders/*.json      -- DispatchOrder JSON, one file per order
    queue_dir/results/<id>.json  -- DispatchResult JSON, written after processing
    queue_dir/processed/*.json   -- the order file, moved here after processing

Crash-safety: an order file already present under ``processed/`` is never
reprocessed, even if left in ``orders/`` by a crash between delivery and the
move (the move happens last; a result file guards against double-delivery
too — if a result already exists for an order id, the order is treated as
already processed and moved without re-dispatching).

No LLM calls. This module is deterministic plumbing only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import time
from pathlib import Path

import structlog

from . import ledger as ledger_mod
from .dispatch import (
    DispatchOrder,
    DispatchResult,
    DispatchStatus,
    LaneLock,
    LaneLockError,
    dispatch_to_tmux,
)

logger = structlog.get_logger(__name__)

DEFAULT_QUEUE_DIR = Path("/var/lib/polyphony-chitra/queue")
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
) -> DispatchResult | None:
    """Process a single order file. Returns the result, or None if skipped.

    Crash-safe: if a result file already exists for this order id, the order
    is considered already processed — it is moved to ``processed/`` without
    re-dispatching, and None is returned (no duplicate delivery).
    """
    try:
        order = DispatchOrder.model_validate_json(order_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("dispatchd_order_unreadable", path=str(order_path), error=str(exc))
        # Move aside so a malformed file doesn't spin the loop forever.
        processed_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            order_path.replace(processed_dir / order_path.name)
        return None

    existing_result = results_dir / f"{order.order_id}.json"
    if existing_result.exists():
        logger.info("dispatchd_order_already_processed", order_id=order.order_id)
        processed_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            order_path.replace(processed_dir / order_path.name)
        return None

    lock = LaneLock(order.session_ref, lock_dir=lock_dir)
    try:
        lock.acquire(blocking=True, timeout_seconds=5.0)
    except LaneLockError as exc:
        logger.warning("dispatchd_lane_lock_failed", order_id=order.order_id, session_ref=order.session_ref, error=str(exc))
        result = DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
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
) -> list[DispatchResult]:
    """Process every pending order in ``queue_dir/orders`` once, FIFO by mtime."""
    orders_dir, results_dir, processed_dir = _ensure_queue_dirs(queue_dir)
    pending = sorted(orders_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
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
) -> None:
    """Run the daemon loop: drain the queue, sleep, repeat. Runs until killed."""
    logger.info("dispatchd_started", queue_dir=str(queue_dir), poll_seconds=poll_seconds)
    while True:
        run_once(queue_dir, lock_dir=lock_dir, ledger_path=ledger_path, ledger_key_path=ledger_key_path)
        time.sleep(poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatchd", description="Deterministic tmux dispatch daemon (chitra phase 1).")
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR, help="Order/result/processed queue root.")
    parser.add_argument(
        "--lock-dir",
        type=Path,
        default=None,
        help="LaneLock directory (env POLYPHONY_CHITRA_LANE_LOCK_DIR, else a dir under the system temp dir).",
    )
    parser.add_argument("--ledger-path", type=Path, default=None, help="Delivery ledger JSONL path (default: next to the state dir).")
    parser.add_argument("--ledger-key-path", type=Path, default=None, help="HMAC signing key path (generated on first use if missing).")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Drain the queue once and exit (for tests/cron), instead of looping forever.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.once:
        results = run_once(args.queue_dir, lock_dir=args.lock_dir, ledger_path=args.ledger_path, ledger_key_path=args.ledger_key_path)
        print(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return 0
    run_forever(
        args.queue_dir,
        poll_seconds=args.poll_seconds,
        lock_dir=args.lock_dir,
        ledger_path=args.ledger_path,
        ledger_key_path=args.ledger_key_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
