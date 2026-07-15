"""Tests for chitra.completion_gate and chitra.taxonomy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import chitra.dispatchd as dispatchd_mod
from chitra.completion_gate import (
    CompletionClaimEvent,
    CompletionEvidence,
    TodoItem,
    check_todo_residue,
    evaluate_completion_claim,
    evaluate_turn_end,
    scan_deferral_language,
)
from chitra.dispatch import DispatchOrder, DispatchStatus
from chitra.dispatchd import process_one_order
from chitra.policy_config import GatePolicy
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


def test_load_taxonomy_can_validate_a_configured_replacement(tmp_path: Path) -> None:
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps({"entries": [{"code": "DEFERRAL_STUB", "cue": "custom", "disposition": "DECISION"}]}), encoding="utf-8")
    taxonomy = load_taxonomy(path)
    assert taxonomy[0].cue == "custom"


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

GOOD_CLAIM = """The requested parser gate was completed and deployed at SHA abc1234.
It rejects unsupported completion claims before delivery. Live health probe status=200 with 12 requests."""
GOOD_EVIDENCE = [
    CompletionEvidence(kind="deploy", citation="deployed SHA abc1234"),
    CompletionEvidence(kind="live_verify", citation="live health probe status=200 with 12 requests"),
]


def test_completion_claim_without_labeled_brief_and_with_real_evidence_is_clean() -> None:
    audit = evaluate_completion_claim([], GOOD_CLAIM, GOOD_EVIDENCE, load_taxonomy())
    assert audit.verdict == "CLEAN"
    assert audit.todo_residue == []
    assert audit.deferral_matches == []
    assert audit.evidence_gap is False
    assert "brief_issues" not in audit.model_dump()
    assert "brief" not in audit.summary


def test_done_claim_with_no_evidence_is_flagged_fake_done() -> None:
    audit = evaluate_completion_claim([], "the feature is done", [], load_taxonomy())
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.evidence_gap is True
    assert "deploy evidence" in audit.summary
    assert "live-verify evidence" in audit.summary


def test_done_claim_with_only_deploy_evidence_still_disputes_missing_live_verify() -> None:
    audit = evaluate_completion_claim(
        [],
        GOOD_CLAIM,
        [CompletionEvidence(kind="deploy", citation="deployed SHA abc1234")],
        load_taxonomy(),
    )
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.evidence_gap is True
    assert "missing live-verify evidence citation" in audit.summary
    assert "missing deploy evidence citation" not in audit.summary


def test_open_todo_residue_disputes_even_with_full_evidence() -> None:
    items = [TodoItem(text="finish the migration", status="open")]
    audit = evaluate_completion_claim(items, GOOD_CLAIM, GOOD_EVIDENCE, load_taxonomy())
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.todo_residue == ["finish the migration"]
    assert "finish the migration" in audit.summary


def test_deferral_language_disputes_even_with_full_evidence_and_no_todos() -> None:
    audit = evaluate_completion_claim([], GOOD_CLAIM + "\nDone -- you'll need to wire the rest", GOOD_EVIDENCE, load_taxonomy())
    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.deferral_matches


def test_deferral_and_evidence_checks_remain_enforcing_without_brief_gate() -> None:
    audit = evaluate_completion_claim(
        [],
        "Completed, but TODO: deploy and verify this later.",
        [],
        load_taxonomy(),
    )

    assert audit.verdict == "COMPLETION_DISPUTE"
    assert audit.deferral_matches
    assert audit.evidence_gap is True


def test_configured_completion_policy_controls_statuses_phrases_and_evidence() -> None:
    policy = GatePolicy(complete_todo_statuses=["closed"], deferral_phrases=["later phrase"], required_evidence=[])
    audit = evaluate_completion_claim(
        [TodoItem(text="release", status="closed")],
        GOOD_CLAIM,
        [CompletionEvidence(kind="artifact", citation="proof /tmp/release.json", todo_item="release")],
        load_taxonomy(),
        policy=policy,
    )
    assert audit.verdict == "CLEAN"


def test_literal_pass_exemplar_clears_with_honest_cited_per_item_evidence() -> None:
    todos = [TodoItem(text="deploy service", status="done"), TodoItem(text="verify live traffic", status="done")]
    claim = """The forced completion gate and roster state shipped with proof artifacts.
