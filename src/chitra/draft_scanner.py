"""draft_scanner — periodic sweep of tmux input boxes for unsubmitted
operator drafts. Flags only; never submits or discards anything.

Formalizes the residual-sweep duty ("check EVERY box EVERY sweep") as a
mechanical guarantee: a draft sitting in an input box for one sweep cycle
too long should never be silently lost between interactive-monitor turns.

No LLM calls. Deterministic flagging only.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

import structlog

from .dispatch import (
    TmuxRunner,
    capture_dispatch_pane,
    pane_input_check,
    tmux_pane_target,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DraftFinding:
    """A target whose input box holds an unsubmitted operator draft."""

    session_ref: str
    last_line: str
    tail_hash: str

    def to_dict(self) -> dict[str, str]:
        return {"session_ref": self.session_ref, "last_line": self.last_line, "tail_hash": self.tail_hash}


@dataclass(slots=True)
class ScanResult:
    """Result of one scan pass over a list of targets."""

    findings: list[DraftFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "errors": self.errors,
        }


def scan_targets(
    session_refs: list[str],
    *,
    runner: TmuxRunner | None = None,
    local_extra: set[str] | None = None,
) -> ScanResult:
    """Scan each ``host:session:pane`` target for an unsubmitted draft.

    Reuses ``pane_input_check``'s idle-vs-draft distinction: a target is
    flagged only when the check reports ``ok=False`` with a draft-shaped
    reason (i.e. the pane is NOT idle) — a target with no capturable pane
    (e.g. unreachable host) is recorded as an error, not a false-positive
    finding.
    """
    result = ScanResult()
    for ref in session_refs:
        parts = ref.split(":")
        if len(parts) != 3:
            result.errors.append(f"{ref}: malformed session_ref (expected host:session:pane)")
            continue
        host, session, pane_field = parts
        pane = tmux_pane_target(session, pane_field)
        captured = capture_dispatch_pane(host, pane, runner=runner, local_extra=local_extra)
        check = pane_input_check(captured)
        if not captured:
            result.errors.append(f"{ref}: no pane capture (unreachable or empty)")
            continue
        if not check.ok:
            result.findings.append(DraftFinding(session_ref=ref, last_line=check.last_line, tail_hash=check.tail_hash))
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="draft-scanner", description="Flag unsubmitted operator drafts across tmux targets (chitra).")
    parser.add_argument("--targets", nargs="+", required=True, help="host:session:pane targets to scan.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = scan_targets(args.targets)
    if result.findings:
        logger.warning("draft_scanner_findings", count=len(result.findings))
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
