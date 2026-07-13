# Changelog

All notable changes to this project are documented here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project uses [Semantic Versioning](https://semver.org/), currently in the 0.x range (see `docs/DESIGN.md` for why 1.0.0 is reserved for later).

## [Unreleased]

### Changed
- Raised the default graceful-pause thresholds to 92% for the five-hour
  window and 95% for the seven-day window, with approaching warnings at
  80% and 90%, respectively.

## [0.8.1] - 2026-07-12

A hardening patch, not a feature release. An independent adversarial review
(`docs/SOL-ADVERSARIAL-REVIEW.md`) of the two open feature PRs this
consolidates (#54 board-table-colors, #55 graceful-session-pause-resume)
found two BLOCKER-severity defects and five HIGH-severity defects across
dispatch delivery, pause/resume durability, and account-identity handling.
This release fixes all seven, replaces the two PRs (superseded, closed),
and does **not** introduce any new user-facing feature beyond what #55
already proposed — the scope is entirely "make the same proposed behavior
actually durable and correct."

Every fix below is paired with new fault-injection, concurrency, or
kill-point tests that exercise the failure path directly (not just the
happy path) — see the PR description for the full per-finding table.

### Fixed
- **Dispatch queue: frozen orders were silently discarded, not held.** A
  session held for a rate-limit reason now durably defers ordinary orders
  (`queue/deferred/`, no result file written) instead of returning a
  terminal `BLOCKED` result and archiving them to `processed/`. Once the
  hold clears, `chitra.dispatchd.requeue_deferred_for_session` atomically
  returns the backlog to `orders/` in original FIFO order for exactly-once
  delivery.
- **Pause/resume was two uncoordinated writes, not a transaction.**
  `chitra.rate_limit_guard` is now driven by a durable, crash-safe
  transaction outbox (`chitra.rate_limit_state`) walking
  `pause_requested → checkpoint_sent → stop_sent → awaiting_quiescence →
  held → resume_requested → resume_sent`. Every transition consumes a real
  `chitra.dispatchd` delivery result; every waiting phase is bounded by a
  configurable deadline (`PolicyConfig.pause`) with bounded retries, then
  escalates for operator visibility without ever dropping the freeze. A
  pause now enqueues a second, deterministic `/goal clear` stop order after
  the checkpoint is confirmed, then verifies the target session's own
  transcript has gone quiet before recording `held` — a graceful pause
  proves the turn stopped, it does not just label the goal "held". A resume
  enqueues its re-arm nudge, waits for confirmed delivery, and only then
  clears the hold and requeues the deferred backlog — never the reverse.
- **`dispatchd` could double-deliver a nudge on crash or worker race.**
  Orders are now atomically claimed (renamed into `queue/in_flight/`)
  before any delivery attempt, so two racing workers can never both process
  the same order file. A send-nonce marker plus an owner-pid marker let a
  restarted daemon tell a live in-progress claim apart from one abandoned
  by a crashed worker, and reconcile a possible crash-after-paste via the
  same transcript-grep evidence `dispatch_to_tmux` itself uses — never
  blindly re-pasting into a live pane.
- **The rate-limit freeze check ran before the lane lock (TOCTOU), and its
  bypass was an unrestricted public boolean.** The freeze is now checked
  under the same lane-lock hold used for delivery, closing the race window.
  `DispatchOrder.bypass_rate_limit_freeze` is honored only when the order's
  `task_type` is also one of dispatchd's own sealed internal task types —
  an arbitrary queue writer can no longer invent a bypass. `--goals-root`
  is now actually forwarded from the CLI into `run_once`/`run_forever`
  (it was accepted by the parser but silently dropped before reaching
  either).
- **Unknown-account sessions were silently merged.** `chitra.usage.
  evaluate_grouped` no longer groups every blank-`account` session into one
  shared identity — each is isolated so one hot, unknown-identity session
  can never attribute its pause verdict to an unrelated unknown sibling. A
  new `chitra.account_registry` tracks each lane's last-known account
  identity within a bounded freshness window, surfacing a missing snapshot
  or a mid-session account change as an operator escalation instead of
  silently doing nothing. Codex host-wide fan-out remains an explicit,
  documented, fail-closed gap (no per-lane Codex usage snapshot exists yet)
  — never silently attempted.
- **The `goals.json` store was atomic per write but not against concurrent
  writers.** `upsert_goal`, `redirect_goal`, `close_goal`, and every
  read-modify-write helper (`hold_goal`, `resume_goal`, `add_ask`,
  `resolve_ask`, `update_now`) now serialize their full read-modify-write
  transaction with a `flock`-protected critical section, closing the
  lost-update window where a concurrent writer's mutation could be silently
  erased by whichever `os.replace()` landed last.
- **Box-format roster cells overflowed on emoji/CJK content.** `_wrap_cell`
  now wraps by terminal display width (matching `_pad`'s own measurement),
  not `textwrap.wrap`'s code-point count — a wide-character-heavy Goal/Now/
  Needs cell no longer produces a wider physical line than its column.
  Overlong unbroken tokens are hard-split by display width, never by code
  points. The `cards`/`box` default is **unchanged** (still `cards`) —
  which format the operator wants by default remains an open decision; it
  is now a single named constant (`board.ROSTER_DEFAULT_FORMAT`) so
  resolving that decision later is a one-line change.

### Changed
- Version escalation is frozen at the `0.8.x` line. Six minor-version
  increments landed in roughly two days without six independently hardened
  maturity steps behind them; an independent review assessed the honest
  feature maturity at 0.3.2-equivalent. The already-published `v0.2.0`/
  `v0.7.0`/`v0.8.0` tags are immutable and are not being deleted or
  rewritten. Only `0.8.x` hardening patches ship until transactionality,
  idempotence, and evidence-backed status are demonstrated with the kind of
  fault-injection tests this release adds — no `0.9` without an explicit
  operator go-ahead.

## [0.8.0] - 2026-07-11

### Added
- Sticky strategic goal records with redirect-only revisions, deterministic specification checks, and policy-configured canonical guidance documents.

## [0.7.0] - 2026-07-11

### Added
- `chitra.convlog` v2 operator briefs now record a plain-language subject and progress summary, render those details as a grounding lead-in, and keep v1 conversation-log entries readable.
- Roster reports now list every unreviewed published artifact by title and complete, copyable URL in deterministic oldest-first order.
- `chitra.capabilities`: a packaged, strictly validated capability manifest with a reversible, time-boxed runtime toggle overlay and `chitra-capabilities` CLI. It exposes only enabled tool commands as MCP-shaped definitions; daemons remain non-toggleable.
- `chitra.merge_queue`: pure caller-supplied merge-queue hygiene decisions, chitra-owned hold markers, an atomic `queue_holds.json` store, append-only `queue_hygiene.jsonl`, and the gated `chitra-queue` CLI. It cannot merge, approve, branch, invoke `gh`, or make network calls.

## [0.5.0] - 2026-07-11

### Added
- `chitra.usage`: account-aware evaluation that attributes fresh rate-limit snapshots to every session on the same account, including stale siblings.

## [0.4.0] - 2026-07-10

### Added
- `chitra.usage`: strict usage-snapshot reading and pure threshold evaluation for Claude Code sidecar files and the local Codex account. It reports `ok`, `approaching`, or `pause` without pausing, resuming, dispatching to, or otherwise deciding for a lane.
- Goal hold bookkeeping: `chitra-goals hold`, `resume`, and `due` preserve the monitor-stated goal while recording an explicit hold reason and optional ISO8601 resume time. Timed holds are listed deterministically for operator review; operator-parked holds are never automatically surfaced as due.

## [0.3.0] - 2026-07-10

### Added
- `chitra.goals`: deterministic, per-lane goal store and roster — records the monitor's stated goal, completion condition, and current status (`working`/`held`/`idle`/`blocked`/`done-pending-verification`/`done-pending-close`) with no LLM call in its own code path. Exposed via the `chitra-goals` CLI (`roster`, `scan-asks`).
- Persistent open-asks tracking: `chitra-goals scan-asks` reads the full last assistant message from a lane's transcript (never a fixed-size pane tail) and, with `--record`, holds each numbered `awaiting ruling`/open-question line in the lane's durable record.
- Operator-facing roster rendering (`roster --format box`) with a color legend, a `Needs` column, a computed marker, and an idle-by-design (🟡) state.
- Receiving-board pipeline reconciliation (`chitra.board_updater` path) so triaged events flow into `facts.json` consistently.
- `task_type → model/harness` routing (`chitra.routing_config`): a structured `routes` config that resolves a concrete model+harness (+zdr) at dispatch and records the resolved selection structurally in the signed ledger, alongside the existing opaque `routing_hint` pass-through.
- `chitra.watchd` tmux pane-change emitter.

### Fixed
- Cross-host confirmation: the remote-dispatch path now expands the remote transcript root and matches delivery markers with local-normalized comparison over ssh, so a delivery to a session on another host is confirmed rather than reported unlocatable.
- Dispatch robustness: pane-capture fallback so an unlocatable transcript is no longer treated as `FAILED`; dimmed placeholder input rows are treated as idle, not a draft; Claude transcript writes are allowed before verify.

### Changed
- Renamed the `POLYPHONY_CHITRA_*` environment variables to `CHITRA_*` (e.g. `CHITRA_LOCAL_HOST`, `CHITRA_LANE_LOCK_DIR`) and the default `/var/lib/polyphony-chitra/` state paths to `/var/lib/chitra/`, so the tool's public interface no longer names an internal project affiliation. If you set any `POLYPHONY_CHITRA_*` variable or rely on the old default paths, update to the `CHITRA_*` names / `/var/lib/chitra/` paths.
- Test coverage for `liveness_check()` (malformed `session_ref`, remote-host assume-live, local-host with/without an attached tmux client).

## [0.2.0] - 2026-07-09

### Added
- Extracted, hardened `chitra.dispatch` tmux delivery library: fixes a missing `-p` bracketed-paste flag and a missing tmux copy-mode check, both silent failure modes in the original internal implementation.
- `chitra.dispatchd`: JSON order/result queue daemon with `LaneLock` single-writer enforcement (crash-safe, no double-delivery).
- `chitra.triaged`: state-transition dedup daemon over a tailed events log.
- `chitra.draft_scanner`: flags unsubmitted drafts in tmux input boxes (flag-only, never submits/discards).
- `chitra.board_updater`: validated `facts.json` writer with backup/rollback.
- `chitra.ledger`: HMAC-signed, append-only delivery ledger — every successfully delivered `[C]`-tagged message is signed and logged automatically, proving both "this was delivered" and "this was never sent."

## [0.1.0] - 2026-07-09

### Added
- Initial internal extraction (pre-public-repo).
