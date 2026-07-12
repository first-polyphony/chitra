"""Goal-first, principle-backed reasoning with an explicit oracle fallback gate.

The deterministic package never calls a model.  The adapter supplies Chitra's
typed judgment about the frozen goal and, only when this module emits an
``OracleEscalation``, invokes the read-only ``oracle`` custom agent.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from chitra.goals import GoalRecord, check_specification, validate_goal
from chitra.principles import PrincipleMatch, PrinciplesIndex, lookup_principles

logger = structlog.get_logger(__name__)

ORACLE_AGENT: Literal["oracle"] = "oracle"
ORACLE_DEFINITION = "/opt/polyphony/deploy-main/infra/ccr/agents/oracle.md"


class ReasoningError(ValueError):
    """Raised when a reasoning or provenance contract is internally invalid."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DecisionRoute(StrEnum):
    GOAL = "goal"
    PRINCIPLES = "principles"
    ORACLE_ESCALATION = "oracle_escalation"
    ORACLE_ANSWER = "oracle_answer"
    ABSTAIN = "abstain"


class GoalClauseRef(_FrozenModel):
    field: Literal["intent", "goal", "done_when", "scope", "source"]
    quote: str = Field(min_length=1)


class FrozenGoal(_FrozenModel):
    session_ref: str
    intent: str
    goal: str
    done_when: str
    scope: str
    source: str
    goal_version: int = Field(ge=1)
    contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def from_record(cls, record: GoalRecord) -> FrozenGoal:
        issues = validate_goal(record) + check_specification(record)
        if issues:
            raise ReasoningError(f"goal record is not strict-valid: {'; '.join(issues)}")
        fields = {
            "session_ref": record.session_ref,
            "intent": record.intent,
            "goal": record.goal,
            "done_when": record.done_when,
            "scope": record.scope,
            "source": record.source,
            "goal_version": record.goal_version,
        }
        contract_id = f"sha256:{_hash(fields)}"
        return cls.model_validate({**fields, "contract_id": contract_id})

    def validate_clause(self, clause: GoalClauseRef) -> None:
        value = getattr(self, clause.field)
        if clause.quote not in value:
            raise ReasoningError(f"goal citation is not an exact span of {clause.field}")


class GoalJudgment(_FrozenModel):
    determination: Literal["determines", "insufficient"]
    rationale: str = Field(min_length=1)
    clause_refs: tuple[GoalClauseRef, ...] = Field(min_length=1)
    answer: str | None = None

    @model_validator(mode="after")
    def validate_answer_shape(self) -> GoalJudgment:
        if self.determination == "determines" and not self.answer:
            raise ValueError("a determining goal judgment requires an answer")
        if self.determination == "insufficient" and self.answer is not None:
            raise ValueError("an insufficient goal judgment cannot carry an answer")
        return self


