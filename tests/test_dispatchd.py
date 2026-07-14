"""Tests for chitra.dispatchd: crash-safe reprocessing and queue draining."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import chitra.dispatchd as dispatchd_mod
import chitra.ledger as ledger_mod
from chitra.dispatch import DISPATCH_VERIFY_WAIT_SECONDS, DispatchOrder, DispatchResult, DispatchStatus
from chitra.dispatchd import build_arg_parser, main, process_one_order, requeue_deferred_for_session, resolve_session_prefixes, run_once
from chitra.goals import GoalRecord, hold_goal, upsert_goal
from chitra.reasoning import DecisionAttestation
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


def test_reasoned_order_logs_attestation_our_side_without_leaking_metadata_into_lane_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_nudges: list[str] = []

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        seen_nudges.append(order.nudge)
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    approved = "Use the existing typed boundary."
    attestation = DecisionAttestation.create(
        outcome="answer",
        message_kind="reasoned_answer",
        approved_text=approved,
        source="goal",
        goal_contract_id="sha256:" + "1" * 64,
        goal_version=1,
        goal_fields=("scope",),
        corpus_id="sha256:" + "2" * 64,
        confidence_basis="the frozen goal directly determines this answer",
        review_signal_id="sha256:" + "3" * 64,
        review_verdict="accept",
        reviewer_count=2,
        autonomy="autonomous",
        operator_confirmation_required=False,
    )
    queue_dir = tmp_path / "queue"
    _write_order(
        queue_dir / "orders",
        DispatchOrder(
            order_id="attested",
            session_ref="localhost:s:0.0",
            nudge=approved,
            message_kind="reasoned_answer",
            decision_attestation=attestation,
        ),
    )

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "delivery.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        attestation_ledger_path=tmp_path / "attestations.jsonl",
    )[0]

    entry = ledger_mod.AttestationLedgerEntry.model_validate_json((tmp_path / "attestations.jsonl").read_text(encoding="utf-8"))
    assert entry.attestation == attestation
    assert result.decision_attestation_id == attestation.attestation_id
    assert seen_nudges == [approved]
    assert attestation.attestation_id not in seen_nudges[0]
    assert "reviewer" not in seen_nudges[0]


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
        if "tail -c" in remote_cmd:
            return fake_completed(0, "Stop editing main and open a PR.")
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


def test_dispatchd_blocks_orders_outside_its_owned_session_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        calls.append(order.session_ref)
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    queue_dir = tmp_path / "queue"
    _write_order(queue_dir / "orders", DispatchOrder(order_id="wrong-lane", session_ref="localhost:monitor:0.0", nudge="hi"))
    _write_order(
        queue_dir / "orders",
        DispatchOrder(order_id="owned-lane", session_ref="localhost:boomtown-design-a:0.0", nudge="hi"),
    )

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        allowed_session_prefixes=("boomtown-",),
    )

    assert [result.status for result in results] == [DispatchStatus.BLOCKED, DispatchStatus.SENT]
    assert "not owned by this dispatcher" in results[0].reason
    assert calls == ["localhost:boomtown-design-a:0.0"]
    assert (queue_dir / "processed" / "wrong-lane.json").exists()
    assert (queue_dir / "results" / "wrong-lane.json").exists()


def test_dispatchd_deny_namespace_overrides_an_allow_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dispatchd_mod,
        "dispatch_to_tmux",
        lambda order, **kwargs: DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT),
    )
    queue_dir = tmp_path / "queue"
    _write_order(queue_dir / "orders", DispatchOrder(order_id="reserved", session_ref="localhost:boomtown:0.0", nudge="hi"))

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        allowed_session_prefixes=("boomtown",),
        denied_session_prefixes=("boomtown",),
    )[0]

    assert result.status == DispatchStatus.BLOCKED
    assert "denied by prefix" in result.reason


def test_dispatchd_session_namespace_environment_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHITRA_ALLOWED_SESSION_PREFIXES", " boomtown-, boomtown-, design-")

    assert resolve_session_prefixes(None, env_var="CHITRA_ALLOWED_SESSION_PREFIXES") == ("boomtown-", "design-")


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


# --- rate-limit freeze / durable deferred subqueue (SOL findings #1, #7) ---


def _tracked_goal(session_ref: str = "tophand:feeds-111:0.0") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Ship the tested feature to production safely.",
        done_when="Tests pass.",
        source="task",
        status="working",
    )


def test_rate_limit_held_session_is_deferred_not_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session held for a rate-limit reason gets no ordinary new-work
    order delivered -- and, unlike a permanent BLOCKED/processed rejection,
    the order is durably parked in deferred/ with no result file, so it can
    still be delivered later. dispatch_to_tmux (any pane I/O) must never be
    called. See docs/SOL-ADVERSARIAL-REVIEW finding #1."""
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, _tracked_goal())
    hold_goal(goals_root, "tophand:feeds-111:0.0", reason="rate-limit:5h", resume_at="2026-07-12T12:00:00+00:00")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-frozen", session_ref="tophand:feeds-111:0.0", nudge="new work")
    _write_order(queue_dir / "orders", order)

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)

    assert call_count["n"] == 0
    assert len(results) == 1
    assert results[0].status == DispatchStatus.DEFERRED
    assert "rate-limit-deferred" in results[0].reason
    # Durably parked, not discarded: no result file, order sits in deferred/.
    assert not (queue_dir / "results" / "ord-frozen.json").exists()
    assert not (queue_dir / "processed" / "ord-frozen.json").exists()
    assert (queue_dir / "deferred" / "ord-frozen.json").exists()


