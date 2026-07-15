"""Deterministic host-pressure sampling and load-shed planning primitives."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from chitra._fsio import parse_iso8601
from chitra.account_registry import RegistryEntry
from chitra.goals import LOAD_SHED_HOLD_REASON_PREFIX, GoalRecord, GoalStatus, session_host, session_name
from chitra.lane_activity import LaneActivity
from chitra.policy_config import LoadPolicy, PausePolicy, UsagePolicy
from chitra.rate_limit_state import LoadHostState, PauseBackend


@dataclass(frozen=True, slots=True)
class PressureSample:
    """One host's memory availability and Linux PSI avg60 facts."""

    mem_available_pct: float
    memory_some_avg60: float
    memory_full_avg60: float
    cpu_some_avg60: float


@dataclass(frozen=True, slots=True)
class ShedCandidate:
    """One live lane plus the facts needed for the documented shed order."""

    session_ref: str
    goal: GoalRecord | None
    attached: bool
    last_activity_at: str = ""
    transcript_mtime: float | None = None
    backend: PauseBackend = "claude"

    @property
    def goal_status(self) -> GoalStatus | None:
        return None if self.goal is None else self.goal.status


def _meminfo_kib(path: Path) -> tuple[float, float]:
    values: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, remainder = line.partition(":")
        if not separator:
            continue
        token = remainder.strip().split(maxsplit=1)[0]
        try:
            values[name] = float(token)
        except ValueError:
            continue
    try:
        return values["MemTotal"], values["MemAvailable"]
    except KeyError as exc:
        raise ValueError(f"{path} is missing MemTotal or MemAvailable") from exc


def _psi_avg60(path: Path) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        for field in fields[1:]:
            name, separator, value = field.partition("=")
            if name == "avg60" and separator:
                try:
                    values[fields[0]] = float(value)
                except ValueError as exc:
                    raise ValueError(f"{path} has an invalid avg60 value") from exc
    return values


def sample_pressure(
    *,
    meminfo_path: Path = Path("/proc/meminfo"),
    memory_pressure_path: Path = Path("/proc/pressure/memory"),
    cpu_pressure_path: Path = Path("/proc/pressure/cpu"),
) -> PressureSample:
    """Read one local Linux pressure sample from procfs."""
    total, available = _meminfo_kib(meminfo_path)
    if total <= 0:
        raise ValueError(f"{meminfo_path} MemTotal must be positive")
    memory = _psi_avg60(memory_pressure_path)
    cpu = _psi_avg60(cpu_pressure_path)
    if "some" not in memory or "full" not in memory or "some" not in cpu:
        raise ValueError("pressure files must expose memory some/full and cpu some avg60")
    return PressureSample(
        mem_available_pct=(available / total) * 100.0,
        memory_some_avg60=memory["some"],
        memory_full_avg60=memory["full"],
        cpu_some_avg60=cpu["some"],
    )


def pressure_level(sample: PressureSample, policy: LoadPolicy) -> int:
    """Return the highest memory level, with CPU pressure capped at L1."""
    if sample.mem_available_pct < policy.l3_mem_available_pct or sample.memory_full_avg60 > policy.l3_memory_full_avg60:
        return 3
    if sample.mem_available_pct < policy.l2_mem_available_pct or sample.memory_some_avg60 > policy.l2_memory_some_avg60:
        return 2
    if (
        sample.mem_available_pct < policy.l1_mem_available_pct
        or sample.memory_some_avg60 > policy.l1_memory_some_avg60
        or sample.cpu_some_avg60 > policy.l1_cpu_some_avg60
    ):
        return 1
    return 0


def pressure_is_clear(sample: PressureSample, policy: LoadPolicy) -> bool:
    """Return whether the sample is strictly below the resume hysteresis line."""
    return (
        sample.mem_available_pct > policy.clear_mem_available_pct
        and sample.memory_some_avg60 < policy.clear_memory_some_avg60
        and sample.memory_full_avg60 <= policy.l3_memory_full_avg60
        and sample.cpu_some_avg60 <= policy.l1_cpu_some_avg60
    )


