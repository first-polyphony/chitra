"""Tests for chitra.dispatchd: crash-safe reprocessing and queue draining."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import chitra.dispatchd as dispatchd_mod
import chitra.ledger as ledger_mod
from chitra.dispatch import DispatchOrder, DispatchResult, DispatchStatus
from chitra.dispatchd import process_one_order, run_once
from chitra.routing_config import ROUTING_CONFIG_ENV_VAR


def _write_order(orders_dir: Path, order: DispatchOrder) -> Path:
    orders_dir.mkdir(parents=True, exist_ok=True)
    path = orders_dir / f"{order.order_id}.json"
    path.write_text(order.model_dump_json(), encoding="utf-8")
    return path


def test_run_once_processes_pending_orders_and_moves_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

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


def test_remote_delivery_writes_a_ledger_entry_same_as_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remote deliveries are chitra's primary path now, not a rarely-used
    side path -- a SENT result for a remote-host order must sign and append
    a ledger entry exactly like a local one. Runs the real dispatch_to_tmux
    (not mocked) against a fake ssh-wrapped runner so this also exercises the
    remote copy-mode-check / paste / transcript-verify path end to end."""
    import chitra.dispatch as dispatch_mod

    def fake_completed(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    def fake_runner(cmd: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "ssh", f"remote target must never shell out locally: {cmd}"
        assert cmd[-2] == "otherhost"
        remote_cmd = cmd[-1]
        if "capture-pane" in remote_cmd:
            return fake_completed(0, "ubuntu@otherhost:~$ ")
        if "display-message" in remote_cmd:
            return fake_completed(0, "0\n")
        if "paste-buffer" in remote_cmd:
            return fake_completed(0, "")
        if "find " in remote_cmd:
            return fake_completed(0, "1720000000 /remote/projects/foo/abc.jsonl\n")
        return fake_completed(0, "")

    # dispatch_to_tmux resolves its default runner (run_cmd) as a module
    # global at call time, so patching it here reaches the real (unmocked)
    # dispatch_to_tmux invoked by process_one_order -- this is an end-to-end
    # test of the fix, not a re-mock of dispatch_to_tmux itself.
    monkeypatch.setattr(dispatch_mod, "run_cmd", fake_runner)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-remote-1", session_ref="otherhost:f3:0.0", nudge="Stop editing main and open a PR.")
    _write_order(queue_dir / "orders", order)

    monkeypatch.setenv("REMOTE_DISPATCH_HOSTS", "otherhost")
    ledger_path = tmp_path / "ledger.jsonl"
    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=ledger_path,
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT
    assert ledger_path.exists()
    entry = ledger_mod.LedgerEntry.model_validate_json(ledger_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry.session_ref == "otherhost:f3:0.0"


def test_partially_processed_order_is_not_reprocessed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A result file already existing for an order id means it was already
    delivered — process_one_order must not re-dispatch, only file-move."""

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


def test_ledger_write_failure_does_not_prevent_completion_or_cause_redelivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression test: a ledger append failure after a real successful
    dispatch must not leave the order un-completed -- that would cause a
    redelivery (paste the same nudge into the live pane again) on the next
    pass, exactly the double-delivery the crash-safety design promises
    never happens."""

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    def failing_append_entry(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated disk failure writing the ledger")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    monkeypatch.setattr(ledger_mod, "append_entry", failing_append_entry)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-4", session_ref="localhost:s:0.0", nudge="hi")
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT
    # The order must still be marked complete -- moved and result-written --
    # even though the ledger write failed. Otherwise the next run_once()
    # would see it still pending and redeliver it.
    assert not (queue_dir / "orders" / "ord-4.json").exists()
    assert (queue_dir / "processed" / "ord-4.json").exists()
    assert (queue_dir / "results" / "ord-4.json").exists()


def test_routing_hint_flows_from_order_through_result_and_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An opaque routing_hint set on the order must appear unchanged on the
    DispatchResult and in the signed ledger entry -- chitra carries it
    through for audit purposes only, never interprets it."""

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            status=DispatchStatus.SENT,
            reason="sent: test",
        )

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-5", session_ref="localhost:s:0.0", nudge="hi", routing_hint="opus-panel")
    _write_order(queue_dir / "orders", order)

    ledger_path = tmp_path / "ledger.jsonl"
    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=ledger_path,
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].routing_hint == "opus-panel"

    ledger_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(ledger_lines) == 1
    entry = ledger_mod.LedgerEntry.model_validate_json(ledger_lines[0])
    assert entry.routing_hint == "opus-panel"


def test_routing_hint_defaults_to_none_and_is_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward compatibility: an order that never sets routing_hint (the
    default) flows through as None on both the result and the ledger."""

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(
            order_id=order.order_id,
            session_ref=order.session_ref,
            routing_hint=order.routing_hint,
            status=DispatchStatus.SENT,
            reason="sent: test",
        )

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-6", session_ref="localhost:s:0.0", nudge="hi")
    assert order.routing_hint is None
    _write_order(queue_dir / "orders", order)

    ledger_path = tmp_path / "ledger.jsonl"
    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=ledger_path,
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].routing_hint is None

    ledger_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    entry = ledger_mod.LedgerEntry.model_validate_json(ledger_lines[0])
    assert entry.routing_hint is None


def test_directive_voice_violation_writes_a_result_file_and_no_ledger_entry(tmp_path: Path) -> None:
    """End-to-end (real dispatch_to_tmux, not mocked): a nudge that trips the
    directive-voice guard must still produce a result file -- BLOCKED orders
    write results like any other order -- but must never generate a
    delivery-ledger entry, since dispatchd only signs/logs on SENT."""
    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-7", session_ref="localhost:s:0.0", nudge="the operator wants this pasted")
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].status == DispatchStatus.BLOCKED
    assert results[0].reason.startswith("directive-voice:")
    assert (queue_dir / "results" / "ord-7.json").exists()
    assert (queue_dir / "processed" / "ord-7.json").exists()
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


def _fake_dispatch_passthrough(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
    return DispatchResult(
        order_id=order.order_id,
        session_ref=order.session_ref,
        routing_hint=order.routing_hint,
        status=DispatchStatus.SENT,
        reason="sent: test",
    )


def test_routing_config_fills_in_routing_hint_when_task_type_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """task_type set, no explicit routing_hint, config has a matching entry:
    dispatchd fills in routing_hint from the config before dispatch."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)

    config_path = tmp_path / "routing.yaml"
    config_path.write_text(yaml.safe_dump({"defaults": {"code-review": "sonnet"}}), encoding="utf-8")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-7", session_ref="localhost:s:0.0", nudge="hi", task_type="code-review")
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        routing_config_path=config_path,
    )

    assert len(results) == 1
    assert results[0].routing_hint == "sonnet"


def test_routing_config_no_match_leaves_routing_hint_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """task_type set but absent from the config's defaults map: routing_hint
    stays None, exactly as if no task_type had been given."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)

    config_path = tmp_path / "routing.yaml"
    config_path.write_text(yaml.safe_dump({"defaults": {"code-review": "sonnet"}}), encoding="utf-8")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-8", session_ref="localhost:s:0.0", nudge="hi", task_type="unlisted-type")
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        routing_config_path=config_path,
    )

    assert len(results) == 1
    assert results[0].routing_hint is None


def test_no_routing_config_set_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CHITRA_ROUTING_CONFIG unset and no path passed: dispatchd runs exactly
    as it did before task_type/routing_config existed -- routing_hint is
    unaffected."""
    monkeypatch.delenv(ROUTING_CONFIG_ENV_VAR, raising=False)
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-9", session_ref="localhost:s:0.0", nudge="hi", task_type="code-review")
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].routing_hint is None


def test_explicit_routing_hint_wins_over_config_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller already set routing_hint directly: the config lookup is
    skipped entirely, even though task_type also matches a config entry."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)

    config_path = tmp_path / "routing.yaml"
    config_path.write_text(yaml.safe_dump({"defaults": {"code-review": "sonnet"}}), encoding="utf-8")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(
        order_id="ord-10",
        session_ref="localhost:s:0.0",
        nudge="hi",
        task_type="code-review",
        routing_hint="caller-chosen-hint",
    )
    _write_order(queue_dir / "orders", order)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        routing_config_path=config_path,
    )

    assert len(results) == 1
    assert results[0].routing_hint == "caller-chosen-hint"


def test_malformed_routing_config_raises_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured routing_config_path that fails to parse is a real
    configuration error -- run_once raises rather than silently ignoring
    it or falling back to no-config behavior."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)

    config_path = tmp_path / "routing.yaml"
    config_path.write_text("defaults: [this is not a mapping: :", encoding="utf-8")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-11", session_ref="localhost:s:0.0", nudge="hi", task_type="code-review")
    _write_order(queue_dir / "orders", order)

    with pytest.raises(yaml.YAMLError):
        run_once(
            queue_dir,
            lock_dir=tmp_path / "locks",
            ledger_path=tmp_path / "ledger.jsonl",
            ledger_key_path=tmp_path / "ledger.key",
            routing_config_path=config_path,
        )


def test_missing_routing_config_file_raises_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured routing_config_path that doesn't exist is a real
    configuration error -- run_once raises rather than silently no-oping."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-12", session_ref="localhost:s:0.0", nudge="hi", task_type="code-review")
    _write_order(queue_dir / "orders", order)

    with pytest.raises(OSError):
        run_once(
            queue_dir,
            lock_dir=tmp_path / "locks",
            ledger_path=tmp_path / "ledger.jsonl",
            ledger_key_path=tmp_path / "ledger.key",
            routing_config_path=tmp_path / "does-not-exist.yaml",
        )
