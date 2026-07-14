# Evasion taxonomy

## What this is

`src/chitra/taxonomy.json` (loaded by `src/chitra/taxonomy.py`) ships a fixed
ruleset for detecting common AI-agent completion-evasion patterns, distilled
from operational observation of AI coding agents. Each entry has:

- `code` — a short, stable identifier (e.g. `DEFERRAL_STUB`, `FAKE_DONE`).
- `cue` — the observable behavior that triggers the code.
- `disposition` — the response class (`NUDGE`, `DECISION`, `DEAD_STOP`,
  `ENVIRONMENTAL`) the ruleset's originating system assigns it.

There are 24 entries. This is the real, verified count — do not assume it
should be a round number.

## Where it lives

Data: `src/chitra/taxonomy.json`. Loader: `src/chitra/taxonomy.py`
(`load_taxonomy()` returns a cached tuple of validated `TaxonomyEntry`
Pydantic models). Consumer: `src/chitra/completion_gate.py`.

## How the completion gate uses it

`watchd` calls `evaluate_turn_end` automatically whenever a pane finishes a
turn. A turn with no completion claim is recorded as finished but unverified;
a completion claim is passed to `evaluate_completion_claim` and audited
against concrete, checkable behaviors:

1. An open/in-progress todo-list item surviving under a done claim
   (`check_todo_residue`) — a deferral being hidden.
2. A self-declared done claim with no concrete deploy SHA and live probe/log
   citations (`evidence_gap`) — a bare boolean or "CI evidence" assertion does
   not count.
3. Missing per-item verification, an invalid three-question delivery brief,
   or a blocked-todo posture with no disclosed open ask/blocker.

`scan_deferral_language` additionally does simple, case-insensitive substring
matching against a fixed phrase list (`"you'll need to"`, `"TODO"`,
`"conditionally healthy"`, `"parse-only"`, `"CI evidence"`, etc.).

## Honest scope note

Only two of the 24 codes are actually operationalized by the completion
gate today: **`DEFERRAL_STUB`** (via `scan_deferral_language` and
`check_todo_residue`) and **`FAKE_DONE`**-style patterns (via the
deploy/live-verify evidence-gap check). The other 22 codes, and the
`disposition` field itself (`NUDGE`/`DECISION`/`DEAD_STOP`/`ENVIRONMENTAL`),
are inherited labels from the ruleset's own structure — they are carried
here for completeness and future extension, not currently acted on by any
chitra code path. A future PR that wants to operationalize another code
(e.g. `SILENT_TOOL_FAILURE`) should extend `completion_gate.py` explicitly
rather than assume the taxonomy's mere presence implies coverage.

## Why this exists in chitra (not just the source system)

This taxonomy was distilled from a real, more detailed internal ruleset used
elsewhere in the fleet for AI-agent session stewardship. That internal
system and its component names are deliberately not referenced here or
anywhere in chitra's shipped files — this repo is public, and internal
component names stay out of public docs, per this codebase's established
convention (see `docs/DESIGN.md`'s note on internal deployment specifics
being stripped at extraction time). The 24 codes above are the genericized,
standalone ruleset; nothing about their use here depends on the internal
system's identity.

An earlier internal estimate put this ruleset's size at 26 entries. The
verified count, used throughout this document and the data file, is 24 —
the estimate was wrong and should not be treated as authoritative anywhere
in this repo.