def test_deferred_order_is_requeued_and_delivered_exactly_once_after_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: an order that arrives while a lane is rate-limit-held is
    never lost -- once the hold clears and requeue_deferred_for_session runs
    (as chitra.rate_limit_guard.apply_resume does), the SAME order is
    delivered, and delivered exactly once."""
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, _tracked_goal())
    hold_goal(goals_root, "tophand:feeds-111:0.0", reason="rate-limit:5h", resume_at="2026-07-12T12:00:00+00:00")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-later", session_ref="tophand:feeds-111:0.0", nudge="new work")
    _write_order(queue_dir / "orders", order)

    deferred_pass = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)
    assert deferred_pass[0].status == DispatchStatus.DEFERRED
    assert call_count["n"] == 0

    # A resume clears the hold; requeue_deferred_for_session (called by
    # chitra.rate_limit_guard.apply_resume) returns the backlog to orders/.
    from chitra.goals import resume_goal

    resume_goal(goals_root, "tophand:feeds-111:0.0")
    requeued = requeue_deferred_for_session(queue_dir, "tophand:feeds-111:0.0")
    assert requeued == ["ord-later"]
    assert not (queue_dir / "deferred" / "ord-later.json").exists()
    assert (queue_dir / "orders" / "ord-later.json").exists()

    delivered_pass = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)
    assert call_count["n"] == 1
    assert len(delivered_pass) == 1
    assert delivered_pass[0].status == DispatchStatus.SENT
    assert (queue_dir / "results" / "ord-later.json").exists()

    # A third pass must not redeliver -- ordinary idempotency still applies.
    third_pass = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)
    assert third_pass == []
    assert call_count["n"] == 1


def test_hold_for_non_rate_limit_reason_does_not_freeze_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator- or throttle-held lane is untouched by the rate-limit
    freeze -- only a hold_reason prefixed 'rate-limit:' defers delivery."""

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, _tracked_goal())
    hold_goal(goals_root, "tophand:feeds-111:0.0", reason="operator")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-not-frozen", session_ref="tophand:feeds-111:0.0", nudge="new work")
    _write_order(queue_dir / "orders", order)

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT


def test_bypass_flag_alone_does_not_escape_the_freeze_without_a_sealed_task_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An arbitrary queue writer cannot invent a bypass merely by setting
    bypass_rate_limit_freeze=True -- dispatchd only honors it for its own
    sealed internal task types. See docs/SOL-ADVERSARIAL-REVIEW finding #7."""
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, _tracked_goal())
    hold_goal(goals_root, "tophand:feeds-111:0.0", reason="rate-limit:5h", resume_at="2026-07-12T12:00:00+00:00")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(
        order_id="ord-fake-bypass",
        session_ref="tophand:feeds-111:0.0",
        nudge="not really a checkpoint",
        bypass_rate_limit_freeze=True,
        task_type="anything-else",
    )
    _write_order(queue_dir / "orders", order)

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)

    assert call_count["n"] == 0
    assert results[0].status == DispatchStatus.DEFERRED