class PrincipleJudgment(_FrozenModel):
    answer: str = Field(min_length=1)
    principle_ids: tuple[str, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class RiskContract(_FrozenModel):
    risk_class: Literal["A0", "A1", "A2", "A3"]
    genuinely_ambiguous: bool = False
    hard_to_reverse: bool = False
    consequence: str = Field(min_length=1)


class TriangulationRequest(_FrozenModel):
    question: str = Field(min_length=1)
    answer_category: str = Field(min_length=1)
    scopes: tuple[str, ...] = ("global",)
    goal: FrozenGoal
    goal_judgment: GoalJudgment
    principle_judgment: PrincipleJudgment | None = None
    risk: RiskContract


class Provenance(_FrozenModel):
    source: Literal["goal", "principle", "oracle-escalated"]
    reference: str
    citation: str


class OracleEscalation(_FrozenModel):
    escalation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    agent: Literal["oracle"] = ORACLE_AGENT
    definition_path: str = ORACLE_DEFINITION
    question: str
    reason: str
    prompt: str
    operator_confirmation_required: bool


class DecisionOutcome(_FrozenModel):
    decision_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    route: DecisionRoute
    answer: str | None
    goal_contract_id: str
    corpus_id: str
    confidence: Literal["high", "bounded", "insufficient"]
    provenance: tuple[Provenance, ...]
    principle_matches: tuple[PrincipleMatch, ...]
    escalation: OracleEscalation | None = None
    operator_confirmation_required: bool = False
    reason: str

    def to_attestation(self) -> DecisionAttestation:
        if self.answer is None or self.route not in {DecisionRoute.GOAL, DecisionRoute.PRINCIPLES, DecisionRoute.ORACLE_ANSWER}:
            raise ReasoningError("only a settled answer can attest a reasoned dispatch")
        return DecisionAttestation(
            decision_id=self.decision_id,
            route=cast(AttestedRoute, self.route),
            approved_text=self.answer,
            answer_sha256=_hash_text(self.answer),
            goal_contract_id=self.goal_contract_id,
            corpus_id=self.corpus_id,
            provenance=self.provenance,
            operator_confirmation_required=self.operator_confirmation_required,
        )


AttestedRoute = Literal[DecisionRoute.GOAL, DecisionRoute.PRINCIPLES, DecisionRoute.ORACLE_ANSWER]


class DecisionAttestation(_FrozenModel):
    decision_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    route: AttestedRoute
    approved_text: str = Field(min_length=1)
    answer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    goal_contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    corpus_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provenance: tuple[Provenance, ...] = Field(min_length=1)
    operator_confirmation_required: bool = False

    @model_validator(mode="after")
    def validate_text_hash(self) -> DecisionAttestation:
        if self.answer_sha256 != _hash_text(self.approved_text):
            raise ValueError("answer_sha256 does not match approved_text")
        return self


class OracleVerdict(_FrozenModel):
    escalation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verdict: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    citations: tuple[str, ...] = Field(min_length=1)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _jsonable(payload: object) -> object:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, StrEnum):
        return payload.value
    if isinstance(payload, dict):
        return {str(key): _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(value) for value in payload]
    return payload


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _goal_provenance(goal: FrozenGoal, judgment: GoalJudgment) -> tuple[Provenance, ...]:
    provenance: list[Provenance] = []
    for clause in judgment.clause_refs:
        goal.validate_clause(clause)
        provenance.append(
            Provenance(
                source="goal",
                reference=f"{goal.contract_id}@v{goal.goal_version}.{clause.field}",
                citation=clause.quote,
            )
        )
    return tuple(provenance)


def _principle_provenance(matches: tuple[PrincipleMatch, ...]) -> tuple[Provenance, ...]:
    return tuple(
        Provenance(
            source="principle",
            reference=match.principle_id,
            citation=f"{match.citation.path}:{match.citation.line_start}-{match.citation.line_end}@{match.citation.revision}",
        )
        for match in matches
    )


def _outcome(
    *,
    route: DecisionRoute,
    answer: str | None,
    goal_contract_id: str,
    corpus_id: str,
    confidence: Literal["high", "bounded", "insufficient"],
    provenance: tuple[Provenance, ...],
    principle_matches: tuple[PrincipleMatch, ...],
    escalation: OracleEscalation | None,
    operator_confirmation_required: bool,
    reason: str,
) -> DecisionOutcome:
    identity_fields = {
        "route": route,
        "answer": answer,
        "goal_contract_id": goal_contract_id,
        "corpus_id": corpus_id,
        "confidence": confidence,
        "provenance": provenance,
        "principle_matches": principle_matches,
        "escalation": escalation,
        "operator_confirmation_required": operator_confirmation_required,
        "reason": reason,
    }
    return DecisionOutcome(
        decision_id=f"sha256:{_hash(identity_fields)}",
        route=route,
        answer=answer,
        goal_contract_id=goal_contract_id,
        corpus_id=corpus_id,
        confidence=confidence,
        provenance=provenance,
        principle_matches=principle_matches,
        escalation=escalation,
        operator_confirmation_required=operator_confirmation_required,
        reason=reason,
    )


