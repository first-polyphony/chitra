"""Tests for deterministic host-pressure sampling and load-shed planning."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.goals import GoalRecord
from chitra.load_shed import (
    PressureSample,
    ShedCandidate,
    advance_load_state,
    effective_max_running,
    pause_policy_for_load,
    pressure_is_clear,
    pressure_level,
    rank_shed_candidates,
    sample_pressure,
)
from chitra.policy_config import LoadPolicy, PausePolicy, UsagePolicy
from chitra.rate_limit_state import LoadHostState

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _sample(*, available: float = 80, memory_some: float = 0, memory_full: float = 0, cpu_some: float = 0) -> PressureSample:
    return PressureSample(available, memory_some, memory_full, cpu_some)


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (_sample(available=25), 0),
        (_sample(available=24.9), 1),
        (_sample(available=15), 1),
        (_sample(available=14.9), 2),
        (_sample(available=8), 2),
        (_sample(available=7.9), 3),
        (_sample(memory_some=10), 0),
        (_sample(memory_some=10.1), 1),
        (_sample(memory_some=25), 1),
        (_sample(memory_some=25.1), 2),
        (_sample(memory_full=10), 0),
        (_sample(memory_full=10.1), 3),
        (_sample(cpu_some=60), 0),
        (_sample(cpu_some=99), 1),
    ],
)
def test_pressure_level_uses_strict_ladder_thresholds(sample: PressureSample, expected: int) -> None:
    assert pressure_level(sample, LoadPolicy()) == expected


def test_pressure_sampler_reads_memavailable_and_psi_avg60(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    memory = tmp_path / "memory"
    cpu = tmp_path / "cpu"
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 240 kB\n", encoding="utf-8")
    memory.write_text("some avg10=1.00 avg60=11.50 avg300=2.00 total=1\nfull avg10=0 avg60=0.50 avg300=0 total=1\n")
    cpu.write_text("some avg10=1 avg60=61.00 avg300=1 total=1\nfull avg10=0 avg60=0 avg300=0 total=0\n")

    sample = sample_pressure(meminfo_path=meminfo, memory_pressure_path=memory, cpu_pressure_path=cpu)

    assert sample == PressureSample(24.0, 11.5, 0.5, 61.0)
    assert pressure_level(sample, LoadPolicy()) == 1


def test_breach_and_clear_both_require_two_persistable_sweeps() -> None:
    policy = LoadPolicy()
    breach = _sample(available=14)

    first = advance_load_state(None, host="tophand", sample=breach, policy=policy, now=NOW)
    second = advance_load_state(first, host="tophand", sample=breach, policy=policy, now=NOW)

    assert first.load_level == 0 and first.breach_sweeps == 1
    assert second.load_level == 2 and second.breach_sweeps == 0

    clear = _sample(available=31, memory_some=4.9)
    assert pressure_is_clear(clear, policy) is True
    clearing = advance_load_state(second, host="tophand", sample=clear, policy=policy, now=NOW)
    cleared = advance_load_state(clearing, host="tophand", sample=clear, policy=policy, now=NOW)
    assert clearing.load_level == 2 and clearing.clear_sweeps == 1
    assert cleared.load_level == 0 and cleared.clear_sweeps == 0


def _goal(session_ref: str, status: str = "working") -> GoalRecord:
    return GoalRecord(
        session_ref=session_ref,
        goal="Keep the tracked lane within safe host capacity limits.",
        done_when="The lane finishes without unsafe host pressure.",
        source="task",
        status=status,  # type: ignore[arg-type]
    )


def test_shed_priority_is_blocked_idle_detached_then_active() -> None:
    candidates = [
        ShedCandidate("h:active:0.0", _goal("h:active:0.0"), attached=True, last_activity_at="2026-07-14T12:00:00+00:00"),
        ShedCandidate("h:detached-new:0.0", _goal("h:detached-new:0.0"), attached=False, transcript_mtime=20),
        ShedCandidate("h:idle:0.0", _goal("h:idle:0.0", "idle"), attached=True, last_activity_at="2026-07-14T10:00:00+00:00"),
        ShedCandidate("h:blocked:0.0", _goal("h:blocked:0.0", "blocked"), attached=True),
        ShedCandidate("h:no-goal:0.0", None, attached=True),
        ShedCandidate("h:detached-old:0.0", _goal("h:detached-old:0.0"), attached=False, transcript_mtime=10),
    ]

    ranked = [candidate.session_ref for candidate in rank_shed_candidates(candidates)]

    assert ranked[:2] == ["h:blocked:0.0", "h:no-goal:0.0"]
    assert ranked[2:] == ["h:idle:0.0", "h:detached-old:0.0", "h:detached-new:0.0", "h:active:0.0"]


def test_load_caps_override_usage_baseline_and_l3_uses_tighter_graceful_deadlines() -> None:
    load = LoadPolicy()
    assert [effective_max_running(UsagePolicy(), load, level) for level in range(4)] == [8, 6, 4, 2]
    assert effective_max_running(UsagePolicy(max_running=3), load, 1) == 3

    ordinary = PausePolicy()
    assert pause_policy_for_load(ordinary, load, 2) is ordinary
    assert pause_policy_for_load(ordinary, load, 3) == load.l3_pause
    assert load.l3_pause.quiescence_timeout_seconds < ordinary.quiescence_timeout_seconds


def test_load_state_fixture_retains_last_shed_stack() -> None:
    state = LoadHostState(host="tophand", load_level=2, shed_lanes=("h:a:0.0", "h:b:0.0"))
    assert state.shed_lanes[-1] == "h:b:0.0"