def test_sealed_task_type_bypass_is_delivered_despite_the_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """chitra.rate_limit_guard's own checkpoint/stop/re-arm nudges (its
    sealed internal task types) must reach a session even while it is
    frozen -- they ARE the pause/resume mechanism."""
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, _tracked_goal())
    hold_goal(goals_root, "tophand:feeds-111:0.0", reason="rate-limit:5h", resume_at="2026-07-12T12:00:00+00:00")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(
        order_id="ord-checkpoint",
        session_ref="tophand:feeds-111:0.0",
        nudge="checkpoint now",
        bypass_rate_limit_freeze=True,
        task_type="rate-limit-checkpoint",
    )
    _write_order(queue_dir / "orders", order)

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)

    assert call_count["n"] == 1
    assert results[0].status == DispatchStatus.SENT


def test_load_shed_hold_defers_ordinary_work_but_allows_guard_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        calls.append(order.order_id)
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, _tracked_goal())
    hold_goal(goals_root, "tophand:feeds-111:0.0", reason="load-shed:tophand:2")
    queue_dir = tmp_path / "queue"
    _write_order(
        queue_dir / "orders",
        DispatchOrder(order_id="ordinary", session_ref="tophand:feeds-111:0.0", nudge="new work"),
    )
    _write_order(
        queue_dir / "orders",
        DispatchOrder(
            order_id="load-checkpoint",
            session_ref="tophand:feeds-111:0.0",
            nudge="checkpoint now",
            task_type="load-shed-checkpoint",
            bypass_rate_limit_freeze=True,
        ),
    )

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", goals_root=goals_root)

    assert [result.status for result in results] == [DispatchStatus.DEFERRED, DispatchStatus.SENT]
    assert calls == ["load-checkpoint"]


def test_no_goals_root_configured_leaves_dispatch_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward compatibility: a caller that never passes goals_root sees no
    behavior change -- the freeze check is a pure read of an explicitly
    configured goal store, not a new default coupling."""

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    monkeypatch.setenv("CHITRA_STATE_DIR", str(tmp_path / "no-such-state-dir"))

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-default", session_ref="tophand:feeds-111:0.0", nudge="new work")
    _write_order(queue_dir / "orders", order)

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl")

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT


def test_goals_root_is_wired_through_the_cli_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for SOL finding #7: build_arg_parser() exposed --goals-root
    but main() never forwarded it to run_once/run_forever, so a deployment
    using a non-default root believed it enabled the freeze but the daemon
    never consulted it. Drive the real CLI entrypoint end to end."""
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    goals_root = tmp_path / "goals"
    upsert_goal(goals_root, _tracked_goal())
    hold_goal(goals_root, "tophand:feeds-111:0.0", reason="rate-limit:5h", resume_at="2026-07-12T12:00:00+00:00")

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-cli", session_ref="tophand:feeds-111:0.0", nudge="new work")
    _write_order(queue_dir / "orders", order)

    exit_code = main(
        [
            "--once",
            "--queue-dir",
            str(queue_dir),
            "--lock-dir",
            str(tmp_path / "locks"),
            "--ledger-path",
            str(tmp_path / "ledger.jsonl"),
            "--goals-root",
            str(goals_root),
        ]
    )

    assert exit_code == 0
    assert call_count["n"] == 0  # the freeze must actually apply via the CLI path, not just direct run_once calls
    # Filesystem state, not printed stdout (which interleaves structlog
    # lines with the JSON print): the order must be durably deferred, never
    # delivered nor discarded, proving --goals-root actually reached
    # process_one_order through main() -> run_once().
    assert (queue_dir / "deferred" / "ord-cli.json").exists()
    assert not (queue_dir / "processed" / "ord-cli.json").exists()
    assert not (queue_dir / "results" / "ord-cli.json").exists()


# --- multiprocessing + kill-point tests (SOL finding #14) ------------------


