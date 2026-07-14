# chitra

Deterministic, systemd-supervised relay and dedup daemons for delivering text into `tmux`-hosted AI-agent sessions and watching their state. Built for fleets of Claude Code sessions; the tmux-level mechanics are agent-agnostic.

**Scope:** chitra delivers messages to, and observes the state of, LLM-driven sessions in tmux. Delivery, queueing, evidence checks, and state transitions remain deterministic. The single bounded reasoning boundary is goal enforcement: when a watched lane ends a turn with a completion claim, `chitra.goal_enforcement` launches independent `claude -p` reviewer processes to compare that lane's direction, questions, and completion posture with its frozen goal. Reviewers never draft Chitra's response, and their identifiers, counts, and verdicts are retained only in Chitra's own logs—never pasted into the monitored lane.

## Quickstart

```bash
pip install git+https://github.com/first-polyphony/chitra.git@<tag>
```

Requires Python 3.12+ and `tmux` on the host. See [Install](#install) for local development setup and [Configuration](#configuration) for the environment variables `chitra.dispatch` reads.

## Why "chitra"

The name is a short form of *Chitragupta*, a figure from Hindu tradition described as the divine registrar and keeper of a complete, accurate ledger of deeds — one who records, verifies what is recorded, and reports to the decision-maker, but does not act on that decision-maker's behalf. That remains this package's contract: it observes, verifies against frozen goals and cited artifacts, gates release, and relays without doing an agent session's work for it. The name is used respectfully as a functional reference, not as religious imagery.

BrowserStack's `chitragupta-node` and `chitragupta-rails` are open-source SDKs that use the same name for structured JSON (JavaScript Object Notation) logging — attaching metadata to log lines rather than relaying or signing them. Different tool, same naming logic: the name attaches to something that records and structures what happened, not something that decides what should happen. No other project surfaced in a search that uses the name specifically for a delivery/relay or ledger-signing role.

## What's in this repo

chitra delivers to and observes LLM-driven sessions from the outside. Its relay and storage paths are deterministic; the isolated watched-session reviewers above are the deliberately narrow exception.

### Session-management primitives

- **`chitra.usage`** — strict reader for Claude statusline sidecar and Codex account usage snapshots, plus pure rate-limit threshold evaluation. Usage is attributed to the account; `evaluate` emits each session's account-level verdict so callers can pause stale siblings with an over-limit account. A session with no known account identity is never merged with another unknown-identity session — each is its own isolated group. It reports facts (`ok`, `approaching`, `pause`, or stale/unknown) and never pauses a lane or chooses an action.
- **`chitra.goals`** — a deterministic per-lane goal store. Strategic fields (`goal`, `done_when`, `intent`, `scope`, and `source`) can only be revised with a reasoned `redirect`; routine tactical updates remain available through `now`, while `check` applies a stricter specification threshold and `guidance` resolves the configured canonical-decisions document. `set` surfaces a persistent operator flag when the supplied `done_when` is missing or uses vague aggregate language, but never rewrites it. `close` blocks until caller-supplied delivered items balance the currently stated conditions, including a hard failure when a required item is relabeled follow-on, out of scope, deferred, or future work without an operator-recorded descope or explicit acknowledgement. Its `hold`, `resume`, and `due` subcommands record the monitor's hold bookkeeping while preserving the stated goal; the caller decides whether and how to act on a due record. Every read-modify-write helper serializes its full transaction with a `flock`-protected critical section, so concurrent writers cannot silently lose each other's updates.
- **`chitra.goal_enforcement`** — freezes the watched session's current goal, launches each adversarial reviewer in a separate process, requires unanimous acceptance, rejects stale or tampered bindings, and automatically restarts a redirected review with one reviewer while recording the restart in goal history. Spend, credentials, irreversible actions, and strategy redirects remain operator-gated even after unanimous acceptance.
- **`chitra.reasoning`** — goal-first decision triangulation whose public dispatch record is the immutable `DecisionAttestation`. The attestation binds exact approved text, frozen-goal and corpus lineage, the watched-session review signal, and the operator-gate outcome. `dispatchd` logs it to Chitra's private `attestations.jsonl`; only the approved text is pasted.
- **`chitra.close_gate`** — pure close-time inventory parsing and diffing over the operator-stated `done_when`, explicit delivered-item/evidence bindings, close notes, recorded goal revisions, and explicit operator acknowledgements. It never infers delivery from close prose.
- **`chitra.completion_gate`** — citation-bearing completion review. Deploy and live-verification claims must retain concrete SHA, path, or probe citations; completed todo items need per-item proof; and delivery briefs must answer what was built, what it does, and whether it actually works.
- **`chitra.watchd`** — tmux pane-change emitter and forced turn-end boundary. Every detected finished turn runs the deterministic completion audit, while only a completion claim launches isolated watched-session reviewers. A turn without a completion claim becomes `turn-finished-unverified`, while a disputed claim becomes `completion-disputed`, so neither can render idle-green.
- **`chitra.account_registry`** — a freshness-bounded fact table of which account each tracked lane was last observed under. Used by `chitra.rate_limit_guard` to surface a missing usage snapshot or a mid-session account change as an operator escalation, instead of silently ignoring it or silently merging it with an unrelated lane.
- **`chitra.rate_limit_state`** — the durable transaction outbox behind `chitra.rate_limit_guard`'s pause/resume state machine (see below). A crash between sweeps, or between any two phases, is never a data-loss event: the next sweep re-reads the transaction and continues from wherever it stopped.
- **`chitra.rate_limit_guard`** (`chitra-rate-limit-guard`, `default_enabled: true`) — advances the shared durable pause/resume transaction for provider limits and host load through `pause_requested → checkpoint_sent → stop_sent → awaiting_quiescence → held → resume_requested → resume_sent`. Claude lanes retain the checkpoint plus deterministic `/goal clear` sequence. Codex lanes receive a fixed checkpoint-and-stop order and prove the stop through Watchd's backend-neutral pane-quiescence signal; no unverifiable Codex-internal stop API is assumed. Provider-limit resumes are selected by `session_ref` ascending, at most one new resume per sweep, and each later sweep rechecks fresh usage. Load pressure is sampled from `MemAvailable` and Linux memory/CPU PSI, activated or cleared only after two consecutive sweeps, and narrows the running-lane cap from 8 to 6/4/2 at L1/L2/L3. Load holds use the distinct `load-shed:<host>:<level>` prefix and resume last-shed-first, one lane per sweep, after the clear hysteresis. Every waiting phase remains bounded and graceful; L3 shortens deadlines but never kills a lane. When a lane reaches `held`, an append-preserving recovery ledger records why it stopped, its transcript pointer, its goal-derived resume note, and its reset time; see [`docs/pause-recovery.md`](docs/pause-recovery.md).
- **`chitra.ownership`** (`chitra-ownership`) — a read-only Watchtower-facing query over the Sweepd tracked-lane state. Given a host and one or more resolved `session_ref` values, it reports whether any belongs to a currently tracked `working` lane. It never dispatches, pauses, or kills. This repository has no clean descendant-PID-to-tmux mapping, so Watchtower resolves a PID to `session_ref` before invoking the query.
- **`chitra.sweepd`** — publishes the compact fleet-state delta consumed by downstream monitors. Its snapshot and digest include the current `load_level` keyed by host and the durable shed-lane list; each lane also carries its host's level and whether its hold is a load shed.

- **`chitra.dispatch`** — a tmux dispatch library. It checks for tmux copy-mode and cancels it, uses `paste-buffer -p` for a proper bracketed-paste wrapper, then confirms delivery by grepping the target session's own transcript rather than trusting a pane screenshot. Includes `LaneLock`, a file-based single-writer lock: only one writer delivers to a given session id at a time.
- **`chitra.dispatchd`** — a daemon that drains a JSON order queue (`queue/orders/*.json`), atomically claims each order (renamed into `queue/in_flight/*.json`) before any delivery attempt, delivers it via `chitra.dispatch` under a `LaneLock`, writes a result JSON, and moves the processed order aside. Once a result file exists for an order, it is never redispatched; a crash between the paste actually happening and the result file being written is reconciled on the next pass via a send-nonce marker plus the same transcript-grep evidence `dispatch_to_tmux` itself uses, rather than blindly redispatching — see "Crash-safety" below. A session held by `chitra.rate_limit_guard` for a rate-limit or load-shed reason gets its ordinary orders durably deferred (`queue/deferred/*.json`, no result written) rather than discarded — `requeue_deferred_for_session` returns them to `orders/` FIFO once the hold clears.
- **`chitra.triaged`** — a daemon that tails an events log and emits a triage event only when a session's state signature changes, not on every repeated poll. Its receiving compatibility artifacts are `queue.tsv`, deduplicated critical `flags.log`, and `stats.json`.
- **`chitra.draft_scanner`** — a periodic scan of `host:session:pane` targets for an unsubmitted draft sitting in the tmux input box. Flags only; never submits or discards anything.
- **`chitra.board_updater`** — a deterministic, validated writer for a small JSON "board" document: it backs up the existing file, validates the new one against caller-supplied constraints, writes, and rolls back if validation fails.
- **`chitra.board`** — the deterministic, operator-facing board renderer. It strictly validates the full facts schema, renders the bundled interactive HTML to `index.html` atomically, and records result freshness in `health.json`.
- **`chitra.ledger`** — an append-only delivery ledger signed with HMAC (hash-based message authentication code). Every successfully delivered message is signed and logged, so a reader with the signing key can verify an exact recorded delivery. Absence is only conventional evidence; see "Message tag and delivery authentication" below.
- **`chitra.convlog`** — a deterministic operator-brief validator, BLUF renderer, and append-only conversation log. It validates, renders, and logs briefs the caller composed; it never composes or judges their content.

### Watchd reviewer configuration

The normal completion-claim review round uses two isolated reviewers and pins
the subprocess model to `claude-haiku-4-5`. Operators can tune the round down
to one reviewer or point it at a credential wrapper without changing code:

- `CHITRA_WATCHD_REVIEWER_MODEL` / `--reviewer-model` — reviewer model
  (default `claude-haiku-4-5`).
- `CHITRA_WATCHD_REVIEWER_COUNT` / `--reviewer-count` — normal-round reviewer
  count, which must be at least 1 (default 2). A goal redirect still restarts
  with exactly one reviewer.
- `CHITRA_WATCHD_REVIEWER_COMMAND` / `--reviewer-command` — subprocess command
  (default `claude`).

The subprocess inherits the watchd service environment, including `HOME` and
`CLAUDE_CONFIG_DIR`; provisioning that credential path belongs to deployment.

## Operator brief conversation log

`chitra-convo` records one four-state exchange in a plain JSONL conversation log: the full raw upward session message, the caller-composed and chitra-rendered operator brief, the operator's explicit ruling, and the lane directive sent down (optionally linked to its dispatch ledger order id). The caller, normally the monitor harness LLM, owns interpretation and composition; chitra only validates the declared schema, renders the fixed bottom-line-up-front layout, and records exact text.

A thread is pending exactly when its latest operator brief contains a decision and no later operator ruling exists. Brief revisions are allowed, and the latest revision is authoritative; silence never retires a pending ask. `chitra-convo pending` renders all such threads as one numbered message, oldest first.

## Operator-stated done conditions and close inventory

Chitra consumes done conditions supplied by the operator or enrollment material. It does **not** enumerate, derive, propose, author, annotate, or rewrite a lane's `done_when`. Interactive enrollment-time elicitation is not part of this release.

At `chitra-goals set`, a deterministic lint checks only for missing conditions and uncounted aggregate language such as `representative consumers`, `some clients`, or bare deliverable plurals. A finding adds the fixed message “This session's done conditions are missing or vague — flag for the operator.” to the lane's persistent `open_asks`, which the roster renders under `AWAITING RULING`. The set still succeeds and the supplied `done_when` is stored unchanged. No corrected wording or candidate enumeration is produced.

At `chitra-goals close`, the caller must state each delivered item with a repeated `--delivered-item`. Chitra deterministically reads explicit conjunctions, lists, and counts from the existing `done_when` and blocks deletion if the delivered inventory is short. Repeated `--close-note` values are checked for a required item being reclassified as `follow-on`, `out of scope`, `out-of-scope`, `deferred`, or `future work`. A real operator descope uses `chitra-goals redirect --done-when ... --reason ...`, which preserves the prior condition in `goal_history` and increments `goal_version`; an exact operator exception can instead be supplied with `--operator-acknowledged-item`. These are records of operator direction, not Chitra-authored conditions.

```bash
chitra-goals close \
  --session-ref host:lane:0.0 \
  --delivered-item "X client" \
  --delivered-item "Y client" \
  --close-note "Both clients passed live validation."
```

The Python API also accepts `CompletionEvidence` records, but counts one as delivered only when its caller supplied an explicit `todo_item` binding. Citation prose is never interpreted as a delivered item.

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

Before a paste attempt, `dispatchd` writes a send-nonce beside the claimed order in `in_flight/`. If the worker crashes after the paste but before the result is written, the next pass sees that nonce and checks the target transcript before doing any second paste. A confirmed prior delivery produces a synthesized `SENT` result; only an unconfirmed attempt is dispatched again. Existing result files remain the final idempotency guard.

## Message tag and delivery authentication

Every dispatched message carries a `tag` (default `"[C]"`) marking it as a chitra relay delivery. An operator typing directly into a pane needs no tag and no authentication; the pane is that operator's own channel. `DispatchOrder`/`DispatchResult` also carry an optional `routing_hint` (default `None`) — an opaque string recording a routing/model-preference decision the calling system already made; chitra never reads, validates, or acts on its contents, only passes it through unchanged into the result and the signed ledger entry for audit purposes.

Without the ledger, a receiving session cannot distinguish "chitra genuinely delivered this" from an unauthenticated claim. On every **successful** delivery — never on blocked or failed attempts — `dispatchd` appends a signed record to an append-only JSON Lines (JSONL) ledger. The current `sig_v3` HMAC-SHA256 payload covers `(timestamp, session_ref, tag, message_hash, routing_hint, task_type, routing_hint_source, resolved_model, resolved_harness, resolved_zdr)`; the verifier retains the versioned v1/v2 field sets for older entries. The signing key lives in the state directory and is generated on first use. This adds no extra step to a normal send.

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

Thirteen command-line interface (CLI) entrypoints are installed. `dispatchd` and `triaged` are the always-on daemons. Periodic or ad-hoc tools are `draft-scanner`, `chitra-board`, `chitra-goals`, `chitra-artifacts`, `chitra-usage`, `chitra-rate-limit-guard`, `chitra-ownership`, `chitra-sweepd`, `chitra-convo`, `chitra-capabilities`, and `chitra-queue`. The board renderer also runs as `python -m chitra.board`. Example systemd units — with placeholder paths and a placeholder service user you must fill in — live under `packaging/systemd/`.

The guard remains a one-shot CLI. To schedule its two-minute sweep, copy the example service and timer, edit every placeholder for the target host, then enable the timer:

```bash
sudo cp packaging/systemd/chitra-rate-limit-guard.service.example /etc/systemd/system/chitra-rate-limit-guard.service
sudo cp packaging/systemd/chitra-rate-limit-guard.timer.example /etc/systemd/system/chitra-rate-limit-guard.timer
sudoedit /etc/systemd/system/chitra-rate-limit-guard.service
sudo systemctl daemon-reload
sudo systemctl enable --now chitra-rate-limit-guard.timer
```

Watchtower's read-only ownership check uses its already-resolved session references:

```bash
chitra-ownership --host tophand \
  --session-ref tophand:lane-a:0.0 \
  --session-ref tophand:lane-b:0.0
```

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

`dispatchd` also accepts `--policy-config-path`, `--invalid-orders-dir`, `--capture-lines`, `--post-paste-wait-seconds`, `--transcript-recency-seconds`, `--lane-lock-timeout-seconds`, and `--goals-root` (the state root consulted for the guard freeze/defer check; default: `CHITRA_STATE_DIR`); see `dispatchd --help`. `chitra-rate-limit-guard` accepts `--usage-dir`, `--host` (required), `--codex`, `--goals-root`, `--queue-dir`, and `--policy-config`; see `chitra-rate-limit-guard --help`. `chitra-ownership` accepts `--host`, repeatable `--session-ref`, and `--state-dir`. The generic replay evaluator and fixture workflow are documented in [`docs/self-tuning.md`](docs/self-tuning.md).

## A note on the observer pattern

Internally, chitra is paired with a read-only observer that consumes its event and state output for learning and reflection; it never writes back into chitra's queues, locks, or state. That coupling is not shipped here. Instead, chitra exposes plain, documented file and queue formats: JSON orders and results (`chitra.dispatch`'s `DispatchOrder`/`DispatchResult` models), the `<ISO8601> <LANE_ID> <TEXT>` events-log line format documented in `chitra.triaged`'s module docstring, and the JSON triage log it emits. Any read-only consumer — an internal tool, a dashboard, another open-source project — can be built against these formats without chitra needing to know it exists. For such a consumer, the module docstrings are the complete contract.

## Roadmap

See `docs/ROADMAP.md` for the v1.1 plan.

## Authors

Built with [Claude](https://claude.com/claude-code) (Anthropic) and [Codex](https://openai.com/index/introducing-codex/) (OpenAI), orchestrated by its maintainers.

## License

MIT License — see `LICENSE`.
