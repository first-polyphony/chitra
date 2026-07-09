"""Tests for chitra.dispatchd: crash-safe reprocessing and queue draining."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chitra.dispatch import DispatchOrder, DispatchResult, DispatchStatus
from chitra.dispatchd import process_one_order, run_once


def _write_order(orders_dir: Path, order: DispatchOrder) -> Path:
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / f"{order.order_id}.json"
    path.write_text(order.model_dump_json(), encoding="utf-8")
    return path


def test_run_once_processes_pending_orders_and_moves_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import chitra.dispatchd as dispatchd_mod

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT, reason="sent: test")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-1", session_ref="localhost:s:0.0", nudge="hi")
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT
    assert not (queue_dir / "orders" / "ord-1.json").exists()
    assert (queue_dir / "processed" / "ord-1.json").exists()
    assert (queue_dir / "results" / "ord-1.json").exists()
    # A successful send is signed and logged automatically, no extra step.
    assert (tmp_path / "ledger.jsonl").exists()


def test_partially_processed_order_is_not_reprocessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A result file already existing for an order id means it was already
    delivered — process_one_order must not re-dispatch, only file-move."""
    import chitra.dispatchd as dispatchd_mod

    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    results_dir = queue_dir / "results"
    processed_dir = queue_dir / "processed"
    order = DispatchOrder(order_id="ord-2", session_ref="localhost:s:0.0", nudge="hi")
    order_path = _write_order(orders_dir, order)

    # Simulate a crash AFTER the result was written but BEFORE the order
    # file was moved to processed/.
    results_dir.mkdir(parents=True, exist_ok=True)
    existing_result = DispatchResult(order_id="ord-2", session_ref=order.session_ref, status=DispatchStatus.SENT)
    (results_dir / "ord-2.json").write_text(existing_result.model_dump_json(), encoding="utf-8")

    result = process_one_order(
        order_path,
        orders_dir=orders_dir,
        results_dir=results_dir,
        processed_dir=processed_dir,
        lock_dir=tmp_path / "locks",
    )

    assert result is None  # skipped, not re-dispatched
    assert call_count["n"] == 0
    assert (processed_dir / "ord-2.json").exists()


def test_blocked_result_does_not_write_a_ledger_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import chitra.dispatchd as dispatchd_mod

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.BLOCKED, reason="blocked: test")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-3", session_ref="localhost:s:0.0", nudge="hi")
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].status == DispatchStatus.BLOCKED
    # Only a real send is signed/logged — a blocked attempt is not a delivery.
    assert not (tmp_path / "ledger.jsonl").exists()


def test_malformed_order_file_is_moved_aside_not_crashed_on(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    bad = orders_dir / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")

    results = run_once(queue_dir, lock_dir=tmp_path / "locks")

    assert results == []
    assert not bad.exists()
    assert (queue_dir / "processed" / "bad.json").exists()