def _mp_race_worker(queue_dir_str: str, lock_dir_str: str, ledger_path_str: str, log_path_str: str) -> None:
    """Runs in a forked child process (Linux default start method): drains
    the shared queue directory, logging every real dispatch attempt it
    makes to a shared, flock-serialized file so the parent can verify no
    order was ever delivered by more than one worker."""
    import fcntl

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        with open(log_path_str, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(order.order_id + "\n")
            fh.flush()
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    dispatchd_mod.dispatch_to_tmux = fake_dispatch  # type: ignore[assignment]
    for _ in range(3):  # multiple passes: mop up anything this worker didn't win on its first attempt
        run_once(Path(queue_dir_str), lock_dir=Path(lock_dir_str), ledger_path=Path(ledger_path_str))


def test_concurrent_workers_claim_and_deliver_each_order_exactly_once(tmp_path: Path) -> None:
    """Real OS-process concurrency: several workers race to drain the SAME
    queue directory. The atomic claim (rename into in_flight/) must ensure
    every order is delivered by exactly one worker, exactly once -- not
    zero, not two. See docs/SOL-ADVERSARIAL-REVIEW finding #5."""
    order_ids = [f"race-{i}" for i in range(16)]
    queue_dir = tmp_path / "queue"
    for order_id in order_ids:
        _write_order(queue_dir / "orders", DispatchOrder(order_id=order_id, session_ref=f"localhost:{order_id}:0.0", nudge="work"))

    log_path = tmp_path / "delivered.log"
    log_path.write_text("", encoding="utf-8")

    ctx = multiprocessing.get_context("fork")
    procs = [
        ctx.Process(
            target=_mp_race_worker,
            args=(str(queue_dir), str(tmp_path / "locks"), str(tmp_path / "ledger.jsonl"), str(log_path)),
        )
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    delivered = log_path.read_text(encoding="utf-8").splitlines()
    assert sorted(delivered) == sorted(order_ids)  # every order delivered, and none delivered twice
    for order_id in order_ids:
        assert (queue_dir / "results" / f"{order_id}.json").exists()
        assert (queue_dir / "processed" / f"{order_id}.json").exists()


def test_kill_point_crash_after_pane_touch_reconciles_instead_of_double_pasting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the exact gap SOL finding #5 named: a worker pastes into the
    live pane (dispatch_to_tmux succeeds) and then the process dies before
    _write_result_atomic runs. The order is left claimed with a send-nonce
    but no result. A later pass must NOT paste a second time -- it must
    reconcile via the target transcript (the same evidence dispatch_to_tmux
    itself uses) and synthesize the SENT result instead."""
    projects_root = tmp_path / "projects"
    nudge = "checkpoint now please"
    real_write_result_atomic = dispatchd_mod._write_result_atomic

    def fake_dispatch_that_pastes(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        # The "pane touch": write the transcript a real delivery would have
        # produced, exactly like a genuine paste would before the process died.
        session_dir = projects_root / "some-project"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "abc123.jsonl").write_text(json.dumps({"text": order.nudge}) + "\n", encoding="utf-8")
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    def crashing_write_result_once(*args: Any, **kwargs: Any) -> Path:
        raise RuntimeError("simulated process death before the result file was written")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch_that_pastes)
    monkeypatch.setattr(dispatchd_mod, "_write_result_atomic", crashing_write_result_once)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-crash", session_ref="localhost:s:0.0", nudge=nudge)
    order_path = _write_order(queue_dir / "orders", order)
    orders_dir, results_dir, processed_dir = dispatchd_mod._ensure_queue_dirs(queue_dir)

    with pytest.raises(RuntimeError, match="simulated process death"):
        process_one_order(
            order_path,
            orders_dir=orders_dir,
            results_dir=results_dir,
            processed_dir=processed_dir,
            lock_dir=tmp_path / "locks",
            ledger_path=tmp_path / "ledger.jsonl",
            projects_root=projects_root,
        )

    # Post-crash state: claimed with a nonce, no result, no processed move,
    # and the owner marker for the dead attempt is already gone (removed by
    # process_one_order's own finally, exactly as a live reclaim check would
    # expect from a genuinely-dead process).
    assert (queue_dir / "in_flight" / "ord-crash.json").exists()
    assert (queue_dir / "in_flight" / ".ord-crash.nonce").exists()
    assert not (queue_dir / "in_flight" / ".ord-crash.owner").exists()
    assert not (queue_dir / "results" / "ord-crash.json").exists()
    assert not (queue_dir / "processed" / "ord-crash.json").exists()

    # "Restart": a fresh pass with the real _write_result_atomic restored and
    # a call-counting dispatch_to_tmux to prove no second paste happens.
    monkeypatch.setattr(dispatchd_mod, "_write_result_atomic", real_write_result_atomic)
    second_dispatch_calls = {"n": 0}

    def counting_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        second_dispatch_calls["n"] += 1
        return fake_dispatch_that_pastes(order, **kwargs)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", counting_dispatch)

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", projects_root=projects_root)

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT
    assert "reconciled" in results[0].reason
    assert second_dispatch_calls["n"] == 0  # never pasted a second time
    assert (queue_dir / "results" / "ord-crash.json").exists()
    assert (queue_dir / "processed" / "ord-crash.json").exists()
    assert not (queue_dir / "in_flight" / ".ord-crash.nonce").exists()


def test_kill_point_crash_before_pane_touch_safely_redelivers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same kill-point: if the crash happened BEFORE
    any real pane I/O landed (the nonce exists, but no transcript anywhere
    confirms delivery), a restart must correctly conclude "not confirmed"
    and redeliver normally -- never stall waiting for evidence that will
    never arrive."""
    empty_projects_root = tmp_path / "no-transcripts-here"  # isolates from any real ~/.claude/projects content

    def crashing_claim_time_failure(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        raise RuntimeError("simulated crash before any pane I/O")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", crashing_claim_time_failure)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-early-crash", session_ref="localhost:s:0.0", nudge="a distinctive unmatched nudge")
    order_path = _write_order(queue_dir / "orders", order)
    orders_dir, results_dir, processed_dir = dispatchd_mod._ensure_queue_dirs(queue_dir)

    with pytest.raises(RuntimeError, match="simulated crash before any pane I/O"):
        process_one_order(
            order_path,
            orders_dir=orders_dir,
            results_dir=results_dir,
            processed_dir=processed_dir,
            lock_dir=tmp_path / "locks",
            ledger_path=tmp_path / "ledger.jsonl",
            projects_root=empty_projects_root,
        )

    # A nonce WAS written (the nonce is written immediately before
    # dispatch_to_tmux is called), so this exercises the same reconciliation
    # path -- but with no transcript evidence anywhere, reconciliation must
    # correctly conclude "not confirmed" and safely redeliver.
    assert (queue_dir / "in_flight" / ".ord-early-crash.nonce").exists()

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl", projects_root=empty_projects_root)

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT
    assert "reconciled" not in results[0].reason
    assert (queue_dir / "results" / "ord-early-crash.json").exists()


def test_stale_in_flight_claim_from_a_dead_owner_is_reclaimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim whose owning pid is dead (crashed worker, no graceful
    cleanup) is returned to orders/ on the next run_once pass -- never
    stranded forever. A claim whose owner is still alive is left alone."""

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    dispatchd_mod._ensure_queue_dirs(queue_dir)
    order = DispatchOrder(order_id="ord-orphan", session_ref="localhost:s:0.0", nudge="hi")
    orphaned_path = queue_dir / "in_flight" / "ord-orphan.json"
    orphaned_path.write_text(order.model_dump_json(), encoding="utf-8")
    # A pid essentially guaranteed not to be alive on any real system.
    (queue_dir / "in_flight" / ".ord-orphan.owner").write_text("999999999", encoding="utf-8")

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl")

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT
    assert (queue_dir / "results" / "ord-orphan.json").exists()


def test_live_owner_claim_is_never_stolen(tmp_path: Path) -> None:
    """The inverse: a claim whose owner pid IS alive (this test process
    itself) must never be reclaimed out from under it."""
    queue_dir = tmp_path / "queue"
    dispatchd_mod._ensure_queue_dirs(queue_dir)
    order = DispatchOrder(order_id="ord-live", session_ref="localhost:s:0.0", nudge="hi")
    claimed_path = queue_dir / "in_flight" / "ord-live.json"
    claimed_path.write_text(order.model_dump_json(), encoding="utf-8")
    (queue_dir / "in_flight" / ".ord-live.owner").write_text(str(os.getpid()), encoding="utf-8")

    dispatchd_mod._reclaim_stale_in_flight(queue_dir)

    assert claimed_path.exists()  # still claimed -- not reclaimed to orders/
    assert not (queue_dir / "orders" / "ord-live.json").exists()


def test_live_preclaim_reservation_keeps_pending_order_from_a_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner marker is created before rename, so another run_once pass
    cannot reclaim an order during that tiny pre-rename claim window."""
    calls = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        calls["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-reserved", session_ref="localhost:s:0.0", nudge="hi")
    _write_order(queue_dir / "orders", order)
    dispatchd_mod._ensure_queue_dirs(queue_dir)
    owner_path = queue_dir / "in_flight" / ".ord-reserved.owner"
    owner_path.write_text(str(os.getpid()), encoding="utf-8")

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl")

    assert results == []
    assert calls["n"] == 0
    assert (queue_dir / "orders" / "ord-reserved.json").exists()
    assert owner_path.exists()


def test_dead_preclaim_reservation_is_reclaimed_before_processing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash before rename leaves only a dead marker; it must not strand
    the still-pending order forever."""

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-stale-reservation", session_ref="localhost:s:0.0", nudge="hi")
    _write_order(queue_dir / "orders", order)
    dispatchd_mod._ensure_queue_dirs(queue_dir)
    owner_path = queue_dir / "in_flight" / ".ord-stale-reservation.owner"
    owner_path.write_text("999999999", encoding="utf-8")

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", ledger_path=tmp_path / "ledger.jsonl")

    assert len(results) == 1
    assert results[0].status == DispatchStatus.SENT
    assert not owner_path.exists()
    assert (queue_dir / "processed" / "ord-stale-reservation.json").exists()


def test_result_appearing_while_waiting_for_the_lane_lock_is_caught_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for SOL finding #5's explicit ask: recheck idempotency
    UNDER the lane lock, not only before acquiring it. Simulate a
    concurrent order for the same session finishing (writing this order's
    result) in the window between the pre-lock check and the lock actually
    being acquired -- process_one_order must catch that under the lock and
    never call dispatch_to_tmux."""
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    order = DispatchOrder(order_id="ord-race-under-lock", session_ref="localhost:s:0.0", nudge="hi")
    order_path = _write_order(queue_dir / "orders", order)
    orders_dir, results_dir, processed_dir = dispatchd_mod._ensure_queue_dirs(queue_dir)

    from chitra.dispatch import LaneLock

    real_acquire = LaneLock.acquire

    def acquire_then_plant_a_concurrent_result(self: LaneLock, **kwargs: Any) -> bool:
        acquired = real_acquire(self, **kwargs)
        # Simulate: another worker delivered this exact order and wrote its
        # result WHILE this call was waiting on the lock.
        concurrent_result = DispatchResult(order_id="ord-race-under-lock", session_ref=order.session_ref, status=DispatchStatus.SENT)
        (results_dir / "ord-race-under-lock.json").write_text(concurrent_result.model_dump_json(), encoding="utf-8")
        return acquired

    monkeypatch.setattr(LaneLock, "acquire", acquire_then_plant_a_concurrent_result)

    result = process_one_order(
        order_path, orders_dir=orders_dir, results_dir=results_dir, processed_dir=processed_dir, lock_dir=tmp_path / "locks"
    )

    assert result is None  # recognized as already-processed, not re-dispatched
    assert call_count["n"] == 0
    assert (processed_dir / "ord-race-under-lock.json").exists()


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

    assert len(results) == 1
    assert results[0].status == DispatchStatus.FAILED
    assert results[0].reason.startswith("invalid-order:")
    assert not bad.exists()
    assert (queue_dir / "invalid" / "bad.json").exists()
    assert (queue_dir / "results" / "bad.json").exists()


def test_malformed_order_file_is_logged_at_error_level(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unreadable/incomplete order is a silently-lost order (see the
    'Known limitation' docstring in process_one_order) -- it must be logged
    at ERROR, not buried at WARNING among routine noise. structlog's default
    logger factory prints to stdout rather than routing through stdlib
    logging, so this is asserted via captured output rather than caplog."""
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    bad = orders_dir / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")

    run_once(queue_dir, lock_dir=tmp_path / "locks")

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if "dispatchd_order_unreadable" in line]
    assert len(lines) == 1
    assert "error" in lines[0].lower()
    assert "warning" not in lines[0].lower()


def test_invalid_order_uses_the_configured_invalid_directory_and_result_record(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (orders_dir / "bad.json").write_text("{bad", encoding="utf-8")
    invalid_dir = tmp_path / "quarantine"

    results = run_once(queue_dir, lock_dir=tmp_path / "locks", invalid_dir=invalid_dir)

    assert results[0].order_id == "bad"
    assert (invalid_dir / "bad.json").exists()
    stored = DispatchResult.model_validate_json((queue_dir / "results" / "bad.json").read_text(encoding="utf-8"))
    assert stored == results[0]


def test_run_once_skips_order_file_that_vanishes_before_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file present in the glob() listing but removed before stat() (e.g.
    raced by something else touching the queue dir) must be skipped, not
    crash the drain loop."""
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    good_order = DispatchOrder(order_id="ord-good", session_ref="localhost:s:0.0", nudge="hi")
    _write_order(orders_dir, good_order)
    vanished_path = orders_dir / "vanished.json"
    vanished_path.write_text('{"not": "a real order"}', encoding="utf-8")

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "vanished.json":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    def fake_dispatch(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT, reason="sent: test")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    results = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert len(results) == 1
    assert results[0].order_id == "ord-good"
    # The vanished file was skipped entirely -- neither processed nor moved.
    # (checked via os.path, not Path.exists(), since Path.stat() is patched above)
    assert os.path.exists(vanished_path)


def _fake_dispatch_passthrough(order: DispatchOrder, **kwargs: Any) -> DispatchResult:
    return DispatchResult(
        order_id=order.order_id,
        session_ref=order.session_ref,
        routing_hint=order.routing_hint,
        status=DispatchStatus.SENT,
        reason="sent: test",
    )


def test_routing_config_fills_in_routing_hint_when_task_type_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_routing_provenance_is_stamped_on_result_and_signed_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)
    config_path = tmp_path / "routing.yaml"
    config_path.write_text(yaml.safe_dump({"defaults": {"code-review": "sonnet"}}), encoding="utf-8")
    queue_dir = tmp_path / "queue"
    _write_order(
        queue_dir / "orders",
        DispatchOrder(order_id="provenance", session_ref="localhost:s:0.0", nudge="hi", task_type="code-review"),
    )
    ledger_path = tmp_path / "ledger.jsonl"

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=ledger_path,
        ledger_key_path=tmp_path / "ledger.key",
        routing_config_path=config_path,
    )[0]

    assert result.task_type == "code-review"
    assert result.routing_hint_source == "config"
    entry = ledger_mod.LedgerEntry.model_validate_json(ledger_path.read_text(encoding="utf-8"))
    assert entry.task_type == "code-review"
    assert entry.routing_hint_source == "config"
    assert entry.sig_v == 3


def test_routes_entry_resolves_model_and_harness_into_result_and_signed_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured ``routes`` entry is actively RESOLVED at dispatch: the
    concrete model+harness (+zdr) and ``"route"`` provenance land on the
    result and in the HMAC-signed ledger entry (closes ROADMAP line 97)."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)
    config_path = tmp_path / "routing.yaml"
    config_path.write_text(
        yaml.safe_dump({"routes": {"design-judgment": {"model": "opus-4.8", "harness": "claude-code", "zdr": True}}}),
        encoding="utf-8",
    )
    queue_dir = tmp_path / "queue"
    _write_order(
        queue_dir / "orders",
        DispatchOrder(order_id="routed", session_ref="localhost:s:0.0", nudge="hi", task_type="design-judgment"),
    )
    ledger_path = tmp_path / "ledger.jsonl"

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=ledger_path,
        ledger_key_path=tmp_path / "ledger.key",
        routing_config_path=config_path,
    )[0]

    assert result.routing_hint == "opus-4.8@claude-code+zdr"
    assert result.routing_hint_source == "route"
    assert (result.resolved_model, result.resolved_harness, result.resolved_zdr) == ("opus-4.8", "claude-code", True)

    key = ledger_mod.load_or_create_signing_key(tmp_path / "ledger.key")
    entry = ledger_mod.LedgerEntry.model_validate_json(ledger_path.read_text(encoding="utf-8"))
    assert (entry.resolved_model, entry.resolved_harness, entry.resolved_zdr) == ("opus-4.8", "claude-code", True)
    assert entry.routing_hint_source == "route"
    assert entry.sig_v == 3
    assert ledger_mod.verify_entry(entry, key=key) is True


