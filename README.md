# chitra

Deterministic, systemd-supervised relay and dedup daemons for delivering text into (and watching state from) `tmux`-hosted AI-agent sessions — built for fleets of Claude Code sessions, but the tmux-level mechanics are agent-agnostic.

**Scope statement:** chitra's whole purpose is to deliver messages to, and observe the state of, sessions that are themselves driven by an LLM (e.g. Claude Code instances running in tmux) — that IS its job. What chitra itself never does is call an LLM API to decide what to say or how to act: every decision about message content, timing, and target is made by the caller (a human operator or an orchestrating session) before it reaches chitra. chitra's own code path is 100% deterministic plumbing — it delivers text reliably, verifies delivery against evidence, and dedups repeated state, but makes no drafting or judgment calls of its own. If a request would add reasoning/decision logic or an LLM call *inside chitra's own code*, that's out of scope here; chitra stays deliberately lightweight.

## Why "chitra"

The name is a short form of *Chitragupta*, a figure from Hindu tradition described as the divine registrar and keeper of a complete, accurate ledger of deeds — one who records, verifies what is recorded, and reports to the decision-maker, but does not act on that decision-maker's behalf. That is this package's exact contract: it observes, verifies against artifacts, and relays — it never decides, and it never does an agent session's work for it. The name is used respectfully as a functional reference, not as religious imagery.

## What's in this repo

chitra delivers to and observes LLM-driven sessions from the outside; its own code path makes no LLM calls — no drafting, no judgment calls, deterministic relay/plumbing only.

