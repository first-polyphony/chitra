"""Goal-first, principle-backed decision triangulation for adapter reasoning.

The deterministic Chitra package does not call a model. The adapter supplies
its goal judgment and an oracle callback; this module enforces their order,
performs locked principle lookup, and records provenance for every outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from chitra.goal_enforcement import SessionReviewSignal, freeze_goal
from chitra.goals import GoalRecord, check_specification

logger = structlog.get_logger(__name__)

RiskClass = Literal["a0", "a1", "a2", "a3"]
DecisionSource = Literal["goal", "principle", "oracle-escalated", "abstained"]
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_OPERATOR_GATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("spend", re.compile(r"\b(spend|purchase|buy|billing|payment|paid plan|costs?\s+\$)\b", re.I)),
    ("credentials", re.compile(r"\b(credentials?|password|secret|api[- ]?key|oauth|login|authentication token)\b", re.I)),
    ("irreversible action", re.compile(r"\b(irreversible|delete|destroy|drop database|force[- ]push|terminate|revoke)\b", re.I)),
    ("strategy redirect", re.compile(r"\b(redirect|change (?:the )?goal|switch objectives?|expand (?:the )?scope)\b", re.I)),
)


class ReasoningContractError(ValueError):
    """Raised when a required reasoning contract is invalid or stale."""


class GoalJudgment(BaseModel):
    """Adapter judgment grounded in the frozen goal record."""

    model_config = ConfigDict(extra="forbid")

    determines_answer: bool
    answer: str | None = None
    goal_fields: list[Literal["intent", "goal", "done_when", "scope", "source"]] = Field(default_factory=list)
    inference: str = Field(min_length=1)


class DecisionQuestion(BaseModel):
    """Question and mechanically classified consequence information."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    answer_category: Literal["answer", "nudge", "action"] = "answer"
    risk_class: RiskClass = "a1"
    genuinely_ambiguous: bool = False
    expensive_to_reverse: bool = False
    spend: bool = False
    credentials: bool = False
    irreversible: bool = False
    strategy_redirect: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    session_review: SessionReviewSignal | None = None


class PrincipleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principle_id: str
    title: str
    guidance: str
    scope: list[str]
    answer_categories: list[str]
    keywords: list[str]
    status: Literal["binding", "scoped-binding"]
    citations: list[str]


class PrincipleMatch(BaseModel):
    principle: PrincipleRecord
    confidence: float = Field(ge=0, le=1)
    matched_keywords: list[str]


class OracleRequest(BaseModel):
    """Read-only verdict request passed to the adapter's oracle subagent."""

    goal: dict[str, object]
    question: DecisionQuestion
    goal_judgment: GoalJudgment
    principle_matches: list[PrincipleMatch]
    instruction: str = "Return a verdict, not a patch. Advise only; never edit."


class OracleVerdict(BaseModel):
    verdict: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    confidence_basis: str = Field(min_length=1)


