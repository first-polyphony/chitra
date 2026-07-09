"""Deterministic relay/triage daemons for the fleet monitor off-foreground design.

Phase 0+1: extraction and hardening of the tmux dispatch path, plus the
deterministic daemons (dispatchd, triaged) that move relay/triage work off
the interactive Claude Code foreground loop. No LLM calls live here — this
package is plumbing only.
"""

__all__: list[str] = []
