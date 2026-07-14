"""Read-only ownership lookup for external load-management callers.

Chitra answers whether supplied session references identify currently tracked
``working`` lanes on one host.  It never pauses, kills, dispatches, or mutates
state from this surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chitra.goals import GoalStatus, list_goals, session_host
from chitra.state_paths import state_dir as default_state_dir


class TrackedLane(Protocol):
    """The small portion of ``sweepd.LaneState`` needed by this query."""

    @property
    def session_ref(self) -> str: ...

    @property
    def goal_status(self) -> GoalStatus | None: ...


@dataclass(frozen=True, slots=True)
class OwnershipResult:
    """Deterministic owned/unowned partition for one request."""

    host: str
    owned: bool
    owned_session_refs: tuple[str, ...]
    unowned_session_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "owned": self.owned,
            "owned_session_refs": list(self.owned_session_refs),
            "unowned_session_refs": list(self.unowned_session_refs),
        }


@dataclass(frozen=True, slots=True)
class _GoalLane:
    """Adapt one GoalRecord to the same tracked-lane query protocol as Sweepd."""

    session_ref: str
    goal_status: GoalStatus | None


def query_ownership(*, host: str, session_refs: Iterable[str], tracked_lanes: Iterable[TrackedLane]) -> OwnershipResult:
    """Return whether any requested ref is a tracked working lane on ``host``."""
    requested = tuple(sorted(set(session_refs)))
    working = {lane.session_ref for lane in tracked_lanes if lane.goal_status == "working" and session_host(lane.session_ref) == host}
    owned = tuple(ref for ref in requested if ref in working)
    unowned = tuple(ref for ref in requested if ref not in working)
    return OwnershipResult(host=host, owned=bool(owned), owned_session_refs=owned, unowned_session_refs=unowned)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chitra-ownership",
        description="Read-only query: are these session_refs currently tracked working lanes on this host?",
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--session-ref", action="append", required=True, dest="session_refs")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        lanes = [_GoalLane(record.session_ref, record.status) for record in list_goals(args.state_dir)]
        result = query_ownership(host=args.host, session_refs=args.session_refs, tracked_lanes=lanes)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"chitra-ownership: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
