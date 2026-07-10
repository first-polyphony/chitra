# Orchestration loop pattern catalog (naming reference)

This is prior art, not a spec: a catalog of nine named orchestration loop
patterns from a "Fable 5 builder's guide," recorded here purely as a naming
reference for chitra's `task_type` values. **Chitra does not implement,
execute, or choose between any of these patterns.** It only carries whatever
`task_type` string a caller provides through to `routing.yaml`'s lookup,
unchanged — see the README's "Routing config" section and
`chitra.routing_config` for the actual mechanics.

A deployment populating `routing.yaml` might reasonably use `task_type`
values like `heartbeat`, `orchestrator-workers`, `executor-advisor`,
`trust-ledger`, `standing-goals`, `quorum`, `sparring`, `compost`, `ratchet`
if that deployment's own orchestration follows these named patterns. Chitra
has no opinion on this — it's just a real-world naming convention worth
knowing about when picking `task_type` keys.

Each entry gives the pattern's name, its one-line mechanism, and the
expensive/cheap model split it uses.

1. **Heartbeat** — one tick of a `loop.sh`: Signals -> Triage (cheap model)
   -> Conductor (Fable 5, high effort, read-only) -> Worker (cheap model)
   -> Verifier (fresh Fable 5 instance, high effort) -> Gate (`verify.sh`,
   deterministic check) -> trust ledger (auto-merge / open PR, or queue for
   review, or fail with a logged flag). Split: cheap for triage and
   execution, expensive (Fable 5) for conducting and verifying.

2. **Orchestrator-Workers** — Fable 5 plans, cheap models execute: one
   Orchestrator (Fable 5) fans out to 3 Workers (cheap model, e.g. Sonnet),
   each running its own independent loop. Split: one expensive planner, N
   cheap executors.

3. **Executor-Advisor (Oracle)** — a cheap model runs every turn
   (Executor), and only calls out to an expensive model (Advisor, Fable 5)
   on demand when it needs judgment/advice — cheap loop every turn,
   expensive judgment on demand. Split: cheap by default, expensive only
   on escalation.

4. **Trust Ledger** — autonomy is earned per-skill, not per-loop: a watch
   state (few runs or low success rate) graduates to queue (verified, still
   human-reviewed) graduates to auto (many runs, high success rate, ships
   unattended) — with automatic demotion back to watch/alert on any
   failure. Split: not model-tiered; a trust-state machine gating how much
   human review a skill's output still needs.

5. **Standing Goals** — a goal becomes a strict, machine-checkable
   predicate written to a file (`goals/<name>.md`), verified on every
   push/cron by a script; a violation flips a status and wakes a separate
   Sentinel process, which handles it outside the normal pipeline. Split:
   deterministic script for verification; a separate process (model tier
   unspecified) only wakes on violation.

6. **Quorum** — three cheap models vote before an expensive one wakes up:
   Signals go to 3 cheap-model Voters; only if >=2-of-3 agree does the
   expensive model (Fable 5, Conductor) actually activate — avoiding
   needless expensive-model cost on quiet/no-op signals. Split: 3 cheap
   voters gate one expensive conductor.

7. **Sparring** — an adversarial Breaker/Builder loop (one proposes, one
   critiques/breaks) that converges on presenting a decision to a human.
   Split: not specified beyond the two adversarial roles.

8. **Compost** — multiple rejected or failed attempts feed into a
   synthesis/composting step that distills them into a small number
   (<=3) of real proposals for human sign-off, rather than each failure
   being wasted. Split: not specified beyond the composting/synthesis step.

9. **Ratchet** — a measure -> one change -> re-measure loop that only
   "holds" (keeps) a change if the re-measurement doesn't regress,
   otherwise reverts — a one-way ratchet against regression. Split: not
   specified; the mechanism is the measure/change/re-measure/hold-or-revert
   loop itself.