It forces every lane turn-end through cited completion review and preserves earlier failure evidence.
Live health probe status=200, healthy=24, failed=0; record /var/log/chitra/live-verify.log.
Artifacts: proof JSON /srv/proof/completion.json and screenshot /srv/proof/board.png.
Merged PR #56 and PR #65. Deploy SHA 1a2b3c4d.
Honest history: the earlier live probe returned HTTP 503 before the fix; see /var/log/chitra/earlier-503.log."""
    evidence = [
        CompletionEvidence(kind="deploy", citation="Deploy SHA 1a2b3c4d", todo_item="deploy service"),
        CompletionEvidence(
            kind="live_verify",
            citation="Live health probe status=200, healthy=24, failed=0",
            todo_item="verify live traffic",
        ),
        CompletionEvidence(kind="artifact", citation="proof JSON /srv/proof/completion.json"),
        CompletionEvidence(kind="artifact", citation="screenshot /srv/proof/board.png"),
        CompletionEvidence(kind="merged_pr", citation="Merged PR #56 and PR #65"),
        CompletionEvidence(kind="failure", citation="earlier live probe returned HTTP 503; /var/log/chitra/earlier-503.log"),
    ]

    audit = evaluate_completion_claim(todos, claim, evidence, load_taxonomy())

    assert audit.verdict == "CLEAN"
    assert any(item.kind == "failure" and "503" in item.citation for item in evidence)
    assert all(item.text not in audit.per_item_evidence_gap for item in todos)


def test_literal_fail_exemplar_is_rejected_for_hedges_ci_substitution_parse_only_and_posture_mismatch() -> None:
    claim = """The executable is parse-only and not publication-ready.
It is conditionally healthy and correctly blocked.
It is repaired and covered by tests and protected-CI evidence; no human signed."""
    audit = evaluate_completion_claim(
        [TodoItem(text="publish the runnable service", status="blocked")],
        claim,
        [CompletionEvidence(kind="live_verify", citation="CI evidence")],
        load_taxonomy(),
        open_asks=[],
        blockers=[],
    )

    assert audit.verdict == "COMPLETION_DISPUTE"
    assert {match["phrase"] for match in audit.deferral_matches} >= {
        "conditionally healthy",
        "correctly blocked",
        "parse-only",
        "not publication-ready",
        "repaired and covered by tests",
        "CI evidence",
    }
    assert audit.invalid_evidence == ["CI evidence"]
    assert audit.posture_mismatch is True


def test_turn_end_without_completion_claim_is_distinct_and_never_clean() -> None:
    audit = evaluate_turn_end(
        "I need the exact deployment target before continuing.",
        todo_items=[],
        evidence=[],
        taxonomy=load_taxonomy(),
    )
    assert audit.condition == "turn_end_without_completion_claim"
    assert audit.completion is None


# ---------------------------------------------------------------------------
# CompletionClaimEvent marker
# ---------------------------------------------------------------------------


def test_completion_claim_event_marker_value() -> None:
    assert CompletionClaimEvent.COMPLETION_CLAIM == "completion_claim"
    assert CompletionClaimEvent.TURN_END_WITHOUT_CLAIM == "turn_end_without_completion_claim"


# ---------------------------------------------------------------------------
# dispatchd wiring
# ---------------------------------------------------------------------------


def test_dispatchd_blocks_delivery_on_completion_dispute_and_never_calls_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_legacy_true_evidence_booleans_cannot_auto_pass_without_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_dispatch(order: DispatchOrder, **kwargs: object) -> None:  # pragma: no cover - must never be called
        raise AssertionError("bare legacy booleans must not reach delivery")

    monkeypatch.setattr(dispatchd_mod, "dispatch_to_tmux", fake_dispatch)
    queue_dir = tmp_path / "queue"
    orders_dir = queue_dir / "orders"
    orders_dir.mkdir(parents=True)
    (queue_dir / "results").mkdir()
    (queue_dir / "processed").mkdir()
    payload = {
        "order_id": "legacy-bool",
        "session_ref": "localhost:s:0.0",
        "nudge": GOOD_CLAIM,
        "completion_todo_items": [],
        "completion_has_deploy_evidence": True,
        "completion_has_live_verify_evidence": True,
    }
    (orders_dir / "legacy-bool.json").write_text(json.dumps(payload), encoding="utf-8")

    result = process_one_order(
        orders_dir / "legacy-bool.json",
        orders_dir=orders_dir,
        results_dir=queue_dir / "results",
        processed_dir=queue_dir / "processed",
        lock_dir=tmp_path / "locks",
    )

    assert result is not None
    assert result.status == DispatchStatus.COMPLETION_DISPUTE
    assert "evidence citation" in result.reason


def test_dispatchd_proceeds_to_dispatch_on_clean_completion_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        nudge=GOOD_CLAIM,
        completion_todo_items=[],
        completion_evidence=GOOD_EVIDENCE,
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


def test_dispatchd_leaves_a_non_completion_nudge_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