def reason(request: TriangulationRequest, index: PrinciplesIndex, *, confidence_threshold: float = 0.6) -> DecisionOutcome:
    """Apply the strict goal -> principles -> oracle priority order."""
    if not 0.0 < confidence_threshold <= 1.0:
        raise ValueError("confidence_threshold must be in (0, 1]")
    goal_provenance = _goal_provenance(request.goal, request.goal_judgment)
    if request.goal_judgment.determination == "determines":
        logger.info("triangulation_goal_answer", goal_contract_id=request.goal.contract_id)
        return _outcome(
            route=DecisionRoute.GOAL,
            answer=request.goal_judgment.answer,
            goal_contract_id=request.goal.contract_id,
            corpus_id=index.corpus_id,
            confidence="high",
            provenance=goal_provenance,
            principle_matches=(),
            escalation=None,
            operator_confirmation_required=False,
            reason="the frozen goal record determines the answer",
        )

    matches = lookup_principles(
        index,
        request.question,
        scopes=request.scopes,
        answer_category=request.answer_category,
    )
    eligible_by_id = {match.principle_id: match for match in matches if match.score >= confidence_threshold}
    supported: tuple[PrincipleMatch, ...] = ()
    if request.principle_judgment is not None:
        supported = tuple(
            eligible_by_id[principle_id]
            for principle_id in request.principle_judgment.principle_ids
            if principle_id in eligible_by_id
        )
        if len(supported) != len(set(request.principle_judgment.principle_ids)):
            supported = ()
    if supported:
        logger.info(
            "triangulation_principle_answer",
            goal_contract_id=request.goal.contract_id,
            principle_ids=[match.principle_id for match in supported],
        )
        return _outcome(
            route=DecisionRoute.PRINCIPLES,
            answer=request.principle_judgment.answer if request.principle_judgment else None,
            goal_contract_id=request.goal.contract_id,
            corpus_id=index.corpus_id,
            confidence="high",
            provenance=goal_provenance + _principle_provenance(supported),
            principle_matches=matches,
            escalation=None,
            operator_confirmation_required=False,
            reason="the goal left a gap and the deterministic principle lookup settled it",
        )

    escalate = (
        request.risk.genuinely_ambiguous
        or request.risk.hard_to_reverse
        or request.risk.risk_class in {"A2", "A3"}
    )
    if escalate:
        reason_text = "goal does not determine the answer and no asserted binding principle cleared the confidence threshold"
        escalation_payload = {
            "goal_contract_id": request.goal.contract_id,
            "corpus_id": index.corpus_id,
            "question": request.question,
            "risk": request.risk.model_dump(),
            "reason": reason_text,
        }
        escalation_id = f"sha256:{_hash(escalation_payload)}"
        prompt = (
            "Return a verdict, not a patch. Advise and never edit. "
            f"Question: {request.question}\n"
            f"Frozen goal v{request.goal.goal_version}: {request.goal.goal}\n"
            f"Done when: {request.goal.done_when}\nScope: {request.goal.scope}\n"
            f"Why sources 1 and 2 were insufficient: {reason_text}. "
            "Cite evidence actually read and label hypotheses."
        )
        escalation = OracleEscalation(
            escalation_id=escalation_id,
            question=request.question,
            reason=reason_text,
            prompt=prompt,
            operator_confirmation_required=request.risk.risk_class == "A3",
        )
        logger.warning("triangulation_oracle_escalation", escalation_id=escalation_id, risk_class=request.risk.risk_class)
        return _outcome(
            route=DecisionRoute.ORACLE_ESCALATION,
            answer=None,
            goal_contract_id=request.goal.contract_id,
            corpus_id=index.corpus_id,
            confidence="insufficient",
            provenance=goal_provenance,
            principle_matches=matches,
            escalation=escalation,
            operator_confirmation_required=request.risk.risk_class == "A3",
            reason=reason_text,
        )

    logger.info("triangulation_bounded_abstention", goal_contract_id=request.goal.contract_id)
    return _outcome(
        route=DecisionRoute.ABSTAIN,
        answer=None,
        goal_contract_id=request.goal.contract_id,
        corpus_id=index.corpus_id,
        confidence="insufficient",
        provenance=goal_provenance,
        principle_matches=matches,
        escalation=None,
        operator_confirmation_required=False,
        reason="routine question remains unsupported; name the missing fact or principle instead of spending an oracle call",
    )


def apply_oracle_verdict(pending: DecisionOutcome, verdict: OracleVerdict) -> DecisionOutcome:
    """Bind the adapter-returned read-only oracle verdict to its exact request."""
    if pending.route != DecisionRoute.ORACLE_ESCALATION or pending.escalation is None:
        raise ReasoningError("oracle verdict can only settle a pending oracle escalation")
    if verdict.escalation_id != pending.escalation.escalation_id:
        raise ReasoningError("oracle verdict is bound to a different escalation")
    provenance = pending.provenance + (
        Provenance(
            source="oracle-escalated",
            reference=f"oracle:{verdict.escalation_id}",
            citation="; ".join(verdict.citations),
        ),
    )
    logger.info("triangulation_oracle_answer_bound", escalation_id=verdict.escalation_id)
    return _outcome(
        route=DecisionRoute.ORACLE_ANSWER,
        answer=verdict.verdict,
        goal_contract_id=pending.goal_contract_id,
        corpus_id=pending.corpus_id,
        confidence="bounded",
        provenance=provenance,
        principle_matches=pending.principle_matches,
        escalation=pending.escalation,
        operator_confirmation_required=pending.operator_confirmation_required,
        reason=f"oracle fallback settled the residual ambiguity: {verdict.reasoning}",
    )