class DecisionAttestation(BaseModel):
    """Immutable pre-dispatch decision record bound to exact approved text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attestation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    outcome: Literal["answer", "abstain"]
    message_kind: Literal["reasoned_answer", "reasoned_nudge", "reasoned_action"]
    approved_text: str = Field(min_length=1)
    approved_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: DecisionSource
    goal_contract_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    goal_version: int = Field(ge=1)
    goal_fields: tuple[str, ...]
    corpus_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    principle_ids: tuple[str, ...] = ()
    principle_citations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    oracle_escalated: bool = False
    confidence_basis: str = Field(min_length=1)
    insufficiency_reasons: tuple[str, ...] = ()
    review_signal_id: str | None = None
    review_verdict: Literal["accept", "reject"] | None = None
    reviewer_count: int = Field(default=0, ge=0)
    autonomy: Literal["autonomous", "operator_required"]
    operator_gate_reasons: tuple[str, ...] = ()
    operator_confirmation_required: bool
    operator_confirmed: bool = False

    @model_validator(mode="after")
    def validate_bindings(self) -> Self:
        if self.approved_text_sha256 != hashlib.sha256(self.approved_text.encode("utf-8")).hexdigest():
            raise ValueError("approved_text_sha256 does not match approved_text")
        if self.operator_confirmed and not self.operator_confirmation_required:
            raise ValueError("operator confirmation cannot be attached to an autonomous decision")
        if self.autonomy == "autonomous" and (self.operator_confirmation_required or self.review_verdict != "accept"):
            raise ValueError("autonomous release requires unanimous watched-session acceptance and no operator gate")
        payload = self.model_dump(mode="json", exclude={"attestation_id"})
        expected = f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}"
        if self.attestation_id != expected:
            raise ValueError("attestation_id does not match the attestation record")
        return self

    @classmethod
    def create(cls, **values: object) -> DecisionAttestation:
        """Build a fully bound attestation; ``None`` can never become text."""
        approved_text = values.get("approved_text")
        if not isinstance(approved_text, str) or not approved_text.strip():
            raise ReasoningContractError("approved_text must be non-empty text")
        payload: dict[str, object] = {
            "principle_ids": (),
            "principle_citations": (),
            "evidence_refs": (),
            "oracle_escalated": False,
            "insufficiency_reasons": (),
            "review_signal_id": None,
            "review_verdict": None,
            "reviewer_count": 0,
            "operator_gate_reasons": (),
            "operator_confirmed": False,
            **values,
            "approved_text": approved_text,
            "approved_text_sha256": hashlib.sha256(approved_text.encode("utf-8")).hexdigest(),
        }
        attestation_id = f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}"
        return cls.model_validate({**payload, "attestation_id": attestation_id})

    def with_operator_confirmation(self) -> DecisionAttestation:
        """Return a new immutable record carrying an explicit operator ruling."""
        if not self.operator_confirmation_required:
            raise ReasoningContractError("this decision does not require operator confirmation")
        payload = self.model_dump(mode="python", exclude={"attestation_id", "approved_text_sha256"})
        payload["operator_confirmed"] = True
        return DecisionAttestation.create(**payload)


class PrinciplesIndex:
    """Validated content-addressed principle index with lexical retrieval."""

    def __init__(self, path: Path | None = None) -> None:
        index_path = path or Path(str(files("chitra").joinpath("principles.lock.json")))
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReasoningContractError(f"principles index unreadable: {exc}") from exc
        if payload.get("schema") != "chitra.principles.lock.v1" or payload.get("reproducible") is not True:
            raise ReasoningContractError("principles index is not a reproducible chitra.principles.lock.v1 bundle")
        corpus_id = payload.pop("corpus_id", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        expected = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
        if corpus_id != expected:
            raise ReasoningContractError("principles index corpus_id does not match its contents")
        try:
            self.principles = [PrincipleRecord.model_validate(item) for item in payload["principles"]]
        except (KeyError, ValueError) as exc:
            raise ReasoningContractError(f"principles index has invalid records: {exc}") from exc
        self.corpus_id = expected

    def lookup(self, question: DecisionQuestion) -> list[PrincipleMatch]:
        """Return binding matches in deterministic confidence/id order."""
        tokens = set(_TOKEN_RE.findall(question.text.lower()))
        matches: list[PrincipleMatch] = []
        for principle in self.principles:
            if question.answer_category not in principle.answer_categories:
                continue
            matched = sorted(tokens & {keyword.lower() for keyword in principle.keywords})
            if not matched:
                continue
            confidence = min(1.0, 0.55 + 0.1 * len(matched))
            matches.append(PrincipleMatch(principle=principle, confidence=confidence, matched_keywords=matched))
        return sorted(matches, key=lambda item: (-item.confidence, item.principle.principle_id))


Oracle = Callable[[OracleRequest], OracleVerdict]


class DecisionReasoner:
    """Enforce goal -> principles -> residual oracle priority."""

    def __init__(self, principles: PrinciplesIndex, *, principle_threshold: float = 0.75) -> None:
        if not 0 < principle_threshold <= 1:
            raise ValueError("principle_threshold must be in (0, 1]")
        self.principles = principles
        self.principle_threshold = principle_threshold

    def decide(
        self,
        goal: GoalRecord,
        goal_judgment: GoalJudgment,
        question: DecisionQuestion,
        *,
        oracle: Oracle | None = None,
    ) -> DecisionAttestation:
        """Resolve from goal, then principles, then the oracle when warranted."""
        goal_issues = check_specification(goal)
        if goal_issues:
            raise ReasoningContractError("frozen goal fails strict specification: " + "; ".join(goal_issues))
        frozen_goal = freeze_goal(goal)
        if question.session_review is not None:
            if question.session_review.session_ref != goal.session_ref:
                raise ReasoningContractError("watched-session review belongs to a different session")
            if question.session_review.goal_contract_id != frozen_goal.contract_id:
                raise ReasoningContractError("watched-session review is stale for the frozen goal")
        if goal_judgment.determines_answer:
            if not goal_judgment.answer or not goal_judgment.goal_fields:
                raise ReasoningContractError("determining goal judgment requires an answer and cited goal fields")
            return self._decision(
                answer=goal_judgment.answer,
                source="goal",
                goal=goal,
                question=question,
                goal_fields=list(goal_judgment.goal_fields),
                evidence_refs=question.evidence_refs,
                confidence_basis="the frozen goal record directly determines the answer",
            )

        matches = self.principles.lookup(question)
        qualifying = [match for match in matches if match.confidence >= self.principle_threshold]
        if qualifying:
            best = qualifying[0]
            return self._decision(
                answer=best.principle.guidance,
                source="principle",
                goal=goal,
                question=question,
                goal_fields=list(goal_judgment.goal_fields),
                principles=[best.principle],
                evidence_refs=question.evidence_refs,
                confidence_basis=(
                    f"binding principle {best.principle.principle_id} matched "
                    f"{len(best.matched_keywords)} deterministic keywords at {best.confidence:.2f}"
                ),
            )

        insufficiency = ["the frozen goal judgment does not determine the answer"]
        if not matches:
            insufficiency.append("the compiled index contains no matching binding principle")
        else:
            insufficiency.append(f"no principle meets the {self.principle_threshold:.2f} confidence threshold")
        oracle_warranted = question.genuinely_ambiguous or question.expensive_to_reverse or question.risk_class in ("a2", "a3")
        if oracle_warranted:
            if oracle is None:
                raise ReasoningContractError("insufficiency gate requires an oracle callback for this consequential residual")
            verdict = oracle(
                OracleRequest(
                    goal=goal.to_dict(),
                    question=question,
                    goal_judgment=goal_judgment,
                    principle_matches=matches,
                )
            )
            logger.info("reasoning_oracle_escalated", session_ref=goal.session_ref, risk_class=question.risk_class)
            return self._decision(
                answer=verdict.verdict,
                source="oracle-escalated",
                goal=goal,
                question=question,
                goal_fields=list(goal_judgment.goal_fields),
                evidence_refs=[*question.evidence_refs, *verdict.evidence_refs],
                confidence_basis=verdict.confidence_basis,
                insufficiency=insufficiency,
            )

        return self._decision(
            answer=(
                "The goal and binding principles do not settle this routine question; "
                "provide the missing fact or a more specific goal clause."
            ),
            source="abstained",
            goal=goal,
            question=question,
            goal_fields=list(goal_judgment.goal_fields),
            evidence_refs=question.evidence_refs,
            confidence_basis="routine residuals do not justify oracle escalation",
            insufficiency=insufficiency,
            outcome="abstain",
        )

    def _decision(
        self,
        *,
        answer: str,
        source: DecisionSource,
        goal: GoalRecord,
        question: DecisionQuestion,
        goal_fields: list[str],
        principles: list[PrincipleRecord] | None = None,
        evidence_refs: list[str],
        confidence_basis: str,
        insufficiency: list[str] | None = None,
        outcome: Literal["answer", "abstain"] = "answer",
    ) -> DecisionAttestation:
        selected = principles or []
        review = question.session_review
        gate_reasons: list[str] = []
        if review is None:
            gate_reasons.append("missing unanimous watched-session review")
        elif review.verdict != "accept":
            gate_reasons.append("watched-session review rejected the lane behavior")
        if question.spend:
            gate_reasons.append("spend")
        if question.credentials:
            gate_reasons.append("credentials")
        if question.irreversible or question.expensive_to_reverse:
            gate_reasons.append("irreversible action")
        if question.strategy_redirect:
            gate_reasons.append("strategy redirect")
        if question.risk_class == "a3":
            gate_reasons.append("a3 consequence")
        combined_text = f"{question.text}\n{answer}"
        for reason, pattern in _OPERATOR_GATE_PATTERNS:
            if pattern.search(combined_text):
                gate_reasons.append(reason)
        if outcome == "abstain":
            gate_reasons.append("abstained decision")
        operator_required = bool(gate_reasons)
        message_kind = {
            "answer": "reasoned_answer",
            "nudge": "reasoned_nudge",
            "action": "reasoned_action",
        }[question.answer_category]
        return DecisionAttestation.create(
            outcome=outcome,
            message_kind=message_kind,
            approved_text=answer,
            source=source,
            goal_contract_id=freeze_goal(goal).contract_id,
            goal_version=goal.goal_version,
            goal_fields=tuple(goal_fields),
            corpus_id=self.principles.corpus_id,
            principle_ids=tuple(item.principle_id for item in selected),
            principle_citations=tuple(citation for item in selected for citation in item.citations),
            evidence_refs=tuple(evidence_refs),
            oracle_escalated=source == "oracle-escalated",
            confidence_basis=confidence_basis,
            insufficiency_reasons=tuple(insufficiency or ()),
            review_signal_id=review.signal_id if review is not None else None,
            review_verdict=review.verdict if review is not None else None,
            reviewer_count=len(review.reviewer_ids) if review is not None else 0,
            autonomy="operator_required" if operator_required else "autonomous",
            operator_gate_reasons=tuple(dict.fromkeys(gate_reasons)),
            operator_confirmation_required=operator_required,
            operator_confirmed=False,
        )
