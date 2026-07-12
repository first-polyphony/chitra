"""Tests for the unshipped adversarial-reasoning adapter."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from adapter_prototype.goal_loop import (
    AdversarialGoalLoop,
    CcrClaudeReasoner,
    GoalLoopBlocked,
    GoalLoopConfigurationError,
    GoalLoopProtocolError,
    LoopRequest,
)
from chitra.goal_enforcement import EvidenceItem
from chitra.goals import GoalRecord


def _goal() -> GoalRecord:
    return GoalRecord(
        session_ref="host:lane:0.0",
        goal="Ship the deterministic parser repair without widening scope.",
        done_when="The parser tests and static checks pass cleanly.",
        intent="Restore correct parser behavior while preserving every existing public contract.",
        scope="Parser implementation and focused tests only.",
        source="task-file:/tmp/parser-repair.md",
        status="working",
    )


class _ScriptedReasoner:
    def __init__(
        self,
        *,
        drafts: list[str],
        review_dispositions: list[str],
        malformed_reviewer: bool = False,
    ) -> None:
        self._drafts = iter(drafts)
        self._review_dispositions = iter(review_dispositions)
        self._malformed_reviewer = malformed_reviewer
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        instance_id: str,
        role: str,
        system_prompt: str,
        payload_json: str,
        output_schema_json: str,
    ) -> str:
        payload = json.loads(payload_json)
        self.calls.append(
            {
                "instance_id": instance_id,
                "role": role,
                "system_prompt": system_prompt,
                "payload": payload,
                "schema": json.loads(output_schema_json),
            }
        )
        if role in {"implementer", "fixer"}:
            return json.dumps({"content": next(self._drafts)})
        if self._malformed_reviewer:
            return json.dumps({"disposition": "accept"})
        disposition = next(self._review_dispositions)
        candidate = payload["candidate"]
        review: dict[str, Any] = {
            "reviewer_id": payload["reviewer_id"],
            "contract_id": candidate["contract_id"],
            "candidate_id": candidate["candidate_id"],
            "disposition": disposition,
            "findings": [],
        }
        if disposition == "reject":
            review["findings"] = [
                {
                    "code": "unsupported_claim",
                    "detail": "The draft claims a deployment that the evidence does not establish.",
                    "basis": "The only evidence is a failing parser test.",
                }
            ]
        return json.dumps(review)


def _request() -> LoopRequest:
    return LoopRequest(
        session_ref="host:lane:0.0",
        kind="answer",
        request="Should I replace the parser framework?",
        evidence=(EvidenceItem(source="test output", text="test_parser_contract fails on nested input"),),
    )


def test_answer_is_returned_only_after_two_isolated_adversarial_acceptances() -> None:
    reasoner = _ScriptedReasoner(
        drafts=["No. Repair the existing parser and preserve its public contract."],
        review_dispositions=["accept", "accept"],
    )
    loop = AdversarialGoalLoop(reasoner=reasoner, load_goal=lambda _session_ref: _goal())

    result = loop.run(_request())

    assert result.content == "No. Repair the existing parser and preserve its public contract."
    assert result.rounds == 1
    assert [call["role"] for call in reasoner.calls] == ["implementer", "reviewer", "reviewer"]
    assert len({call["instance_id"] for call in reasoner.calls}) == 3
    assert "do not put a second JSON object" in reasoner.calls[0]["system_prompt"]
    reviewer_calls = [call for call in reasoner.calls if call["role"] == "reviewer"]
    assert all("Your only job is to find" in call["system_prompt"] for call in reviewer_calls)
    assert all("reasoning" not in call["payload"]["candidate"] for call in reviewer_calls)


def test_reviewer_findings_become_fixer_queue_then_fresh_reviewers_retry() -> None:
    reasoner = _ScriptedReasoner(
        drafts=[
            "The replacement is deployed, so replace the framework.",
            "No. The evidence supports repairing the existing parser only.",
        ],
        review_dispositions=["reject", "accept", "accept", "accept"],
    )
    loop = AdversarialGoalLoop(reasoner=reasoner, load_goal=lambda _session_ref: _goal(), max_rounds=2)

    result = loop.run(_request())

    assert result.content == "No. The evidence supports repairing the existing parser only."
    assert result.rounds == 2
    assert [call["role"] for call in reasoner.calls] == [
        "implementer",
        "reviewer",
        "reviewer",
        "fixer",
        "reviewer",
        "reviewer",
    ]
    fixer_call = reasoner.calls[3]
    assert fixer_call["payload"]["work_queue"][0]["code"] == "unsupported_claim"
    assert fixer_call["payload"]["previous_content"] == "The replacement is deployed, so replace the framework."


def test_persistent_rejection_blocks_without_returning_draft_text() -> None:
    draft = "The replacement is deployed, so replace the framework."
    reasoner = _ScriptedReasoner(drafts=[draft], review_dispositions=["reject", "accept"])
    loop = AdversarialGoalLoop(reasoner=reasoner, load_goal=lambda _session_ref: _goal(), max_rounds=1)

    with pytest.raises(GoalLoopBlocked) as exc_info:
        loop.run(_request())

    assert exc_info.value.verdict.reason == "rejected"
    assert draft not in str(exc_info.value)


def test_goal_redirect_during_review_blocks_the_stale_answer() -> None:
    calls = 0

    def changing_goal(_session_ref: str) -> GoalRecord:
        nonlocal calls
        calls += 1
        goal = _goal()
        return goal if calls == 1 else replace(goal, goal_version=2)

    reasoner = _ScriptedReasoner(drafts=["Repair the parser."], review_dispositions=["accept", "accept"])
    loop = AdversarialGoalLoop(reasoner=reasoner, load_goal=changing_goal)

    with pytest.raises(GoalLoopBlocked) as exc_info:
        loop.run(_request())

    assert exc_info.value.verdict.reason == "goal_changed"


def test_malformed_reviewer_output_fails_closed() -> None:
    reasoner = _ScriptedReasoner(drafts=["Repair the parser."], review_dispositions=[], malformed_reviewer=True)
    loop = AdversarialGoalLoop(reasoner=reasoner, load_goal=lambda _session_ref: _goal())

    with pytest.raises(GoalLoopProtocolError, match="structured-output contract"):
        loop.run(_request())


def test_ccr_reasoner_requires_loopback_and_sanitizes_api_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    settings = tmp_path / "claude-code-settings.json"
    settings.write_text("{}", encoding="utf-8")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout='{"is_error":false,"structured_output":{"content":"ok"}}', stderr="")

    monkeypatch.setattr("adapter_prototype.goal_loop.subprocess.run", fake_run)
    reasoner = CcrClaudeReasoner(
        environment={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:3468",
            "ANTHROPIC_API_KEY": "must-not-propagate",
            "ANTHROPIC_AUTH_TOKEN": "must-not-propagate",
            "CLAUDECODE": "1",
            "PATH": "/usr/bin",
        },
        settings_path=settings,
    )

    result = reasoner.complete(
        instance_id="implementer-0",
        role="implementer",
        system_prompt="draft",
        payload_json="{}",
        output_schema_json='{"type":"object"}',
    )

    assert json.loads(result) == {"content": "ok"}
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]
    assert "CLAUDECODE" not in captured["env"]
    assert captured["env"]["ANTHROPIC_API_BASE_URL"] == "http://127.0.0.1:3468"
    assert captured["env"]["CLAUDE_AGENT_API_BASE_URL"] == "http://127.0.0.1:3468"
    assert captured["command"][captured["command"].index("--model") + 1] == "codex-api/gpt-5.6-sol"
    assert captured["command"][captured["command"].index("--settings") + 1] == str(settings)
    assert "--no-session-persistence" in captured["command"]

    def result_envelope(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = '{"is_error":false,"result":"{\\"content\\":\\"ok\\"}"}'
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("adapter_prototype.goal_loop.subprocess.run", result_envelope)
    fallback = reasoner.complete(
        instance_id="fixer-1",
        role="fixer",
        system_prompt="fix",
        payload_json="{}",
        output_schema_json='{"type":"object"}',
    )
    assert json.loads(fallback) == {"content": "ok"}

    external = CcrClaudeReasoner(environment={"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, settings_path=settings)
    with pytest.raises(GoalLoopConfigurationError, match="loopback CCR"):
        external.complete(
            instance_id="reviewer-0-1",
            role="reviewer",
            system_prompt="review",
            payload_json="{}",
            output_schema_json='{"type":"object"}',
        )
