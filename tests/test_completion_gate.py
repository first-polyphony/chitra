"""Tests for chitra.completion_gate and chitra.taxonomy."""

from __future__ import annotations

from pathlib import Path

import pytest

import chitra.dispatchd as dispatchd_mod
from chitra.completion_gate import (
    CompletionClaimEvent,
    TodoItem,
    check_todo_residue,
    evaluate_completion_claim,
    scan_deferral_language,
)
from chitra.dispatch import DispatchOrder, DispatchStatus
from chitra.dispatchd import process_one_order
from chitra.taxonomy import Disposition, TaxonomyEntry, load_taxonomy

# ---------------------------------------------------------------------------
# taxonomy loading
# ---------------------------------------------------------------------------


def test_load_taxonomy_has_exactly_24_entries() -> None:
    taxonomy = load_taxonomy()
    assert len(taxonomy) == 24


def test_load_taxonomy_entries_are_typed_and_have_required_fields() -> None:
    taxonomy = load_taxonomy()
    for entry in taxonomy:
        assert isinstance(entry, TaxonomyEntry)
        assert entry.code
        assert entry.cue
        assert isinstance(entry.disposition, Disposition)


def test_load_taxonomy_contains_deferral_stub_and_fake_done() -> None:
    codes = {entry.code for entry in load_taxonomy()}
    assert "DEFERRAL_STUB" in codes
    assert "FAKE_DONE" in codes


def test_load_taxonomy_codes_are_unique() -> None:
    codes = [entry.code for entry in load_taxonomy()]
    assert len(codes) == len(set(codes))


# ---------------------------------------------------------------------------
# check_todo_residue / scan_deferral_language
# ---------------------------------------------------------------------------


def test_open_todo_item_under_done_claim_is_flagged_as_deferral() -> None:
    items = [
        TodoItem(text="write tests", status="done"),
        TodoItem(text="wire the daemon", status="in_progress"),
        TodoItem(text="update docs", status="open"),
    ]
    residue = check_todo_residue(items)
    assert residue == ["wire the daemon", "update docs"]


def test_check_todo_residue_empty_when_all_done() -> None:
    items = [TodoItem(text="a", status="done"), TodoItem(text="b", status="done")]
    assert check_todo_residue(items) == []


@pytest.mark.parametrize(
    "text",
    [
        "Done -- you'll need to wire the rest yourself.",
        "TODO: finish this later",
        "This is out of scope for now",
        "Leaving for a follow-up PR",
        "The remaining work is deferred",
    ],
)
def test_scan_deferral_language_matches_real_cue_derived_phrases(text: str) -> None:
    matches = scan_deferral_language(text, load_taxonomy())
    assert matches
    assert all(m["code"] == "DEFERRAL_STUB" for m in matches)


def test_scan_deferral_language_no_match_on_clean_text() -> None:
    matches = scan_deferral_language("All tests pass, deployed and verified live.", load_taxonomy())
    assert matches == []


def test_scan_deferral_language_returns_empty_list_when_taxonomy_lacks_operationalized_codes() -> None:
    unrelated = [TaxonomyEntry(code="SOMETHING_ELSE", cue="n/a", disposition=Disposition.NUDGE)]
    matches = scan_deferral_language("TODO: finish this", unrelated)
    assert matches == []


# ---------------------------------------------------------------------------
# evaluate_completion_claim
# ---------------------------------------------------------------------------


def test_clean_completion_claim_with_no_todos_and_real_evidence_is_clean() -> None:
    audit = evaluate_completion_claim([], "all tests pass, deployed and verified live", True, True, load_taxonomy())
    assert audit.verdict == "CLEAN"
    assert audit.todo_residue == []
    assert audit.deferral_matches == []
    assert audit.evidence_gap is False


def test_done_claim_with_no_evidence_is_flagged_fake_done() -> None:
    audit = evaluate_completion_claim([], "the feature is done", False, False, load_taxonomy())
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.evidence_gap is True
    assert "deploy evidence" in audit.summary
    assert "live-verify evidence" in audit.summary