- **`chitra.dispatch`** — a hardened tmux dispatch library: delivers text into a tmux pane using a verified recipe (checks for tmux copy-mode and cancels it, uses `paste-buffer -p` for a proper bracketed-paste wrapper, then confirms delivery by grepping the target session's own transcript rather than trusting a pane screenshot). Includes `LaneLock`, a file-based single-writer lock: at most one writer may deliver to a given session id at a time.
- **`chitra.dispatchd`** — a daemon that drains a JSON order queue (`queue/orders/*.json`), delivers each order via `chitra.dispatch` under a `LaneLock`, writes a result JSON, and moves the processed order aside. Crash-safe: a partially-processed order is never redelivered.
- **`chitra.triaged`** — a daemon that tails an events log and emits a "triage event" only when a session's state signature actually changes, not on every repeated poll.
- **`chitra.draft_scanner`** — a periodic scan of `host:session:pane` targets for an unsubmitted draft sitting in the tmux input box. Flags only; never submits or discards anything.
- **`chitra.board_updater`** — a deterministic, validated writer for a small JSON "board" document: backs up the existing file, validates the new one against caller-supplied constraints, writes, and rolls back automatically if validation fails.
- **`chitra.ledger`** — HMAC-signed, append-only delivery ledger: every successfully delivered message is automatically signed and logged, so any reader can later verify "chitra delivered this exact message at this time" or, just as importantly, prove "chitra never sent this."

## The verified tmux-injection recipe

The one safe path for delivering text into a **live** tmux session:

1. `tmux display-message -p -t <target> '#{pane_in_mode}'` — if `1` (the pane is in copy-mode, which silently swallows input), run `tmux send-keys -X cancel` and wait briefly.
2. `printf '%s' "$text" | tmux load-buffer -b <name> -`
3. `tmux paste-buffer -p -b <name> -t <target>` — the `-p` flag is mandatory; without it, newlines in multi-line text act as real Enter keypresses and the message can self-submit early.
4. `tmux send-keys -t <target> Enter`
5. Verify by grepping the target session's own transcript file for the delivered text — a pane screenshot or "looks sent" heuristic is not evidence that delivery happened.

## Single-writer rule

`dispatchd` owns explicit lane-ownership locking (`LaneLock`): one writer per session id, acquired before any delivery attempt and released after. Acquiring a lock for an already-locked session id fails/blocks rather than silently proceeding — two writers racing to deliver to the same session at once is exactly the failure mode this exists to prevent (a second, out-of-band delivery mechanism racing a live session's own process can silently corrupt its next turn).

## Message tag and delivery authentication

Every dispatched message carries a `tag` (default `"[C]"`, marking it as a chitra relay delivery — distinct from an operator or user typing directly into a pane, which needs no tag and no authentication, since the pane itself is that operator's own channel).

`chitra.ledger` closes a real gap: a relayed message otherwise has no way for the receiving session to distinguish "chitra genuinely delivered this" from an unauthenticated claim. On every **successful** delivery (never on blocked/failed attempts), `dispatchd` automatically signs an HMAC-SHA256 over `(timestamp, session_ref, tag, message_hash)` using a key stored in the state directory (generated on first use), and appends the signed record to an append-only JSONL ledger. No extra step, no added friction to a normal send.

This proves two things, symmetrically:
- **Positive**: "chitra delivered this exact message to this session at this time" — recompute the HMAC over the ledger entry and compare.
- **Negative**: "chitra did NOT send this" — the ledger is append-only, so a message's absence from it is itself the proof no such delivery happened.

See `chitra.ledger.verify_delivery` for the check as a function call, or read `ledger.jsonl` directly (it's a plain, documented JSONL format) if the verifying reader doesn't have chitra installed.

## Install

Requires Python 3.12+ and `tmux` on the host (chitra shells out to the `tmux` binary; there is no Python tmux dependency to install).

```bash
pip install git+https://github.com/first-polyphony/chitra.git@<tag>
```

(Not yet on PyPI — see `docs/DESIGN.md` for the packaging rationale. This will change once the project has real external installers.)

For local development:

```bash
git clone https://github.com/first-polyphony/chitra.git
cd chitra
pip install -e '.[test]'
pytest
```

## Running the daemons

Two CLI entrypoints are installed: `dispatchd` and `triaged` (plus `draft-scanner` as an ad-hoc tool). Example systemd units — with placeholder paths and a placeholder service user you must fill in — live under `packaging/systemd/`. Copy, edit the placeholders, and install as `chitra-dispatchd.service` / `chitra-triaged.service`.

## Configuration

All configuration is via CLI flags (see `--help` on each entrypoint) or a small number of environment variables read by `chitra.dispatch`:

| Env var | Default | Read by | Notes |
|---|---|---|---|
| `REMOTE_DISPATCH_HOSTS` | *(empty — local delivery only)* | `chitra.dispatch` | Comma-separated allowlist of remote hostnames dispatch may target over ssh |
| `CHITRA_LOCAL_HOST` | *(unset)* | `chitra.dispatch` | Override for this host's own name, for local-vs-remote detection in tests/unusual setups |
| `CHITRA_LANE_LOCK_DIR` | a `chitra-locks` dir under the system temp dir | `chitra.dispatch` | Directory for `LaneLock` lock files |
| `CHITRA_CLAUDE_PROJECTS` | `~/.claude/projects` | `chitra.dispatch` | Root directory searched for transcript-grep verification |
| `CHITRA_SSH_CONFIG` | *(unset)* | `chitra.dispatch` | Optional `ssh -F <path>` config file for remote dispatch |
| `CHITRA_SSH_IDENTITY` | *(unset)* | `chitra.dispatch` | Optional `ssh -i <path>` identity file for remote dispatch |
| `CHITRA_SSH_KNOWN_HOSTS` | *(unset)* | `chitra.dispatch` | Optional `UserKnownHostsFile` for remote dispatch |

`chitra.dispatchd` and `chitra.triaged` take their queue/log/state paths as CLI flags rather than environment variables (see `--help`).

## A note on the observer pattern

Internally, chitra is paired with a read-only observer that consumes its event/state output for learning and reflection purposes — it never writes back into chitra's queues, locks, or state. That coupling is intentionally not shipped here: chitra exposes plain, documented file/queue formats (JSON orders/results in `chitra.dispatch`'s `DispatchOrder`/`DispatchResult` models, the `<ISO8601> <LANE_ID> <TEXT>` events-log line format documented in `chitra.triaged`'s module docstring, and the JSON triage log it emits) precisely so that *any* read-only consumer — an internal tool, a dashboard, a future OSS project — can be built against them without chitra needing to know it exists. If you're building a read-only observer against chitra's output, those module docstrings are the whole contract — nothing else to read.

## Roadmap

See `docs/ROADMAP.md` for the v1.1 plan (kept intentionally small — chitra's lightweightness is a design goal, not an oversight).

## Authors

Built with [Claude](https://claude.com/claude-code) (Anthropic) and [Codex](https://openai.com/index/introducing-codex/) (OpenAI), orchestrated by its maintainers.

## License

MIT License — see `LICENSE`.
