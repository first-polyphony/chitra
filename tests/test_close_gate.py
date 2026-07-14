"""Close-time inventory and done-condition lint acceptance tests."""

from __future__ import annotations

import pytest

from chitra.close_gate import (
    AGGREGATE_DONE_WHEN_TOKENS,
    BARE_DELIVERABLE_PLURALS,
    CloseGateError,
    delivered_items_from_evidence,
    evaluate_close_inventory,
    lint_done_when,
    parse_required_items,
    require_close_inventory,
)
from chitra.completion_gate import CompletionEvidence

F8_DONE_WHEN = "both the X client and the Y client pass live validation"


def test_parser_reads_atomic_conjunction_bullets_and_explicit_counts() -> None:
    assert [item.text for item in parse_required_items("The release artifact exists.")] == ["The release artifact exists"]
    assert [item.text for item in parse_required_items(F8_DONE_WHEN)] == [
        "the X client",
        "the Y client pass live validation",
    ]
    assert [item.text for item in parse_required_items("1. API deployed\n2. Probe passes\n- Docs updated")] == [
        "API deployed",
        "Probe passes",
        "Docs updated",
    ]
    counted = parse_required_items("2 live clients pass validation")
    assert counted[0].quantity == 2
    assert counted[0].counted_noun == "client"


@pytest.mark.parametrize("token", AGGREGATE_DONE_WHEN_TOKENS)
def test_lint_flags_each_aggregate_quantifier_without_a_count(token: str) -> None:
    finding = lint_done_when(f"{token} consumers pass live validation")

    assert finding is not None
    assert finding.code == "vague_done_when"
    assert token in finding.matches


@pytest.mark.parametrize("noun", sorted(BARE_DELIVERABLE_PLURALS))
def test_lint_flags_each_bare_deliverable_plural_without_a_count(noun: str) -> None:
    finding = lint_done_when(f"{noun} pass live validation")

    assert finding is not None
    assert noun in finding.matches


def test_vague_done_when_is_flagged_without_rewriting_and_explicit_both_is_clean() -> None:
    vague = "representative consumers pass live validation"
    original = vague

    finding = lint_done_when(vague)

    assert finding is not None
    assert finding.code == "vague_done_when"
    assert vague == original
    assert lint_done_when("both consumer A and consumer B pass live validation") is None
    assert lint_done_when("2 clients and three integrations pass live validation") is None
    assert lint_done_when("") is not None


def test_f8_shape_close_fails_for_undelivered_follow_on_without_operator_descope() -> None:
    verdict = evaluate_close_inventory(
        F8_DONE_WHEN,
        ["X client"],
        close_notes=["The Y client is follow-on work."],
    )

    assert verdict.verdict == "FAIL"
    assert [gap.required_item.text for gap in verdict.missing] == ["the Y client pass live validation"]
    assert [item.text for item in verdict.reclassified] == ["the Y client pass live validation"]
    assert "F8 close tell" in verdict.summary
    with pytest.raises(CloseGateError, match="F8 close tell"):
        require_close_inventory(
            F8_DONE_WHEN,
            ["X client"],
            close_notes=["The Y client is follow-on work."],
        )


def test_f8_shape_close_passes_with_explicit_operator_ack() -> None:
    verdict = evaluate_close_inventory(
        F8_DONE_WHEN,
        ["X client"],
        close_notes=["The Y client is deferred."],
        operator_acknowledged_items=["Y client"],
    )

    assert verdict.verdict == "PASS"
    assert [item.text for item in verdict.acknowledged] == ["the Y client pass live validation"]


def test_f8_shape_close_passes_after_recorded_goal_revision_descopes_y() -> None:
    verdict = evaluate_close_inventory(
        "The X client passes live validation",
        ["X client"],
        close_notes=["The Y client is future work."],
        goal_version=2,
        goal_history=[{"done_when": F8_DONE_WHEN}],
    )

    assert verdict.verdict == "PASS"
    assert [item.text for item in verdict.recorded_descopes] == ["the Y client pass live validation"]


def test_counted_requirement_needs_that_many_explicit_delivered_items() -> None:
    failed = evaluate_close_inventory("2 live clients pass validation", ["X client"])
    passed = evaluate_close_inventory("2 live clients pass validation", ["X client", "Y client"])

    assert failed.verdict == "FAIL"
    assert failed.missing[0].missing_count == 1
    assert passed.verdict == "PASS"


def test_completion_evidence_contributes_only_explicit_todo_item_bindings() -> None:
    evidence = [
        CompletionEvidence(kind="live_verify", citation="probe status=200", todo_item="X client"),
        CompletionEvidence(kind="deploy", citation="Y client appears only in citation SHA abc1234"),
    ]

    assert delivered_items_from_evidence(evidence) == ("X client",)
    assert evaluate_close_inventory("The X client passes live validation", [], evidence=evidence).verdict == "PASS"


@pytest.mark.parametrize("phrase", ["follow-on", "out of scope", "out-of-scope", "deferred", "future work"])
def test_every_reclassification_phrase_is_an_f8_close_tell(phrase: str) -> None:
    verdict = evaluate_close_inventory("The Y client passes live validation", [], close_notes=[f"Y client is {phrase}."])

    assert verdict.verdict == "FAIL"
    assert verdict.reclassified


def test_negated_reclassification_phrase_is_not_treated_as_a_descope() -> None:
    verdict = evaluate_close_inventory(
        "The Y client passes live validation",
        ["Y client"],
        close_notes=["Y client is not deferred."],
    )

    assert verdict.verdict == "PASS"
