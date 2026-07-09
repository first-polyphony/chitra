# Changelog

All notable changes to this project are documented here, in the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. This project uses [Semantic Versioning](https://semver.org/), currently in the 0.x range (see `docs/DESIGN.md` for why 1.0.0 is reserved for later).

## [Unreleased]

### Changed
- Renamed the `POLYPHONY_CHITRA_*` environment variables to `CHITRA_*` (e.g. `CHITRA_LOCAL_HOST`, `CHITRA_LANE_LOCK_DIR`) and the default `/var/lib/polyphony-chitra/` state paths to `/var/lib/chitra/`, so the tool's public interface no longer names an internal project affiliation. If you set any `POLYPHONY_CHITRA_*` variable or rely on the old default paths, update to the `CHITRA_*` names / `/var/lib/chitra/` paths.

### Added
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
