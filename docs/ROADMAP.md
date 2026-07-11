# chitra next-version plan

chitra is deliberately small. Every item below is scoped to justify itself against that — if an idea would make the core daemons bigger, more dependent, or reasoning-capable, it belongs in a separate tool that *consumes* chitra's output, not in chitra.

This document was previously titled "v1.1" and framed as a wishlist. It's retitled here because most of what's below isn't a loose set of ideas — it's a consolidation of decisions and research already made across this program (PyPI publishing mechanics, a security audit, a routing-preferences design study) plus the pre-existing pull-only beads plan and the self-improvement observer plan, carried over largely unchanged. There is no committed version number attached to this plan; treat it as "what's next," not a numbered release promise.

## Status (2026-07-10)

Fully closed out. Landed and merged: the config/self-tuning surface ([PR #24](https://github.com/first-polyphony/chitra/pull/24)), the CodeQL-nightly fixes on both this repo ([PR #25](https://github.com/first-polyphony/chitra/pull/25)) and the monorepo ([PR #1975](https://github.com/first-polyphony/polyphony/pull/1975)), the Temporal-workflow-manager decision above ([PR #26](https://github.com/first-polyphony/chitra/pull/26)), the internal adapter — internal loop self-tuning loop, predecessor monitor reconciliation feed, internal pipeline promotion pipeline — on the monorepo ([PR #1965](https://github.com/first-polyphony/polyphony/pull/1965)), the mirror-sync porting the new config surface into the flat-layout monorepo mirror ([first-polyphony/polyphony#1984](https://github.com/first-polyphony/polyphony/pull/1984)), and two systemd-unit path fixes caught while bringing the service up for the first time ([#1995](https://github.com/first-polyphony/polyphony/pull/1995), [#1998](https://github.com/first-polyphony/polyphony/pull/1998)).

`polyphony-chitra-dispatchd` and `polyphony-chitra-triaged` are now installed, enabled, and running live on trailhead for the first time, reading the new self-tuning `policy.yaml`, verified via clean startup logs.

**Two items remain, both deliberately deferred, not forgotten:**
- **Run `/simplify` on this repo** now that it's fully complete — not yet scheduled.
- **Open the PyPI publish** — the gated checklist below is fully specified; the operator has not yet given the go-ahead to actually cut a release or flip the repo from private to public.

## In progress

### Completion gate

A deterministic audit of "done"/"complete" claims against todo residue and
deploy+live-verify evidence gaps — see `docs/evasion-taxonomy.md` and
`docs/review.md` for the design rationale. Wired into `dispatchd.py` as an
opt-in pre-delivery check; never auto-closes anything, only classifies and
surfaces. See [PR #14](https://github.com/first-polyphony/chitra/pull/14).

## v1.1

### Deferred: capabilities from the retired predecessor monitor session-monitor

When predecessor monitor's fleet-nudging role was decommissioned in favour of chitra, an audit found two capabilities that lived in
predecessor monitor's monitor but had no equivalent in chitra. One of the two — session↔goal
binding — has since been built (see below); crash recovery / checkpointing is
the one that remains deferred, and it still must clear chitra's own scope test
before it lands, because it adds persistent per-lane state to what is otherwise
a thin relay:

- **Crash recovery / checkpointing (still deferred)** — predecessor monitor's `checkpoint` /
  `checkpoint-restore` / `recovery-list` / `recovery-resume` /
  `manual-takeover` subcommands let a watcher snapshot a lane and resume it
  after a crash. This is the most distinctive thing predecessor monitor's monitor did that
  chitra doesn't. It adds persistent per-lane state, so by this document's own
  rule it likely belongs in a **consumer** of chitra's ledger/feed, not in the
  core daemons. Recorded here as a candidate, **not committed**.

**Session↔goal binding — built (no longer deferred).** The deterministic
goal store and roster landed in `chitra.goals` / the `chitra-goals` CLI via
[PR #38](https://github.com/first-polyphony/chitra/pull/38) (goal store +
roster), [#39](https://github.com/first-polyphony/chitra/pull/39) (persist open
asks, full-transcript read) and [#40](https://github.com/first-polyphony/chitra/pull/40)
(roster color legend / `Needs` column). A per-lane goal record now binds a
session to an explicit goal, completion condition, and status — the equivalent
of predecessor monitor's `enroll` / `list-goals` / `revise-goal` / `close-goal`. This stays
inside chitra's scope test because the store is deterministic with no LLM call
in its own code path (it records the monitor's stated goal; it does not
generate or reason about one).

Deliberately left out of scope: predecessor monitor's LLM-reasoner nudge generation. Reasoning
belongs in a consuming tool, per the scope statement at the top of this file.

---

## Landed since v0.2.0

These PRs implement decisions this program already made. As of this consolidation pass all of them have merged to `main` in `first-polyphony/chitra`; the monorepo mirror PRs are tracked separately and may lag. Listed here so the roadmap reflects real repo state rather than intent.

| PR | Repo | Status |
|---|---|---|
| [#4](https://github.com/first-polyphony/chitra/pull/4) chore: switch license to MIT | chitra | merged |
| [#5](https://github.com/first-polyphony/chitra/pull/5) fix: genericize `CHITRA_*` env vars, add `liveness_check` test coverage | chitra | merged |
| [#1917](https://github.com/first-polyphony/polyphony/pull/1917) fix(chitra): genericize `CHITRA_*` env vars, add `liveness_check` tests | monorepo mirror | see PR for current status |
| [#8](https://github.com/first-polyphony/chitra/pull/8) docs: clarify chitra's scope statement re: LLM calls | chitra | merged |
| [#1918](https://github.com/first-polyphony/polyphony/pull/1918) docs(chitra): clarify scope statement re: LLM calls | monorepo mirror | see PR for current status |
| [#13](https://github.com/first-polyphony/chitra/pull/13) feat: add opaque `routing_hint` pass-through field + `routing.yaml` config | chitra | see PR for current status |

`pyproject.toml`'s env var naming and the observer-pattern scope wording (see below) already match what this document assumes.

---

## PyPI migration — gated checklist

Full mechanics research: see `internal/publishing-research-readout.md` and `internal/license-history.md`, added by [PR #7](https://github.com/first-polyphony/chitra/pull/7) (merged; these files exist on `main`). Summary of the key findings and a concrete, ordered gate list follows — each gate should be satisfied in order before the next, and none skipped, before `pip install chitra-monitor` becomes a real, supportable path.

**Gate 1 — License question: decided.** `pyproject.toml` declares `license = "MIT"`, copyright held by Reticle Works (Trey Herr) — the final license decision, confirmed by the operator and merged via PR #4. This gate is closed.

**Gate 2 — Distribution name: decided.** The name `chitra` is already taken on PyPI by an unrelated, dormant (since 2021, ~4.5 years stale) image-utility project (`aniketmaurya/chitra`). A PEP 541 name-transfer claim is not viable — that dormant project doesn't clear PEP 541's abandonment bar (no notability, no proof a rename is unacceptable). The distribution name is **`chitra-monitor`**, confirmed available on PyPI and decided by the operator on 2026-07-09 (an earlier candidate, `polyphony-chitra`, is superseded); every internal module and every consumer's `import chitra` stays unchanged — standard, unremarkable practice (Pillow ships as `Pillow`/imports as `PIL`; `opencv-python`/imports as `cv2`). Only one line in `pyproject.toml` changes: `name = "chitra"` → `name = "chitra-monitor"`. Lock this name in before Gate 4, since a pending-publisher registration is tied to the exact name and a delay risks someone else claiming it.

**Gate 3 — Add missing packaging metadata.** Confirmed by reading `pyproject.toml` directly: `classifiers` and `keywords` are absent entirely, and `project.urls` is missing a `Homepage` key (PyPI's UI looks for that specific key to render its sidebar link). Add classifiers appropriate to a pre-1.0 project with no external users yet (`Development Status :: 3 - Alpha`, `Intended Audience :: Developers`, the correct `License ::` classifier once Gate 1 resolves, Python 3.12/3.13, `Typing :: Typed` — earned since `mypy strict = true` is already enforced in CI). This is independent of the naming decision and can happen in parallel.

**Gate 4 — Register trusted publishing (OIDC), not a stored API token.** On PyPI, register a **pending publisher** (the project doesn't need to exist yet) naming the exact GitHub owner/repo/workflow-file/environment. On the GitHub Actions side, the release workflow needs `permissions: id-token: write` and calls `pypa/gh-action-pypi-publish@release/v1` with no token/password input — that's the entire auth surface. Confirmed finding: **this does not require the GitHub repo to be public.** OIDC trust is between GitHub Actions and PyPI at publish time; repo visibility plays no part in that trust decision. The resulting PyPI package is public the moment it's published (no private-tier), but `first-polyphony/chitra` can stay private indefinitely — this gate is fully decoupled from any future public/private repo decision.

**Gate 5 — Register a separate TestPyPI trusted publisher and do a dry run.** TestPyPI's trusted-publisher registration is host-specific — a pypi.org registration does not cover test.pypi.org. Register a second pending publisher at `test.pypi.org/manage/account/publishing/` with the same repo/workflow details, then verify at least one dry-run install (`pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ chitra-monitor` — the second index is needed because dependencies like `structlog`/`pydantic` aren't mirrored to TestPyPI) before cutting the first real release.

**Gate 6 — Wire the release trigger.** Recommended: GitHub-Release/tag-triggered publish, not `workflow_dispatch` as the primary trigger — this matches chitra's existing Keep-a-Changelog discipline (every version already gets a real changelog entry) and forces version bump + changelog + tag to happen together as one ritual, rather than allowing a publish from an untagged or mismatched commit. Keep `workflow_dispatch` available only as a manual override for re-running a failed publish step against an already-tagged version.

**Gate 7 — Confirm PEP 740 attestations are on.** No extra work needed beyond Gate 4: as of `pypa/gh-action-pypi-publish@release/v1` v1.11.0+, Sigstore-backed provenance attestations are generated automatically whenever trusted publishing is used, reusing the same OIDC identity. Just confirm the pinned action version is v1.11.0 or later.

**Gate 8 — Run `twine check dist/*` in CI before any publish step.** Catches structural README problems (GitHub-specific `> [!NOTE]` alert syntax renders as a literal blockquote on PyPI; relative image/link paths that resolve on GitHub 404 on PyPI) as a build-time gate, not a post-publish surprise.

**Gate 9 — Cut the first real release.** Only after Gates 1–8 are satisfied. Remember: PyPI refuses to reuse a filename/version even after deletion — there is no "publish, notice a mistake, fix, re-publish 0.2.0" loop. Get it right in TestPyPI (Gate 5) rather than treating the first real publish as a rehearsal.

Note: PyPI has required account 2FA for any account with upload activity since 2024-01-01 — not a new gate to add, just confirm whoever administers the pending-publisher registration already has it enabled.

---

## Security — outstanding items

What's already fixed and confirmed live (via direct `gh api` queries against `first-polyphony/chitra`, 2026-07-09): secret scanning, secret-scanning push protection, Dependabot security updates, and Dependabot version updates are all **enabled and active** (0 open Dependabot alerts). No action needed on these.

**Outstanding, with owner-facing next steps:**

1. **CodeQL is configured but not functioning.** The workflow runs on push/PR/weekly cron and completes analysis successfully, but every run fails at the upload/reporting step with a GitHub Actions token-permissions error (`Resource not accessible by integration`). Result: `code-scanning/alerts` returns "no analysis found" — zero enforced static-analysis signal is reaching GitHub's UI, despite the workflow appearing green in the CI summary. **Next step:** add `actions: read` to the CodeQL workflow's `permissions:` block (or review the `GITHUB_TOKEN` permission model more broadly) and confirm a subsequent run actually populates the code-scanning tab. This should be fixed regardless of the PyPI or public/private timeline — a code-scanning badge that silently produces nothing is worse than no badge.
2. **No required PR reviewer in branch protection.** Consistent with this fleet's own auto-merge convention (CI-green + automated review → merge, no manual approval gate) — not an oversight. Worth naming explicitly because "no required review" reads differently to an external contributor on a repo accepting outside PRs than it does internally. **Next step:** no change needed while the repo stays private and fleet-only; revisit as an explicit operator decision if/when external contributors are invited, not before.
3. **`enforce_admins` is off**, meaning the repo owner can bypass branch protection. Low-risk on a private, single-maintainer repo. **Next step:** no action needed now; flag for a conscious decision if the repo ever goes public.

---

## Best practices — already landed in code (see PR links above for merge status)

These fixes were already designed and coded this program; they are not new roadmap work, just tracked here so the roadmap reflects what's already been decided:

- **`CHITRA_*` env var naming** — genericized per PR #5 (chitra) / #1917 (monorepo mirror).
- **`liveness_check` test coverage** — added in the same PRs (#5 / #1917).
- **LLM-scope-statement clarity fix** — PR #8 (chitra) / #1918 (monorepo mirror) tightens the wording that chitra has no internal LLM calls anywhere in its own code path while still managing LLM-driven sessions in the panes it dispatches to. The self-improvement and beads sections below use this same framing deliberately — see each section.

---

## Routing preferences (built)

This is no longer a proposal: `DispatchOrder` carries an optional `routing_hint: str | None = None` field plus an optional `task_type` string that drives config lookup (see the README's "Routing config" subsection for the full mechanics). The config now supports two shapes; summary of what's actually in the repo:

- **`defaults` — opaque hint (unchanged).** A flat `task_type -> routing_hint` map. Chitra fills in the opaque `routing_hint` string but never interprets or acts on it — carried through to `DispatchResult` and the signed ledger entry exactly the way `tag` passes through, for audit/observability only. `resolve_routing_hint` is a pure dictionary lookup (provenance `"config"`).
- **`routes` — active model/harness routing (built for #29).** A structured `task_type -> {model, harness, zdr?}` map. `chitra.routing_config.resolve_route` resolves the concrete model+harness (+zdr) at dispatch; `dispatchd` records the **resolved** selection structurally (`resolved_model` / `resolved_harness` / `resolved_zdr`) plus a `model@harness[+zdr]` `routing_hint`, with provenance `"route"`. This is still config-driven substitution, not a smart router — chitra does not decide what a task type IS or classify content; the operator states, per task_type, which concrete model+harness that type routes to. A `routes` entry wins over a `defaults` entry for the same `task_type`.
- **Explicit caller hint always wins.** `dispatchd` only consults the config when the order's `routing_hint` is unset AND a `task_type` is present; an explicit `routing_hint` from the caller is never overridden.
- **Missing config is a no-op, not an error.** If neither the env var nor the flag is set, `dispatchd` runs with no routing config at all. If a path IS configured but the file is missing or fails to parse, that's a real configuration error and `load_routing_config` raises rather than silently ignoring it.

For real-world naming precedent on what a deployment's `task_type` values might look like — not a prescription chitra enforces — see [`docs/workflow-pattern-catalog.md`](workflow-pattern-catalog.md), a catalog of named orchestration loop patterns.

**Ledger provenance gap — closed.** The ledger now records `task_type` and a `routing_hint_source` provenance flag (`explicit` / `config` / `route` / `unset`) alongside `routing_hint`, and — for resolved `routes` selections — the concrete `resolved_model` / `resolved_harness` / `resolved_zdr`. All are part of the HMAC-signed payload (ledger `sig_v` bumped to 3; older v1/v2 entries still verify). An auditor reading `ledger.jsonl` can now distinguish a caller-chosen hint from a config default from an actively-resolved route, and see the exact model+harness chitra selected.

**Explicitly out of scope for chitra — a named scope boundary, not a quiet omission.** A more ambitious idea was also discussed this program: chitra actively *suggesting* how a receiving session should organize its own sub-agent hierarchy (e.g., "this looks like a multi-file refactor, consider a plan→build→validate breakdown"). This does **not** belong inside chitra. Generating a task-aware suggestion requires evaluating the content/class of a task and producing a judgment about appropriate structure — that's reasoning, not plumbing, and doing it would require either an LLM call (violating chitra's zero-LLM-calls invariant) or a hardcoded rule table far richer than the flat `task_type -> routing_hint` map above. This conclusion should not be softened: if this capability is built, it belongs in a **separate, higher-level advisor system** that reads chitra's order/ledger data (`routing_hint`, delivery history per `session_ref`, dispatch outcomes) as its data source, does its own reasoning there, and feeds the *result* back to chitra as an ordinary dispatched `nudge` — keeping chitra's own code path LLM-free.

---

## Temporal-based workflow manager (planned, separate consuming tool)

A design study (2026-07-10) was commissioned to answer: could chitra grow into something that matches Claude Code's own workflow/orchestration functionality — multi-step dispatch, fan-out/fan-in, completion gating, retry — while staying agnostic to which agent does the actual work, and able to execute the nine named patterns in [`docs/workflow-pattern-catalog.md`](workflow-pattern-catalog.md) as literal, runnable shapes rather than just naming labels?

The study's own recommendation was a lighter-weight, file-based sequencer built in chitra's existing idiom (durable JSON order queue, lane locks, the completion gate as a step-exit condition), with Temporal declined as the backend — its verdict was that Temporal's workflow-as-code model conflicts with "a shape is data an operator selects," and that a workflow-orchestration server cluster is a poor fit for chitra's small-and-dependency-light identity.

**The operator has decided to proceed with Temporal specifically as the backend**, overriding that recommendation. Scope, consistent with every other item on this page and with the study's own boundary analysis:

- **This is not built inside chitra.** Per chitra's stated design philosophy (`DESIGN.md`: orchestration logic belongs in "a separate, higher-level system that uses chitra as its delivery/dedup layer") and per the study's own scope-boundary finding, the workflow manager is a **separate tool** that consumes chitra's existing artifacts (the order queue, the signed ledger, `evaluate_completion_claim`'s verdicts) rather than new code inside `dispatchd`/`triaged`.
- **The hybrid concession identified by the study still applies**: chitra itself gains two small, opaque, pass-through fields — `workflow_id` and `step_id` — on `DispatchOrder`/`DispatchResult`/`LedgerEntry`, carried and signed exactly like the existing `tag` and `routing_hint` fields, never interpreted or acted on by chitra. This is what lets the signed ledger double as a workflow-execution audit trail without chitra learning anything about workflows.
- **Shape coverage, per the study's translation analysis** — of the nine cataloged patterns, three translate cleanly into a deterministic step-DAG (Heartbeat, Orchestrator-Workers, Ratchet), two require mandating structured/enum verdicts rather than free-text votes (Quorum, Compost), two are deterministic but a different machinery class entirely — counters/promotion state and cron-verified predicates, not a DAG (Trust Ledger, Standing Goals) — and two do not fit a deterministic engine at all and should stay agent-internal or fixed-round degraded forms (Sparring, Executor-Advisor/Oracle). The Temporal workflow manager should implement the first five categories as real, versioned shape templates and explicitly document the last two as out of scope rather than force-fitting them.
- **Every judgment call inside a shape is itself a dispatch to an external agent.** The engine (Temporal or otherwise) walks the DAG, evaluates structured verdicts, and advances state — it never summarizes, scores, or judges content itself. This preserves chitra's zero-LLM-calls invariant at the layer that touches chitra; Temporal's own workflow code is judgment-free plumbing over agent-produced verdicts.

Not yet scheduled or estimated; this section records the decision and scope, not a committed timeline. Implementation should start with the `workflow_id`/`step_id` field addition in chitra (small, reviewable independently) before the separate Temporal tool is built against it.

---

## beads integration (pull side only)

[beads](https://github.com/steveyegge/beads) (a git-native work tracker) remains a candidate backing store for the read side of a lane-status/decisions ledger: something that wants to know "what's the current state and history of decisions for session X" could query beads instead of re-deriving it. This is explicitly **pull-only** — chitra's own orders and dispatch stay push-based through `dispatchd`'s JSON queue; beads has no push mechanism, and nothing about chitra's delivery path changes. Scope stays narrow: read-side integration for status/ledger queries, nothing else. No code exists for this yet; it remains a pilot candidate, not a committed item, and should stay that way until a concrete consumer needs it — adding it speculatively would violate the "stays lightweight" test every item on this page has to pass.

## Self-improvement: a separate, read-only observer — not a framework

chitra's daemons already emit plain, documented artifacts (dispatch results, triage events, the delivery ledger). The plan for "learning from operational traces" is a **separate, read-only observer process** that computes simple rolling statistics over those artifacts — dispatch success rate, queue latency, dedup hit/miss rate — and surfaces threshold-based flags (e.g., "the dedup window looks too short given N near-miss collisions this week"). That's it: statistical monitoring and flagging, not an automated mutator of chitra's own config, and not a new dependency inside chitra itself. To state this with the same clarity as PR #8's scope-statement fix elsewhere in this repo: **chitra's own code path contains no LLM calls anywhere**, including in this observer — the observer computes deterministic rolling statistics over chitra's artifacts, full stop; it does not itself reason about them. (Separately, and outside this roadmap's scope: a sidecar LLM-driven observer capability used elsewhere in the fleet to watch *sessions* chitra dispatches into — confirmed live and unaffected by any change in this program — is a consumer of chitra's output, not a part of chitra, and needs no action here.)

Explicitly ruled out, with reasons: **DSPy** optimizes LLM prompt/pipeline behavior against a scoring metric — chitra has no LLM call anywhere in its core, so there's no prompt to optimize and no natural role for a DSPy-style optimizer here. **RL-from-logs / closed-loop auto-tuning** is a real technique but a heavier lift (defined action space, reward shaping, an evaluation harness) than a read-only observer, and a closed loop that silently rewrites chitra's runtime config is exactly the kind of complexity this project is trying to avoid. Both stay out of scope.

### Deviance-pattern awareness (external research, abstracted internal taxonomy)

A related project maintains an internal taxonomy of AI-agent deviance patterns (false blockers, narrated-but-not-taken action, silent scope reduction, fabricated completion claims) built from operational transcripts. That internal taxonomy does **not** ship in this repo. What's relevant here is external, publicly available research on the same phenomena, which chitra's roadmap can reference without exposing anything proprietary:

- [METR: "Recent Frontier Models Are Reward Hacking"](https://metr.org/blog/2025-06-05-recent-reward-hacking/) — documented cases of agents faking task success (tampering with harnesses, disabling checks) rather than completing the task.
- [Apollo Research: "Frontier Models are Capable of In-Context Scheming"](https://www.apolloresearch.ai/research/frontier-models-are-capable-of-incontext-scheming/) ([paper](https://arxiv.org/pdf/2412.04984)) — models producing misleading self-reports about their own behavior, including sandbagging.
- ["Where LLM Agents Fail and How They Can Learn From Failures"](https://arxiv.org/pdf/2509.25370) and ["Exploring Autonomous Agents: A Closer Look at Why They Fail When Completing Tasks"](https://arxiv.org/pdf/2508.13143) — general agent failure-mode taxonomies (premature ungrounded action, error propagation).
- ["Establishing Best Practices for Building Rigorous Agentic Benchmarks"](https://arxiv.org/html/2507.02825v2) — quantifies premature-termination/early-stop rates across agentic benchmarks.
- ["Are Your Agents Upward Deceivers?"](https://arxiv.org/abs/2512.04864) — defines "upward deception" (concealing failure, taking unreported actions) with a dedicated benchmark.

Honest caveat: no external paper currently treats "false blockers claiming human intervention is needed" as its own named, benchmarked category — it's an unlabeled subset scattered across the sources above. Any future work here should synthesize across them rather than expect one canonical citation.

chitra's own shipped taxonomy (`docs/evasion-taxonomy.md`) operationalizes a subset of these patterns — todo-residue-under-a-done-claim and evidence-gap-under-a-done-claim — as a concrete, deterministic ruleset (the completion gate, `src/chitra/completion_gate.py`), rather than leaving deviance-pattern awareness as research-only. That shipped taxonomy is a genericized 24-entry ruleset with only two codes actually operationalized today; it is not the internal taxonomy referenced above, and it does not expose anything proprietary about that internal system.

### Goal discipline (tracking, not generation)

chitra is not, and will not become, a PRD generator — for turning an idea into a structured spec, use an existing tool built for that. What's in scope here is narrower: once a goal/decision exists for a registered session, **track it, enforce it against drift, and keep the tracking non-brittle across code changes**, plus maintain a simple per-session register of open questions/tasks/decisions that a human or another tool can query. This is bookkeeping discipline, not a planning feature — it fits the observer pattern above rather than adding new surface to the daemons themselves.

## Capability manifest, and MCP as the deferred tool-server upgrade

**Near-term (building): a capability manifest.** chitra's capabilities are being formalized as a declarative manifest — a packaged `capabilities.yaml` (name, purpose, when-to-use, authority, commands, `default_enabled`) plus a runtime toggle overlay in the state dir — read by whichever LLM drives the monitor (Codex or Claude) so it can discover what chitra can do, when to reach for each capability, and whether it's enabled. Deliberately **not** Claude Code skills: those are Claude-only and would weld chitra to one backend; a manifest is plain data both backends read. Enable/disable is per-capability, scoped, and reversible (time-boxable via `--until`), so a one-time intervention — e.g. relieving a stuck merge queue — never ratchets into standing authority over unrelated operations.

**Deferred upgrade: expose the same capabilities over MCP (Model Context Protocol).** Studied 2026-07-11; deferred with explicit triggers. Not needed now — the manifest-plus-shell interface is sufficient for a single monitor session, and adopting MCP earlier buys reuse/validation guarantees chitra is not yet positioned to use, at real (if modest at this scale) context and operational cost.

*What MCP would add over a text manifest + shell invocation:* structured runtime tool discovery (`tools/list`, with live change-notifications) instead of trusting a re-read YAML; typed input/output schemas validated before execution (JSON Schema), so malformed arguments are rejected structurally rather than depending on the model writing a correctly-quoted shell string; a standardized transport (stdio / Streamable HTTP); and "one server, many clients" reuse. Also two things a manifest cannot express: elicitation (a server pausing mid-call to request structured operator input) and per-tool human-approval gating enforced by the host. Sources: MCP spec ([modelcontextprotocol.io](https://modelcontextprotocol.io/introduction); [2026-07-28 release candidate](https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/)).

*Backend parity (the load-bearing fact, verified 2026-07-11):* both Claude Code and Codex CLI are genuine MCP clients supporting local **stdio** servers — the transport chitra would use to wrap its console-scripts. Confirmed from primary docs ([code.claude.com/docs/en/mcp](https://code.claude.com/docs/en/mcp); [OpenAI Codex MCP](https://developers.openai.com/codex/mcp) → [learn.chatgpt.com/docs/extend/mcp](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)). OpenAI's Responses API and Agents SDK also carry first-class MCP support. Not verified: whether Codex CLI's stdio lifecycle (reconnect/health-check) matches Claude Code's documented behavior — flagged; it does not threaten the core claim that both are real MCP stdio clients.

*Why it's an upgrade, not a rewrite:* the manifest fields map cleanly onto an MCP tool definition — `name`→`name`, `purpose`/`when-to-use`→`description`, `command.params`→`inputSchema`, `authority`→annotations (`readOnlyHint`/`destructiveHint`), `enabled`→tool-registration filter. The `argv` template becomes the server-side subprocess recipe; chitra's Python doesn't change. An MCP server can be generated from the manifest rather than hand-written (established tooling: [pycli-mcp](https://github.com/ofek/pycli-mcp), cli2mcp, FastMCP). The manifest is intentionally structured (typed `params`, not a free-text invoke string) precisely so this generation needs no redesign.

*Triggers for the upgrade (any one):* (a) a second concurrent consumer of chitra's capabilities appears beyond the single tmux monitor — MCP's cross-client reuse only pays off with more than one client; (b) capability count or description size grows enough that flat-text loading costs meaningful monitor context each turn (MCP supports deferred/lazy schema loading; a flat YAML does not); (c) a measured rate of LLM-authored shell-invocation errors crosses a threshold where schema validation demonstrably reduces it; (d) a capability must run somewhere other than the local shell (another host/process), which MCP's HTTP/SSE transports allow and shell invocation fundamentally cannot.

*Costs that justify deferring:* token/context overhead of schema-at-connect (direction corroborated by [CircleCI](https://circleci.com/blog/mcp-vs-cli/) and by Anthropic shipping deferred tool-loading to fight MCP context bloat; specific 4–32× multipliers are vendor-reported and unverified); stdio server lifecycle (a longer-lived subprocess — Claude Code explicitly does **not** auto-reconnect stdio servers); OAuth/transport machinery irrelevant to a local single-operator library; and spec instability (still a release candidate as of 2026-07-28). For eight local, single-consumer capabilities, the manifest's lack of server/transport/auth surface is the correct trade.

*Net recommendation:* "manifest now, MCP when X," where X is any trigger above. Sound specifically because the migration is additive — same console-scripts, a generated wrapper server, and both target backends already support the stdio transport chitra would need.
