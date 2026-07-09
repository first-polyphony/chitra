"""Deterministic relay/triage daemons for the fleet monitor off-foreground design.

Phase 0+1: extraction and hardening of the tmux dispatch path, plus the
deterministic daemons (dispatchd, triaged) that move relay/triage work off
the interactive Claude Code foreground loop. This package delivers to and
observes those LLM-driven sessions, but no LLM calls live in its own code
path — it is deterministic plumbing only.
"""

__all__: list[str] = []
