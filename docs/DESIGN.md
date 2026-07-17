# Design notes

## Origin

chitra grew out of a tmux dispatch function that had two real, silently-triggering bugs (documented in `src/chitra/dispatch.py`'s module docstring). It was pulled out into its own hardened, tested library alongside a small set of daemons that turn "occasionally invoke this function from an interactive AI session" into "always-on, deterministic, systemd-supervised background service."

## Bounded reasoning boundary

chitra delivers messages to, and observes the state of, sessions that are themselves driven by an LLM. Its queue, dispatch, evidence, and storage paths remain deterministic. Goal enforcement is the one narrow process boundary: for a completion-claim turn-end, `watchd` invokes separate `claude -p` reviewers on a bounded worker pool to scrutinize the watched session's completed turn against a frozen goal. The pane poll never waits for those reviewers; it keeps an in-flight lane non-green and collects ready verdicts on later polls. Non-completion turns retain deterministic direction signals but do not launch an isolated reviewer. The reviewers do not draft or review Chitra's prospective response, share context, mutate state, or bypass the operator gates for spend, credentials, irreversible actions, and strategy redirects. Their structured signal is an input to `DecisionAttestation`; only the attested approved text can reach the pane.

General-purpose agent orchestration, task decomposition, and response generation remain out of scope. They belong in a higher-level system that uses chitra's delivery and audit surfaces. This bounded exception exists to make the completion and goal gates real rather than trusting the lane's own self-report.

## Done-condition ownership and close boundary

Done conditions belong to the operator and the material used to enroll a session. Chitra never enumerates, derives, proposes, authors, annotates, or rewrites `done_when`; post-hoc authorship would let the system fit the condition to whatever already exists. The first record write copies that condition into write-once `enrolled_done_when`/`enrolled_at` anchors, and every later write is checked at the single `_upsert_goal_locked` boundary. A stable `lane_id`, derived from the session name without host or instance suffix, prevents an open lane from being re-enrolled under a fresh volatile `session_ref`. Legacy records with no anchors are backfilled in memory from their current condition and stored timestamps, then persist the normalization on their next write. Enrollment-time interactive elicitation requires a separate operator-interaction channel and is outside this release.

`chitra.close_gate` therefore has a deliberately narrower role. It deterministically reads explicit items and counts from `enrolled_done_when`, computes enrolled-minus-current and history-derived descopes, compares the remaining inventory with caller-supplied delivered items or explicit `CompletionEvidence.todo_item` bindings, and blocks `chitra-goals close` before state deletion when the inventory is short. It also treats follow-on/out-of-scope/deferred/future-work language over a still-required item as a silent-descope tell unless a recorded operator redirect removed that condition or the caller supplies an explicit operator acknowledgement. Operator-facing core summaries render a reduced current condition together with its `dropping: ...` delta; adapters that render goal conditions must use `descope_delta(record)` to do the same.

The companion done-condition lint is surfacing-only. At `chitra-goals set`, missing or vague aggregate conditions add one fixed persistent `open_asks` flag for the board's `AWAITING RULING` section. Enrollment is not blocked, the supplied value is unchanged, and Chitra emits no replacement or suggested enumeration.

## Distribution and packaging

- **Distribution:** git-installable (`pip install git+https://...@<tag>`) for now, not yet on PyPI. chitra's current consumers install a pinned revision onto systemd hosts they provision themselves — PyPI's advantages (name-based discovery, version-range resolution for downstream packagers) don't apply yet. The build backend (hatchling, standards-based `pyproject.toml`) keeps a future PyPI release a small, mechanical step rather than a rewrite.
- **Layout:** `src/chitra/` (src-layout), not a flat top-level package. This ensures `import chitra` always resolves to the installed wheel, never to a loose working-directory copy — important for a package whose main job is running as an installed systemd service.
- **Versioning:** plain SemVer in the 0.x range. SemVer reserves 0.y.z for "anything may change" — appropriate before there's a real external consumer depending on a stable interface. 1.0.0 is reserved for the day the maintainers are willing to promise CLI/API stability.

## Single-writer rule (why `LaneLock` exists)

A tmux-hosted AI agent session is, from the outside, just a process with a terminal attached. It's tempting to assume you can deliver a message to it two different ways — inject text via tmux, or resume/replay into its own session transcript via whatever resume mechanism the agent's CLI provides — and pick whichever is convenient. In testing, doing so concurrently against a **live, actively-running** session caused a real, reproducible failure: the out-of-band delivery silently appended to the session's own transcript while racing its in-flight writes, corrupting its next turn with no visible error. `LaneLock` exists specifically to make "two writers, one session, at once" structurally impossible: `dispatchd` acquires an exclusive, file-based lock for a session id before attempting delivery and releases it after, and a second acquisition attempt against an already-held lock fails or blocks rather than silently proceeding.

The tmux-injection recipe (documented in the README) is the only channel this repo considers safe for delivering to a **live** session. Any out-of-band resume/replay fallback is outside chitra and must independently confirm the target session is genuinely detached or stopped while honoring the same single-writer lock.

## Future reconciler task-origin contract

No reconciliation or drift-detection path exists in chitra. If one is added, it must use a valid signed delivery-ledger entry to establish that chitra originated a task. It may add tasks or reorder chitra-originated tasks, but a task without matching delivery proof is presumed operator-authored and must never be removed, held, or corrected away. A growing task list is not drift.

## Extensibility without coupling

chitra exposes plain, documented file and queue formats: JSON orders and results (`chitra.dispatch`'s `DispatchOrder`/`DispatchResult` models), the `<ISO8601> <LANE_ID> <TEXT>` events-log line format documented in `chitra.triaged`'s module docstring, and the JSON triage log it emits. Any read-only consumer — a dashboard, a learning loop, another project — can be built against these formats without chitra needing to know it exists. For such a consumer, the module docstrings are the complete contract.
