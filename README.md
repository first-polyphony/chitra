# chitra

Deterministic, systemd-supervised relay and dedup daemons for delivering text into `tmux`-hosted AI-agent sessions and watching their state. Built for fleets of Claude Code sessions; the tmux-level mechanics are agent-agnostic.

**Scope:** chitra's whole purpose is to deliver messages to, and observe the state of, sessions that are themselves driven by an LLM (large language model, e.g. Claude Code instances running in tmux) — that IS its job. What chitra itself never does is call an LLM API to decide what to say or how to act: every decision about message content, timing, and target is made by the caller (a human operator or an orchestrating session) before it reaches chitra. chitra's own code path is deterministic plumbing — it delivers text, verifies delivery against evidence, and dedups repeated state, but makes no drafting or judgment calls of its own. Anything that would add reasoning/decision logic or an LLM call *inside chitra's own code* is out of scope here.

## Quickstart

```bash
pip install git+https://github.com/first-polyphony/chitra.git@<tag>
```

Requires Python 3.12+ and `tmux` on the host. See [Install](#install) for local development setup and [Configuration](#configuration) for the environment variables `chitra.dispatch` reads.

## Why "chitra"

The name is a short form of *Chitragupta*, a figure from Hindu tradition described as the divine registrar and keeper of a complete, accurate ledger of deeds — one who records, verifies what is recorded, and reports to the decision-maker, but does not act on that decision-maker's behalf. That is this package's exact contract: it observes, verifies against artifacts, and relays. It never decides, and it never does an agent session's work for it. The name is used respectfully as a functional reference, not as religious imagery.

BrowserStack's `chitragupta-node` and `chitragupta-rails` are open-source SDKs that use the same name for structured JSON (JavaScript Object Notation) logging — attaching metadata to log lines rather than relaying or signing them. Different tool, same naming logic: the name attaches to something that records and structures what happened, not something that decides what should happen. No other project surfaced in a search that uses the name specifically for a delivery/relay or ledger-signing role.

## What's in this repo

chitra delivers to and observes LLM-driven sessions from the outside; its own code path makes no LLM calls — no drafting, no judgment calls, deterministic relay/plumbing only.

### Session-management primitives

- **`chitra.usage`** — strict reader for Claude statusline sidecar and Codex account usage snapshots, plus pure rate-limit threshold evaluation. It reports facts (`ok`, `approaching`, `pause`, or stale/unknown) and never pauses a lane or chooses an action.
- **`chitra.goals`** — a deterministic per-lane goal store. Its `hold`, `resume`, and `due` subcommands record the monitor's hold bookkeeping while preserving the stated goal; the caller decides whether and how to act on a due record.

- **`chitra.dispatch`** — a tmux dispatch library. It checks for tmux copy-mode and cancels it, uses `paste-buffer -p` for a proper bracketed-paste wrapper, then confirms delivery by grepping the target session's own transcript rather than trusting a pane screenshot. Includes `LaneLock`, a file-based single-writer lock: only one writer delivers to a given session id at a time.
- **`chitra.dispatchd`** — a daemon that drains a JSON order queue (`queue/orders/*.json`), delivers each order via `chitra.dispatch` under a `LaneLock`, writes a result JSON, and moves the processed order aside. Once a result file exists for an order, it is never redispatched — but a crash between the paste actually happening and the result file being written is a real gap that can cause a redelivery on restart; see "Crash-safety" below.
- **`chitra.triaged`** — a daemon that tails an events log and emits a triage event only when a session's state signature changes, not on every repeated poll. Its receiving compatibility artifacts are `queue.tsv`, deduplicated critical `flags.log`, and `stats.json`.
- **`chitra.draft_scanner`** — a periodic scan of `host:session:pane` targets for an unsubmitted draft sitting in the tmux input box. Flags only; never submits or discards anything.
- **`chitra.board_updater`** — a deterministic, validated writer for a small JSON "board" document: it backs up the existing file, validates the new one against caller-supplied constraints, writes, and rolls back if validation fails.
- **`chitra.board`** — the deterministic, operator-facing board renderer. It strictly validates the full facts schema, renders the bundled interactive HTML to `index.html` atomically, and records result freshness in `health.json`.
- **`chitra.ledger`** — an append-only delivery ledger signed with HMAC (hash-based message authentication code). Every successfully delivered message is signed and logged, so any reader can later verify that chitra delivered an exact message at a given time — or that chitra never sent a given message.

## Tmux injection recipe

Delivery into a **live** tmux session follows one path:

1. `tmux display-message -p -t <target> '#{pane_in_mode}'` — if `1` (the pane is in copy-mode, which silently swallows input), run `tmux send-keys -X cancel` and wait briefly.
2. `printf '%s' "$text" | tmux load-buffer -b <name> -`
3. `tmux paste-buffer -p -b <name> -t <target>` — the `-p` flag is mandatory; without it, newlines in multi-line text act as real Enter keypresses and the message can self-submit early.
4. `tmux send-keys -t <target> Enter`
5. Confirm delivery by grepping the target session's own transcript file for the delivered text. A pane screenshot or "looks sent" heuristic is not evidence that delivery happened.

Every step above runs against the **actual target host** — a plain local `tmux`/filesystem call for a local target, or the identical command ssh-wrapped for a remote one (chitra's real deployment shape: it typically runs from one host, e.g. trailhead, and dispatches over ssh into another, e.g. tophand). This matters for steps 1 and 5 in particular: checking the *local* tmux server's copy-mode state, or grepping the *local* filesystem's transcripts, when the target is remote reports on the wrong host entirely and can never confirm a genuine remote delivery.

## Single-writer rule

`dispatchd` acquires a `LaneLock` per session id before any delivery attempt and releases it after: one writer per session id. Acquiring a lock for an already-locked session id fails or blocks rather than silently proceeding. This prevents two writers racing to deliver to the same session at once — an out-of-band delivery racing a live session's own process can silently corrupt its next turn.

## Crash-safety

`dispatchd` guards against redelivery using a result file: before dispatching, it checks whether a result file already exists for an order id, and if so treats the order as already processed. This means **once a result file exists for an order, it is never redispatched**, even across a daemon restart.

The one real gap: the result file is written *after* the paste already happened (paste -> optional ledger sign/log -> result write -> move to `processed/`). If the daemon crashes in that window — after the paste actually landed in the target pane but before the result file is written — the order file is still sitting in `orders/` with no result file, so the next run re-dispatches it and the message is delivered a second time. This window is small (no I/O happens between the paste and the result write beyond the ledger append), but it is real and not closed by anything in this package.

## Message tag and delivery authentication

Every dispatched message carries a `tag` (default `"[C]"`) marking it as a chitra relay delivery. An operator typing directly into a pane needs no tag and no authentication; the pane is that operator's own channel. `DispatchOrder`/`DispatchResult` also carry an optional `routing_hint` (default `None`) — an opaque string recording a routing/model-preference decision the calling system already made; chitra never reads, validates, or acts on its contents, only passes it through unchanged into the result and the signed ledger entry for audit purposes.

Without the ledger, a receiving session cannot distinguish "chitra genuinely delivered this" from an unauthenticated claim. On every **successful** delivery — never on blocked or failed attempts — `dispatchd` signs an HMAC-SHA256 over `(timestamp, session_ref, tag, message_hash, routing_hint)` using a key stored in the state directory (generated on first use) and appends the signed record to an append-only JSON Lines (JSONL) ledger. This adds no extra step to a normal send.

This is a trusted-host threat model: the ledger assumes whoever can write to the state directory is trusted (systemd-supervised `dispatchd`, plus the host's own root/admin). It is not designed to resist a malicious actor with filesystem write access to `ledger.jsonl`.

Within that model, the ledger proves one thing cryptographically, and one thing only by convention:
- **Positive (cryptographic)**: "chitra delivered this exact message to this session at this time" — recompute the HMAC over a given ledger entry and compare; if you have that entry, its authenticity is provable.
- **Absence (convention, not cryptographic)**: `dispatchd` only ever appends to `ledger.jsonl`, so under normal operation a message's absence suggests no such delivery happened. But append-only-ness here is enforced by convention and file permissions, not by a hash chain or monotonic counter linking entries — there is nothing in the file format that would let a reader detect a wholesale truncation or edit. Anyone with write access to the ledger file can rewrite or shorten it undetected. Treat "not in the ledger" as a strong signal under the trusted-host assumption, not as tamper-proof evidence.

See `chitra.ledger.verify_delivery` for the check as a function call, or read `ledger.jsonl` directly (a plain, documented JSONL format) if the verifying reader doesn't have chitra installed.

## Routing config (`task_type` -> default `routing_hint`)

`DispatchOrder` also carries an optional `task_type` — a separate, caller-supplied classification string (e.g. `"code-review"`, `"design-judgment"`). Chitra does not decide what a task type IS or evaluate any content to classify one; the caller states it. `task_type`, the resolved routing selection, and a provenance flag (`routing_hint_source`) are carried through onto `DispatchResult` and the signed ledger entry for audit.

If a caller sets `task_type` but leaves `routing_hint` unset, `dispatchd` consults an operator-populated YAML config keyed by `task_type`. This is still config-driven substitution — like a `.gitattributes` or `nginx.conf` mapping file, not a smart router — and it is skipped entirely whenever the caller already supplied an explicit `routing_hint` (**explicit `routing_hint` always wins**). The config supports two shapes:

- **`defaults` (opaque hint)** — a flat `task_type -> routing_hint` map. Chitra fills in the opaque `routing_hint` string but never acts on it (`routing_hint_source: "config"`). Unchanged; existing configs keep working.
- **`routes` (active model/harness selection)** — a structured `task_type -> {model, harness, zdr?}` map. Chitra **resolves** the model+harness at dispatch, records the resolved selection structurally (`resolved_model` / `resolved_harness` / `resolved_zdr`) plus a `model@harness[+zdr]` `routing_hint`, and stamps `routing_hint_source: "route"`. When both a `routes` and a `defaults` entry exist for the same `task_type`, the structured route wins.

Point `dispatchd` at a config file via the `CHITRA_ROUTING_CONFIG` env var (or its `--routing-config-path` flag). If unset, `dispatchd` runs with no routing config — a normal no-op, not an error. If the env var/flag IS set but the file is missing or fails to parse, that's a real configuration error and `dispatchd` raises rather than silently ignoring it. An example template ships at `docs/routing.yaml.example`:

```yaml
# chitra routing preferences, keyed by task_type.
# defaults: opaque routing_hint chitra carries but never acts on.
defaults:
  heartbeat: sonnet
  quorum: haiku
# routes: structured model+harness (+zdr) chitra RESOLVES and records.
routes:
  design-judgment:
    model: opus-4.8
    harness: claude-code
    zdr: true
  code-fix:
    model: gpt-5.6-sol
    harness: codex-cli
```

The keys/values above are illustrative only. Chitra ships no default content or opinions about what task types or routing targets (model names, harnesses) mean in any given deployment — this is a file each operator populates for their own fleet. For real-world naming precedent (not a prescription), see [`docs/workflow-pattern-catalog.md`](docs/workflow-pattern-catalog.md), a catalog of named orchestration loop patterns some deployments' `task_type` values may correspond to.

## Install

Requires Python 3.12+ and `tmux` on the host (chitra shells out to the `tmux` binary; there is no Python tmux dependency to install).

```bash
pip install git+https://github.com/first-polyphony/chitra.git@<tag>
```

Not yet on PyPI — see `docs/DESIGN.md` for the packaging rationale.

For local development:

```bash
git clone https://github.com/first-polyphony/chitra.git
cd chitra
pip install -e '.[test]'
pytest
```

## Running the daemons

Two command-line interface (CLI) entrypoints are installed: `dispatchd` and `triaged`, plus `draft-scanner` as an ad-hoc tool. The board renderer runs as `python -m chitra.board` (or `chitra-board` when installed). Example systemd units — with placeholder paths and a placeholder service user you must fill in — live under `packaging/systemd/`. Copy them, edit the placeholders, and install as `chitra-dispatchd.service` / `chitra-triaged.service`.

## Configuration

All configuration is via CLI flags (see `--help` on each entrypoint) or a small number of environment variables read by `chitra.dispatch`:

| Env var | Default | Read by | Notes |
|---|---|---|---|
| `REMOTE_DISPATCH_HOSTS` | *(empty — local delivery only)* | `chitra.dispatch` | Comma-separated allowlist of remote hostnames dispatch is allowed to target over ssh |
| `CHITRA_LOCAL_HOST` | *(unset)* | `chitra.dispatch` | Override for this host's own name, for local-vs-remote detection in tests/unusual setups |
| `CHITRA_LANE_LOCK_DIR` | a `chitra-locks` dir under the system temp dir | `chitra.dispatch` | Directory for `LaneLock` lock files |
| `CHITRA_CLAUDE_PROJECTS` | `~/.claude/projects` | `chitra.dispatch` | Root directory searched locally for transcript-grep verification of a local target |
| `CHITRA_REMOTE_CLAUDE_PROJECTS` | `~/.claude/projects` | `chitra.dispatch` | Root directory searched **on the remote host** (over ssh) for transcript-grep verification of a remote target |
| `CHITRA_TRANSCRIPT_GLOB` | `*/*.jsonl` | `chitra.dispatch` | Relative transcript pattern beneath each configured transcript root |
| `CHITRA_SSH_CONFIG` | *(unset)* | `chitra.dispatch` | Optional `ssh -F <path>` config file for remote dispatch |
| `CHITRA_SSH_IDENTITY` | *(unset)* | `chitra.dispatch` | Optional `ssh -i <path>` identity file for remote dispatch |
| `CHITRA_SSH_KNOWN_HOSTS` | *(unset)* | `chitra.dispatch` | Optional `UserKnownHostsFile` for remote dispatch |
| `CHITRA_SSH_STRICT_HOST_KEY_CHECKING` | `accept-new` | `chitra.dispatch` | Value passed to ssh's `StrictHostKeyChecking` option |
| `CHITRA_SSH_CONNECT_TIMEOUT_SECONDS` | `4` | `chitra.dispatch` | Positive integer passed to ssh's `ConnectTimeout` option |
| `CHITRA_STATE_DIR` | `/var/lib/chitra` | `chitra.dispatchd`, `chitra.ledger` | Base directory for the default queue, ledger, and ledger key |
| `CHITRA_POLICY_CONFIG` | *(unset — shipped defaults)* | `chitra.dispatchd` | Optional one-file completion-gate and dispatch policy; see [`docs/policy.yaml.example`](docs/policy.yaml.example) |
| `CHITRA_TRIAGE_EVENTS_LOG` | `/var/lib/chitra/events.log` | `chitra.triaged` | Events log to consume when no CLI flag is supplied |
| `CHITRA_TRIAGE_STATE_FILE` | `/var/lib/chitra/triaged-state.json` | `chitra.triaged` | Persistent transition-dedup state |
| `CHITRA_TRIAGE_LOG` | `/var/lib/chitra/triaged.log` | `chitra.triaged` | JSONL transition log |
| `CHITRA_TRIAGE_QUEUE_FILE` / `CHITRA_TRIAGE_FLAGS_FILE` / `CHITRA_TRIAGE_STATS_FILE` | alongside the state file | `chitra.triaged` | Receiving compatibility artifacts: queue, interrupt-only flags, and counters |
| `CHITRA_TRIAGE_ALERT_STATE_FILE` | alongside the state file | `chitra.triaged` | Persistent 15-minute `(lane, rule, statement)` critical-flag dedup state |
| `CHITRA_BOARD_DIR` | `$CHITRA_STATE_DIR/board` | `chitra.board` | Directory containing `facts.json` and generated `index.html` / `health.json` |
| `CHITRA_BOARD_TEMPLATE` | bundled template | `chitra.board` | Optional replacement HTML template |
| `CHITRA_BOARD_LOCAL_HOST` | local hostname | `chitra.board` | Facts host treated as local for tmux tail capture |
| `CHITRA_BOARD_REMOTE_HOSTS` / `CHITRA_BOARD_SSH_USER` | *(none)* / `ubuntu` | `chitra.board` | Opt-in remote tail capture allowlist and SSH user |
| `CHITRA_BOARD_SNAPSHOT_OWNER` / `CHITRA_BOARD_VALID_HOSTS` | *(none)* | `chitra.board` | Optional deployment-specific owner and tmux-host schema constraints |
| `CHITRA_BOARD_CAPACITY_FILE` | *(none)* | `chitra.board` | Optional external capacity snapshot rendered in the lower board strip |

`dispatchd` also accepts `--policy-config-path`, `--invalid-orders-dir`, `--capture-lines`, `--post-paste-wait-seconds`, `--transcript-recency-seconds`, and `--lane-lock-timeout-seconds`; see `dispatchd --help`. The generic replay evaluator and fixture workflow are documented in [`docs/self-tuning.md`](docs/self-tuning.md).

## A note on the observer pattern

Internally, chitra is paired with a read-only observer that consumes its event and state output for learning and reflection; it never writes back into chitra's queues, locks, or state. That coupling is not shipped here. Instead, chitra exposes plain, documented file and queue formats: JSON orders and results (`chitra.dispatch`'s `DispatchOrder`/`DispatchResult` models), the `<ISO8601> <LANE_ID> <TEXT>` events-log line format documented in `chitra.triaged`'s module docstring, and the JSON triage log it emits. Any read-only consumer — an internal tool, a dashboard, another open-source project — can be built against these formats without chitra needing to know it exists. For such a consumer, the module docstrings are the complete contract.

## Roadmap

See `docs/ROADMAP.md` for the v1.1 plan.

## Authors

Built with [Claude](https://claude.com/claude-code) (Anthropic) and [Codex](https://openai.com/index/introducing-codex/) (OpenAI), orchestrated by its maintainers.

## License

MIT License — see `LICENSE`.
