# Design notes

## Origin

chitra began as an internal extraction: a tmux dispatch function that had two real, silently-triggering bugs (documented in `src/chitra/dispatch.py`'s module docstring), pulled out into its own hardened, tested library alongside a small set of daemons that turn "occasionally invoke this function from an interactive AI session" into "always-on, deterministic, systemd-supervised background service."

## What chitra is not

chitra deliberately does not do reasoning, drafting, or decision-making. If a request would add an LLM call, a decision-making surface, or general-purpose agent orchestration to this repo, it belongs in a different, higher-level system that *uses* chitra as its delivery/dedup layer — not in chitra itself. This is a scope statement, not an oversight: keeping chitra small is a design goal.

## Distribution and packaging

- **Distribution:** git-installable (`pip install git+https://...@<tag>`) for now, not yet on PyPI. chitra's current consumers install a pinned revision onto systemd hosts they provision themselves — PyPI's advantages (name-based discovery, version-range resolution for downstream packagers) don't apply yet. The build backend (hatchling, standards-based `pyproject.toml`) keeps a future PyPI release a small, mechanical step rather than a rewrite.
- **Layout:** `src/chitra/` (src-layout), not a flat top-level package. This ensures `import chitra` always resolves to the installed wheel, never to a loose working-directory copy — important for a package whose main job is running as an installed systemd service.
- **Versioning:** plain SemVer starting in the 0.x range (currently 0.2.0). SemVer reserves 0.y.z for "anything may change" — appropriate before there's a real external consumer depending on a stable interface. 1.0.0 is reserved for the day this repo is public and the maintainers are willing to promise CLI/API stability; a "v1.1" *milestone* label in an issue tracker is a separate thing from the released package version.

## Single-writer rule (why `LaneLock` exists)

A tmux-hosted AI agent session is, from the outside, just a process with a terminal attached. It's tempting to assume you can deliver a message to it two different ways — inject text via tmux, or resume/replay into its own session transcript via whatever resume mechanism the agent's CLI provides — and pick whichever is convenient. In testing, doing so concurrently against a **live, actively-running** session caused a real, reproducible failure: the out-of-band delivery silently appended to the session's own transcript while racing its in-flight writes, corrupting its next turn with no visible error. `LaneLock` exists specifically to make "two writers, one session, at once" structurally impossible: `dispatchd` acquires an exclusive, file-based lock for a session id before attempting delivery and releases it after, and a second acquisition attempt against an already-held lock fails or blocks rather than silently proceeding.

The tmux-injection recipe (documented in the README) is the only channel this repo considers safe for delivering to a **live** session. Any out-of-band resume/replay mechanism a downstream integrator wants to use as a *fallback* must first perform an explicit liveness check (confirm the target session is genuinely detached/stopped) — chitra ships the `LaneLock` enforcement and a liveness-check stub for this, but the fallback delivery path itself is intentionally out of scope for this release; building it without the liveness check is the mistake this whole section is warning against.

## Build/CI history

This repo was extracted from a private monorepo where it was originally developed, tested (lint/typecheck/full test suite green before extraction), and used internally. The extraction process stripped internal deployment specifics (hostnames, internal service names, internal wiki/documentation references) that don't belong in a public tool's source or docs, replacing hardcoded internal defaults with generic, empty-by-default configuration. See the README's "note on the observer pattern" for how an internal read-only consumer stays decoupled from this repo without needing to be described here.