def advance_load_state(
    previous: LoadHostState | None,
    *,
    host: str,
    sample: PressureSample,
    policy: LoadPolicy,
    now: datetime,
) -> LoadHostState:
    """Apply the durable two-sweep breach/clear gates to one sample."""
    prior = previous or LoadHostState(host=host)
    observed = pressure_level(sample, policy)
    active = prior.load_level
    breach_sweeps = 0
    clear_sweeps = 0

    if observed > active:
        breach_sweeps = prior.breach_sweeps + 1 if prior.observed_level == observed else 1
        if breach_sweeps >= policy.consecutive_sweeps:
            active = observed
            breach_sweeps = 0
    elif active > 0 and pressure_is_clear(sample, policy):
        clear_sweeps = prior.clear_sweeps + 1
        if clear_sweeps >= policy.consecutive_sweeps:
            active = 0
            clear_sweeps = 0

    return replace(
        prior,
        observed_level=observed,
        breach_sweeps=breach_sweeps,
        clear_sweeps=clear_sweeps,
        load_level=active,
        mem_available_pct=sample.mem_available_pct,
        memory_some_avg60=sample.memory_some_avg60,
        memory_full_avg60=sample.memory_full_avg60,
        cpu_some_avg60=sample.cpu_some_avg60,
        updated_at=now.isoformat(),
    )


def effective_max_running(usage: UsagePolicy, load: LoadPolicy, level: int) -> int:
    """Return the operator baseline narrowed by the active pressure level."""
    baseline = usage.max_running if usage.max_running is not None else load.baseline_max_running
    level_cap = {0: load.baseline_max_running, 1: load.l1_max_running, 2: load.l2_max_running, 3: load.l3_max_running}[level]
    return min(baseline, level_cap)


def pause_policy_for_load(base: PausePolicy, load: LoadPolicy, level: int) -> PausePolicy:
    """Use the ordinary graceful deadlines except for the tighter L3 policy."""
    return load.l3_pause if level == 3 else base


def _timestamp_rank(value: str) -> float:
    if not value:
        return 0.0
    try:
        return parse_iso8601(value).timestamp()
    except ValueError:
        return 0.0


def rank_shed_candidates(candidates: list[ShedCandidate]) -> list[ShedCandidate]:
    """Apply the documented no-goal/blocked, idle, detached, active order."""

    def key(candidate: ShedCandidate) -> tuple[int, float, str]:
        if candidate.goal is None or candidate.goal_status == "blocked":
            category = 0
            recency = _timestamp_rank(candidate.last_activity_at)
        elif candidate.goal_status == "idle":
            category = 1
            recency = _timestamp_rank(candidate.last_activity_at)
        elif not candidate.attached:
            category = 2
            recency = candidate.transcript_mtime if candidate.transcript_mtime is not None else _timestamp_rank(candidate.last_activity_at)
        else:
            category = 3
            recency = _timestamp_rank(candidate.last_activity_at)
        return category, recency, candidate.session_ref

    return sorted(candidates, key=key)


def build_shed_candidates(
    goals: list[GoalRecord],
    *,
    activities: list[LaneActivity],
    registry: list[RegistryEntry],
    host: str,
) -> list[ShedCandidate]:
    """Build actionable candidates from durable goals plus watchd recency facts."""
    activity_by_ref = {item.session_ref: item for item in activities}
    registry_by_session = {item.tmux_session: item for item in registry}
    candidates: list[ShedCandidate] = []
    for goal in goals:
        if session_host(goal.session_ref) != host or goal.status == "held":
            continue
        activity = activity_by_ref.get(goal.session_ref)
        registry_entry = registry_by_session.get(session_name(goal.session_ref))
        backend_name = (
            activity.backend
            if activity is not None and activity.backend != "unknown"
            else (registry_entry.kind if registry_entry is not None else "claude")
        )
        backend: PauseBackend = "codex" if backend_name == "codex" else "claude"
        candidates.append(
            ShedCandidate(
                session_ref=goal.session_ref,
                goal=goal,
                attached=True if activity is None else activity.attached,
                last_activity_at=goal.updated_at if activity is None else activity.last_change_at,
                backend=backend,
            )
        )
    return candidates


def load_shed_reason(host: str, level: int) -> str:
    """Return the distinct hold-reason convention shared with the guard."""
    return f"{LOAD_SHED_HOLD_REASON_PREFIX}{host}:{level}"