def test_done_claim_with_only_deploy_evidence_still_disputes_missing_live_verify() -> None:
    audit = evaluate_completion_claim([], "done, deployed", True, False, load_taxonomy())
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.evidence_gap is True
    assert "missing live-verify evidence" in audit.summary
    assert "missing deploy evidence" not in audit.summary


def test_open_todo_residue_disputes_even_with_full_evidence() -> None:
    items = [TodoItem(text="finish the migration", status="open")]
    audit = evaluate_completion_claim(items, "done", True, True, load_taxonomy())
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.todo_residue == ["finish the migration"]
    assert "finish the migration" in audit.summary


def test_deferral_language_disputes_even_with_full_evidence_and_no_todos() -> None:
    audit = evaluate_completion_claim([], "done -- you'll need to wire the rest", True, True, load_taxonomy())
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.deferral_matches


# ---------------------------------------------------------------------------
# CompletionClaimEvent marker
# ---------------------------------------------------------------------------


def test_completion_claim_event_marker_value() -> None:
    assert CompletionClaimEvent.COMPLETION_CLAIM == "completion_claim"


# ---------------------------------------------------------------------------
# dispatchd wiring
# ---------------------------------------------------------------------------


def test_dispatchd_blocks_delivery_on_completion_dispute_and_never_calls_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> None:  # pragma: no cover - must never be called
        call_count["n"] += 1
        raise AssertionError("dispatch_to_tmux must not be called for a disputed completion claim")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir(parents=True)
    (queue_dir / "processed").mkdir(parents=True)
    order = DispatchOrder(
        order_id="ord-audit-1",
        session_ref="localhost:s:0.0",
        nudge="the feature is done",
        completion_todo_items=[TodoItem(text="write tests", status="open")],
        completion_has_deploy_evidence=False,
        completion_has_live_verify_evidence=False,
    )
    order_path = orders_dir / "ord-audit-1.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    result = process_one_order(
        order_path,
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
    )

    assert call_count["n"] == 0
    assert result is not None
    assert result.status == DispatchStatus.COMPLETION_DISPUTE
    assert "write tests" in result.reason
    assert (queue_dir / "processed" / "ord-audit-1.json").exists()


def test_dispatchd_proceeds_to_dispatch_on_clean_completion_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chitra.dispatch import DispatchResult

    call_count = {"n": 0}

    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> DispatchResult:
        call_count["n"] += 1
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT, reason="sent: test")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir(parents=True)
    (queue_dir / "processed").mkdir(parents=True)
    order = DispatchOrder(
        order_id="ord-audit-2",
        session_ref="localhost:s:0.0",
        nudge="done, deployed and verified live",
        completion_todo_items=[],
        completion_has_deploy_evidence=True,
        completion_has_live_verify_evidence=True,
    )
    order_path = orders_dir / "ord-audit-2.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    result = process_one_order(
        order_path,
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert call_count["n"] == 1
    assert result is not None
    assert result.status == DispatchStatus.SENT


def test_dispatchd_skips_audit_entirely_when_completion_todo_items_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An order that never opts in to the completion-claim check (the
    default -- completion_todo_items=None) is completely unaffected."""
    from chitra.dispatch import DispatchResult

    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> DispatchResult:
        return DispatchResult(order_id=order.order_id, session_ref=order.session_ref, status=DispatchStatus.SENT)

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)

    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir(parents=True)
    (queue_dir / "processed").mkdir(parents=True)
    order = DispatchOrder(order_id="ord-plain", session_ref="localhost:s:0.0", nudge="hi")
    order_path = orders_dir / "ord-plain.json"
    order_path.write_text(order.model_dump_json(), encoding="utf-8")

    result = process_one_order(
        order_path,
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
        ledger_path=tmp_path / "ledger.jsonl",
        ledger_key_path=tmp_path / "ledger.key",
    )

    assert result is not None
    assert result.status == DispatchStatus.SENT
