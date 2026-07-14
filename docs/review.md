# Review log

## 2026-07-14 — forced completion and goal-enforcement gate (v0.8.2)

`watchd` now treats a finished pane turn as a mandatory event boundary. It
runs isolated watched-session reviewers against the frozen goal and then
classifies the turn as a completion claim or a distinct non-completion turn.
Completion claims require citation-bearing deploy/live proof and a valid
delivery brief; outgoing completion claims are also recognized in `dispatchd`
without caller opt-in. `DecisionAttestation` replaces the former
`DecisionProvenance`/`ReasonedDecision` pair and binds only the exact approved
text that may be pasted. Review and attestation metadata stays in Chitra's own
logs.

## 2026-07-09 — completion gate v1 (historical, superseded by v0.8.2)

### What landed

`src/chitra/taxonomy.json` + `src/chitra/taxonomy.py` (typed loader),
The original version landed `src/chitra/completion_gate.py` plus opt-in wiring
change to `src/chitra/dispatch.py` (`DispatchOrder`'s three new optional
`completion_*` fields, a new `DispatchStatus.COMPLETION_DISPUTE` member) and
`src/chitra/dispatchd.py` (`process_one_order` runs the audit before
delivery when an order opted in). That optional, boolean-evidence contract is
no longer current; it is retained here only as design history. See
`docs/evasion-taxonomy.md` for what the
taxonomy is and its honest scope note.

### Why this was built now

This was scoped as an incident-driven v1 feature. The specific incident
labels ("fix-6"/"fix-7") that reportedly motivated it could **not** be
independently verified as real incident IDs anywhere in this fleet — no
matching record was found via repo search or the fleet's wiki. Rather than
hardcode unverifiable IDs into code, tests, or docs, this feature was built
against the two concrete, described behaviors instead:

1. An open/in-progress todo-list item surviving under a "done"/"complete"
   claim — a deferral being hidden.
2. A self-declared "done" claim with no deploy+live-verify evidence and no
   operator-authorized close — a fake-done claim.

If "fix-6"/"fix-7" are later confirmed as real, citable incidents, this
entry should be updated to reference them properly rather than treating the
absence of verification as permanent.

### Design decisions and why

- **`taxonomy.json` as packaged data, not a Python literal.** The ruleset is
  declarative data (24 `code`/`cue`/`disposition` triples), not behavior.
  Keeping it as JSON next to a thin typed loader (`taxonomy.py`) means a
  future update to the ruleset is a data-only diff, and the loader's
  Pydantic validation still catches malformed entries at load time.
- **Only 2 of 24 codes are operationalized.** `evaluate_completion_claim`
  only acts on `DEFERRAL_STUB` (todo residue + deferral-language matching)
  and `FAKE_DONE`-style patterns (the deploy/live-verify evidence-gap
  check). The other 22 codes are shipped for completeness and future
  extension, not silently assumed to be covered. This is stated explicitly
  in `docs/evasion-taxonomy.md` rather than left implicit.
- **`CompletionClaimEvent` is a new, narrow enum, not an addition to an
  existing one.** `triaged.py` has no formal event-type enum today — it
  parses opaque `<ts> <lane> <text>` lines with no typed classification.
  Rather than retrofit that minimal contract (which would be a bigger,
  riskier change touching an existing, tested parsing path), this feature
  adds a small, standalone marker scoped only to completion-gate callers.
- **Historical v1 wiring lived in `dispatchd.py`, via optional `DispatchOrder` fields, not
  a rewrite of `dispatch.py`'s core paste/verify logic.** `dispatchd.py` is
  the daemon that already owns the "before delivery, check something, and
  possibly block" pattern (see its existing lane-lock and
  already-processed-order checks) — the completion-claim audit is one more
  check of that same shape, run before `dispatch_to_tmux` is ever called.
  `dispatch.py`'s hardened tmux mechanics (paste-buffer bug fixes,
  copy-mode detection, transcript-grep verification) are untouched. The
  The three v1 fields on `DispatchOrder` were optional with safe defaults
  (`completion_todo_items: list[TodoItem] | None = None`), so every existing
  caller and every existing order is completely unaffected unless it
  explicitly opted in by setting `completion_todo_items`. v0.8.2 replaced
  the booleans with cited evidence and added the forced `watchd` boundary.
- **A new `DispatchStatus.COMPLETION_DISPUTE` member, not reuse of
  `BLOCKED`.** A disputed completion claim is a distinct outcome from "an
  operator draft is pending in the pane" (the existing `BLOCKED` case) — it
  deserves its own status so a caller/operator can distinguish "couldn't
  send because the pane was busy" from "didn't send because the claim
  itself looked fake."
- **The gate never closes anything.** `evaluate_completion_claim` and the
  `dispatchd` wiring only classify and surface (write a
  `COMPLETION_DISPUTE` result, or log a `CLEAN` audit as the proof an
  operator can use to authorize a close). Nothing in this change adds a
  close/dismiss/auto-resolve path anywhere. This matches `docs/DESIGN.md`'s
  deterministic completion-audit contract; isolated goal review is a separate
  bounded input signal.
- **`scan_deferral_language` is explicitly simple substring matching, not
  NLP.** The docstring says so directly; false positives (a phrase matching
  in an unrelated context) are an accepted tradeoff for determinism and
  auditability, consistent with the rest of this repo's deterministic,
  no-LLM-calls design.

### Verification

- `pytest -q` — 78 passed (including the 4 new dispatchd-wiring tests and
  taxonomy/gate unit tests).
- `ruff check .` — all checks passed.
- `mypy src/chitra` — success, no issues, strict mode.
