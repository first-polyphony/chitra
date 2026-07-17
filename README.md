# chitra

chitra is a set of deterministic, systemd-supervised daemons that deliver text into `tmux`-hosted AI-agent sessions and watch their state. It lets long-running coding-agent sessions run unattended instead of being babysat pane by pane.

It was built to manage large parallel sessions with LLMs, allowing the user to do more while chitra manages clearly defined goals and nudges agents forward.

## Scope

chitra delivers messages to LLM-driven sessions in tmux and observes their state. Everything on the delivery, queueing, evidence-check, and state-tracking path is deterministic — no model decides what to send or when.

The one deliberate exception is **goal enforcement**. When a watched session ends a turn claiming it is done, `chitra.goal_enforcement` launches independent `claude -p` reviewer processes to check that claim against the session's frozen goal. The reviewers never draft chitra's messages, and their verdicts stay in chitra's own logs — they are never pasted back into the watched session.

## Quickstart

```bash
pip install chitra-monitor  # or: pip install git+https://github.com/first-polyphony/chitra.git@<tag>
```

Replace `<tag>` with a released version from the [tags page](https://github.com/first-polyphony/chitra/tags), or drop `@<tag>` to install from the default branch.

Requires Python 3.12+ and `tmux` on the host. See [Install](#install) for local development setup, [Configuration](#configuration) for environment variables, and [Delivering into a tmux pane](#delivering-into-a-tmux-pane) for what chitra actually does to a pane.

## Why "chitra"

The name is a short form of *Chitragupta*, a figure from Hindu tradition described as the divine registrar and keeper of a complete, accurate ledger of deeds — one who records, verifies what is recorded, and reports to the decision-maker, but does not act on that decision-maker's behalf. That remains this package's contract: it observes, verifies against frozen goals and cited artifacts, gates release, and relays without doing an agent session's work for it. The name is used respectfully as a functional reference, not as religious imagery.

BrowserStack's `chitragupta-node` and `chitragupta-rails` are open-source SDKs that use the same name for structured JSON (JavaScript Object Notation) logging — attaching metadata to log lines rather than relaying or signing them. Different tool, same naming logic: the name attaches to something that records and structures what happened, not something that decides what should happen.

## What's in this repo

chitra installs eleven command-line entrypoints backed by a set of small, single-purpose modules. `dispatchd` and `triaged` are the always-on daemons; the rest are periodic or ad-hoc tools.

**Delivery**
- `chitra.dispatch` / `chitra.dispatchd` — drain a JSON order queue and deliver each message into a tmux session via bracketed paste, confirming delivery by grepping the session's own transcript. One writer per session (`LaneLock`); idempotent and crash-safe (see [Delivery guarantees](#delivery-guarantees)).
- `chitra.ledger` — an append-only, HMAC-signed log of every delivered message.

**Monitoring**
- `chitra.watchd` — emits tmux pane-change and turn-end events and runs a completion audit on each finished turn.
- `chitra.triaged` / `chitra.sweepd` — deduplicated state-change events and a compact fleet-state feed for downstream monitors.
- `chitra.draft_scanner` — flags unsubmitted drafts left sitting in a tmux input box.

**Goals and completion**
- `chitra.goals` — a per-lane goal store with a write-once enrolled done-condition, guarded by `flock`.
- `chitra.goal_enforcement` / `chitra.completion_gate` / `chitra.close_gate` — review a session's completion claim against its frozen goal and cited evidence; spend, credentials, and irreversible actions stay operator-gated.

**Rate limiting**
- `chitra.usage` / `chitra.rate_limit_guard` / `chitra.account_registry` — read account usage and pause/resume lanes on provider limits or host load pressure, over a durable, crash-safe transaction. See [`docs/pause-recovery.md`](docs/pause-recovery.md).

**Rendering**
- `chitra.board` / `chitra.convlog` — a terminal roster of goals and open asks, and an append-only operator-brief conversation log.

## Delivering into a tmux pane

Delivery into a live tmux session follows one path:

1. `tmux display-message -p -t <target> '#{pane_in_mode}'` — if `1`, the pane is in copy-mode (which silently swallows input); run `tmux send-keys -X cancel` and wait briefly.
2. `printf '%s' "$text" | tmux load-buffer -b <name> -`
3. `tmux paste-buffer -p -b <name> -t <target>` — the `-p` flag is mandatory; without it, newlines act as Enter keypresses and the message can self-submit early.
4. `tmux send-keys -t <target> Enter`
5. Confirm delivery by grepping the target session's transcript for the delivered text. "Looks sent" is not evidence.

For a remote target, each command is the same, ssh-wrapped to run on the actual target host. Checking the local tmux server's state, or grepping local transcripts, when the target is remote reports on the wrong host.

## Delivery guarantees

- **Single writer.** `dispatchd` holds a `LaneLock` per session id across each delivery, so two writers can't race to paste into the same session and corrupt its next turn.
- **Idempotent.** Once a result file exists for an order, it is never redispatched, even across a restart. A crash between paste and result is reconciled with a send-nonce marker plus the same transcript-grep check, not a blind second paste.
- **Authenticated.** Every successful delivery appends an HMAC-SHA256-signed record to an append-only JSONL ledger; a reader with the signing key can prove a given message was delivered. This is a trusted-host model — anyone who can write to the ledger file can rewrite it — so treat "not in the ledger" as a strong signal, not tamper-proof evidence. See `chitra.ledger.verify_delivery`.

## Running the daemons

`dispatchd` and `triaged` run continuously. `chitra-rate-limit-guard` is a one-shot CLI meant to run on a timer. Example systemd units, with placeholder paths and service user, live under [`packaging/systemd/`](packaging/systemd/):

```bash
sudo cp packaging/systemd/chitra-rate-limit-guard.service.example /etc/systemd/system/chitra-rate-limit-guard.service
sudo cp packaging/systemd/chitra-rate-limit-guard.timer.example /etc/systemd/system/chitra-rate-limit-guard.timer
sudoedit /etc/systemd/system/chitra-rate-limit-guard.service   # fill in placeholders
sudo systemctl daemon-reload
sudo systemctl enable --now chitra-rate-limit-guard.timer
```

## Configuration

Each entrypoint is configured with CLI flags (`--help` on any command lists them) and a small set of environment variables. The most common:

| Env var | Default | Notes |
|---|---|---|
| `CHITRA_STATE_DIR` | `/var/lib/chitra` | Base directory for the queue, ledger, and ledger key |
| `REMOTE_DISPATCH_HOSTS` | *(empty)* | Comma-separated allowlist of hosts dispatch may target over ssh |
| `CHITRA_CLAUDE_PROJECTS` | `~/.claude/projects` | Root searched for transcript-grep delivery verification |
| `CHITRA_ROUTING_CONFIG` | *(unset)* | Optional `task_type` → routing-hint config; see [`docs/routing.yaml.example`](docs/routing.yaml.example) |
| `CHITRA_POLICY_CONFIG` | *(unset)* | Optional completion-gate and dispatch policy; see [`docs/policy.yaml.example`](docs/policy.yaml.example) |

The full set — ssh options, triage log paths, transcript globs — is documented per-command via `--help`.

**Routing.** A caller can tag a `DispatchOrder` with an opaque `task_type`. If a routing config is set, `dispatchd` maps that to a `routing_hint` (a model/harness preference the caller's system uses); an explicit `routing_hint` always wins, and chitra carries the hint through to the ledger but never acts on it.

## Install

Requires Python 3.12+ and `tmux` (chitra shells out to the `tmux` binary; there is no Python tmux dependency).

```bash
pip install chitra-monitor  # or: pip install git+https://github.com/first-polyphony/chitra.git@<tag>
```

Not yet on PyPI — see [`docs/DESIGN.md`](docs/DESIGN.md) for the packaging rationale.

For local development:

```bash
git clone https://github.com/first-polyphony/chitra.git
cd chitra
pip install -e '.[test]'
pytest
```

## Getting help

Questions and bug reports: [open an issue](https://github.com/first-polyphony/chitra/issues). See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a nontrivial PR; security reports go through [SECURITY.md](SECURITY.md).

## Authors

**Trey Herr** (Reticle Works) — design and direction. Built with [Claude](https://claude.com/claude-code) (Anthropic) and [Codex](https://openai.com/index/introducing-codex/) (OpenAI) as development tools under human direction.

## License

MIT © 2026 Reticle Works. See [LICENSE](LICENSE) for the full text.
