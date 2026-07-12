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
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from chitra.goals import GoalRecord, check_specification

logger = structlog.get_logger(__name__)

RiskClass = Literal["a0", "a1", "a2", "a3"]
DecisionSource = Literal["goal", "principle", "oracle-escalated", "abstained"]
_TOKEN_RE = re.compile(r"[a-z0-9]+")


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
    evidence_refs: list[str] = Field(default_factory=list)


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


class DecisionProvenance(BaseModel):
    source: DecisionSource
    goal_version: int
    goal_fields: list[str]
    principle_ids: list[str]
    principle_citations: list[str]
    evidence_refs: list[str]
    oracle_escalated: bool
    confidence_basis: str


class ReasonedDecision(BaseModel):
    outcome: Literal["answer", "abstain"]
    answer: str
    provenance: DecisionProvenance
    insufficiency_reasons: list[str] = Field(default_factory=list)


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
    ) -> ReasonedDecision:
        """Resolve from goal, then principles, then the oracle when warranted."""
        goal_issues = check_specification(goal)
        if goal_issues:
            raise ReasoningContractError("frozen goal fails strict specification: " + "; ".join(goal_issues))
        if goal_judgment.determines_answer:
            if not goal_judgment.answer or not goal_judgment.goal_fields:
                raise ReasoningContractError("determining goal judgment requires an answer and cited goal fields")
            return self._decision(
                answer=goal_judgment.answer,
                source="goal",
                goal=goal,
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
            goal_fields=list(goal_judgment.goal_fields),
            evidence_refs=question.evidence_refs,
            confidence_basis="routine residuals do not justify oracle escalation",
            insufficiency=insufficiency,
            outcome="abstain",
        )

    @staticmethod
    def _decision(
        *,
        answer: str,
        source: DecisionSource,
        goal: GoalRecord,
        goal_fields: list[str],
        principles: list[PrincipleRecord] | None = None,
        evidence_refs: list[str],
        confidence_basis: str,
        insufficiency: list[str] | None = None,
        outcome: Literal["answer", "abstain"] = "answer",
    ) -> ReasonedDecision:
        selected = principles or []
        return ReasonedDecision(
            outcome=outcome,
            answer=answer,
            provenance=DecisionProvenance(
                source=source,
                goal_version=goal.goal_version,
                goal_fields=goal_fields,
                principle_ids=[item.principle_id for item in selected],
                principle_citations=[citation for item in selected for citation in item.citations],
                evidence_refs=evidence_refs,
                oracle_escalated=source == "oracle-escalated",
                confidence_basis=confidence_basis,
            ),
            insufficiency_reasons=insufficiency or [],
        )