def test_routes_entry_wins_over_a_defaults_entry_for_same_task_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both a ``routes`` and a ``defaults`` entry exist for the same
    task_type, the structured (acted-on) route is preferred."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)
    config_path = tmp_path / "routing.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "defaults": {"code-fix": "sonnet"},
                "routes": {"code-fix": {"model": "gpt-5.6-sol", "harness": "codex-cli"}},
            }
        ),
        encoding="utf-8",
    )
    queue_dir = tmp_path / "queue"
    _write_order(
        queue_dir / "orders",
        DispatchOrder(order_id="both", session_ref="localhost:s:0.0", nudge="hi", task_type="code-fix"),
    )

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        routing_config_path=config_path,
    )[0]

    assert result.routing_hint == "gpt-5.6-sol@codex-cli"
    assert result.routing_hint_source == "route"
    assert result.resolved_model == "gpt-5.6-sol"


def test_defaults_only_config_leaves_resolved_fields_empty_backcompat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: a defaults-only config behaves exactly as before — the
    opaque routing_hint is filled with ``"config"`` provenance and no
    resolved model/harness selection is recorded."""
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)
    config_path = tmp_path / "routing.yaml"
    config_path.write_text(yaml.safe_dump({"defaults": {"code-review": "sonnet"}}), encoding="utf-8")
    queue_dir = tmp_path / "queue"
    _write_order(
        queue_dir / "orders",
        DispatchOrder(order_id="legacy", session_ref="localhost:s:0.0", nudge="hi", task_type="code-review"),
    )

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        routing_config_path=config_path,
    )[0]

    assert result.routing_hint == "sonnet"
    assert result.routing_hint_source == "config"
    assert (result.resolved_model, result.resolved_harness, result.resolved_zdr) == (None, None, False)


def test_policy_file_is_wired_to_completion_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", _fake_dispatch_passthrough)
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.safe_dump({"completion_gate": {"required_evidence": []}}), encoding="utf-8")
    queue_dir = tmp_path / "queue"
    _write_order(
        queue_dir / "orders",
        DispatchOrder(
            order_id="policy-gate",
            session_ref="localhost:s:0.0",
            nudge=(
                "What was built: The configured policy gate was completed.\n"
                "What it does: It exercises the configured evidence requirements.\n"
                "Does it actually work: Local probe status=200 with 1 check; /tmp/policy-proof.json."
            ),
            completion_todo_items=[],
        ),
    )

    result = run_once(
        queue_dir,
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
        policy_config_path=policy_path,
    )[0]

    assert result.status == DispatchStatus.SENT


def test_dispatchd_parser_exposes_policy_invalid_order_and_tuning_flags() -> None:
    args = build_arg_parser().parse_args(
        [
            "--policy-config-path",
            "policy.yaml",
            "--invalid-orders-dir",
            "invalid",
            "--capture-lines",
            "20",
            "--post-paste-wait-seconds",
            "0.25",
            "--transcript-recency-seconds",
            "120",
            "--lane-lock-timeout-seconds",
            "9",
            "--allow-session-prefix",
            "boomtown-",
            "--deny-session-prefix",
            "monitor",
        ]
    )
    assert args.policy_config_path == Path("policy.yaml")
    assert args.invalid_orders_dir == Path("invalid")
    assert (args.capture_lines, args.post_paste_wait_seconds, args.transcript_recency_seconds, args.lane_lock_timeout_seconds) == (
        20,
        0.25,
        120.0,
        9.0,
    )
    assert args.allow_session_prefix == ["boomtown-"]
    assert args.deny_session_prefix == ["monitor"]


def test_dispatchd_parser_uses_the_transcript_write_allowance_by_default() -> None:
    args = build_arg_parser().parse_args([])

    assert args.post_paste_wait_seconds == DISPATCH_VERIFY_WAIT_SECONDS == 15.0


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
