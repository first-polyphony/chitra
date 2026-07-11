# Changelog

All notable changes to this project are documented here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project uses [Semantic Versioning](https://semver.org/), currently in the 0.x range (see `docs/DESIGN.md` for why 1.0.0 is reserved for later).

## [Unreleased]

### Added
- `chitra.convlog` v2 operator briefs now record a plain-language subject and progress summary, render those details as a grounding lead-in, and keep v1 conversation-log entries readable.
- Roster reports now list every unreviewed published artifact by title and complete, copyable URL in deterministic oldest-first order.

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
