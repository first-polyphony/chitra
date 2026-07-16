# SOL adversarial review

Reviewed read-only on 2026-07-12. I inspected the four PRs through `gh`, the PR-head source and tests through the GitHub API, both supplied recovery reports, the local Chitra checkout, and the deployed-source/runtime split on hub-host over read-only SSH. I did **not** accept PR descriptions as evidence. GitHub's check-runs API returned HTTP 403 for the provisioned PAT, so the exact full-suite pass counts are author-reported, not independently corroborated by CI in this review.

## Ranked findings

### 1. BLOCKER — PR #55 does not “hold the queue”; it permanently consumes frozen work

**Evidence:** PR #55, `src/chitra/dispatchd.py:184-216`; PR #55, `tests/test_dispatchd.py` added test `test_rate_limit_held_session_is_blocked_without_touching_the_pane` (the test explicitly asserts the rejected order is in `processed/`).

**Failure scenario:** A lane is rate-limit-held. An ordinary order for work that should wait until reset arrives. `dispatchd` emits a terminal `BLOCKED` result and moves the order to `processed/`. When the rate-limit hold clears, nothing requeues or retries that order. The advertised “freeze dispatch” / “hold the queue” has discarded the queued work from the delivery path.

This is not a wording nit. A freeze preserves pending work; this implementation rejects it permanently. The new test locks in the wrong behavior instead of exposing it.

**Recommended action:** Do not merge. Park frozen orders in a durable deferred state/subqueue and atomically return them to FIFO after resume, or leave them pending with an explicit not-before condition. Add an end-to-end test: ordinary order arrives while held → no pane I/O → resume → the same order is delivered exactly once.

### 2. BLOCKER — PR #55 does not gracefully pause a running session, and its state transitions can strand or silently unfreeze lanes

**Evidence:** PR #55, `src/chitra/rate_limit_guard.py:199-235, 251-304`; deployed adapter runbook, `tools/support/chitra_adapter/harness/CLAUDE.md:331-350`; PR #55, `tests/test_rate_limit_guard.py:149-176, 263-285, 298-379`.

**Failure scenarios:**

1. On a pause verdict, `apply_pause()` writes `status=held` **before** enqueueing the checkpoint nudge. If the queue directory is unwritable, disk is full, or the process dies between those operations, the hold persists but no checkpoint order exists. The next sweep treats the same hold/window as an idempotent no-op. The lane can remain held indefinitely.
2. If the checkpoint order exists but delivery is `BLOCKED`/`FAILED`, the guard never reads that result. `PauseOutcome` records only an order ID. The hold remains unconditional.
3. The canned checkpoint text asks the agent to checkpoint, but the guard never clears `/goal`, verifies the transcript stopped, or stops the already-running turn. The new `dispatchd` check only blocks **future queue orders**. A live agent can continue consuming quota while Chitra's goal record falsely says “held.” This is materially weaker than the existing runbook, which requires checkpoint + `/goal clear` + transcript confirmation before recording the hold (`CLAUDE.md:337-342`).
4. On resume, `apply_resume()` clears the hold **before** enqueueing the re-arm order. If enqueue fails or the process dies, the lane is marked `working`, the freeze is gone, and no resume nudge exists. A later sweep cannot retry because the goal is no longer due/held.
5. A due lane with a missing/stale snapshot is silently left held (`plan_resumes()` simply continues at lines 274-278). If its statusline stops emitting after the pause—the exact quiet state likely during a held session—automatic resume may never happen. There is no timeout escalation.

The 17 new guard tests cover happy state mutations in a temporary directory. They do not inject an enqueue failure, a checkpoint delivery failure, a process crash at either transaction boundary, a missing sidecar after reset, or an actual pane that keeps running.

**Recommended action:** Replace the two uncoordinated writes with a durable state machine/outbox (`pause_requested` → checkpoint delivered/stopped → `held` → `resume_requested` → resume delivered → `working`). Consume dispatch results, retry idempotently, and escalate after a bounded deadline. A “graceful pause” must prove the active turn stopped; merely labeling the goal held is not enough.

### 3. BLOCKER — PR #2219's five-minute “health no-op” is a mutating, costly full-fleet probe that can manufacture outages

**Evidence:** PR #2219, `infra/ccr/reconcile-codex-oauth.sh:62-95`; `infra/ccr/verify-models.sh:2-7, 40-63, 68-134`; incident report `INCIDENT-solcc-api-errors.md:19-20, 42-46`.

**Failure scenario:** Every five minutes the reconciler runs `verify-models.sh`. That script is not a targeted Codex OAuth check:

- It toggles gateway request logging with `getConfig` + `saveConfig` before and after the run (`verify-models.sh:40-59`). The incident report proves the import path's `saveConfig` reload creates a several-second 401/502 warming window. The logging path passes `applyProfile: false`, but there is no test proving that form is reload-free; even if it is, the alleged healthy no-op still mutates shared gateway config twice.
- It sends a real prompt to **every offered model** (`verify-models.sh:68-127`), including OpenRouter routes explicitly labeled as API-billed. With 14 models, a five-minute timer is roughly 4,032 real model calls/day before retries. That is needless spend/quota consumption from a script intended to protect quota and auth stability.
- Any non-429 failure in any provider/model makes the whole probe fail. An unrelated OpenRouter outage, stale Anthropic catalog entry, request-log race, or transient 5xx then triggers the broad import + refresh + config reload intended for Codex OAuth staleness.

Therefore “healthy state is a fast no-op” is false. Healthy state still performs config writes and a full real-model sweep. Unhealthy-but-not-Codex state invokes the wrong repair and can flap the shared gateway every five minutes.

**Recommended action:** Do not merge. Detect the actual condition: compare a digest/generation of `~/.codex/auth.json` with the credential generation last imported into CCR, or run one targeted `preflight-codex-oauth.sh` call against one Codex model. Do not mutate observability settings. Do not probe unrelated providers. Add rate/backoff limits and metrics.

### 4. HIGH — PR #2219 has stale-snapshot/config races and does not clean existing duplicate providers

**Evidence:** PR #2219, `infra/ccr/import-local-oauth-providers.sh:99-120, 138-217, 256-258`; reconciler has no `flock` or other cross-process exclusion; incident report `INCIDENT-solcc-api-errors.md:19-20`.

**Failure scenarios:**

- `import-local-oauth-providers.sh` reads the full config, makes additional RPC calls, then saves that earlier config snapshot. A concurrent `sol-cc` launch, manual refresh, or admin config edit can land between the read and `saveConfig`; the stale save can overwrite the newer valid state. The timer adds another recurrent writer but introduces no shared lock.
- Codex can refresh `auth.json` again during import/refresh. There is no before/after token-generation comparison, so the script can install a credential already superseded by the time the gateway reloads.
- `existingProviderForCandidate()` returns the first matching Anthropic provider and continues. It never filters/removes other matching providers. If live config already has two `anthropic_messages` providers at `https://api.anthropic.com`, rerunning this code leaves both. The PR description's claim that provider count returned to three must have depended on a separate live cleanup; the proposed code does not perform it.
- Matching only by protocol + public base URL is overbroad. If multiple intentional Anthropic credentials/accounts ever exist, the first one is silently treated as the reusable canonical provider.

**Recommended action:** Use one host-wide lock shared by `sol-cc`, reconciler, import, and refresh. Re-read config immediately before a compare-and-swap save or make a narrow provider credential update. Detect duplicate shapes, fail with an explicit migration report, and require a deliberate cleanup policy rather than silently picking the first.

### 5. HIGH — Current `dispatchd` is not crash-safe and can double-deliver despite saying it cannot

**Evidence:** `/opt/polyphony/chitra-main/src/chitra/dispatchd.py:153-159, 207-280`; `/opt/polyphony/chitra-main/tests/test_dispatchd.py:126-142, 169-203`; `/opt/polyphony/chitra-main/src/chitra/dispatch.py:1157-1175`.

**Failure scenarios:**

1. `dispatchd` checks for an existing result **before** acquiring the per-lane lock. Two workers can both see no result. Worker A sends, releases the lock, and only later writes the result. Worker B then acquires the lock and sends the same order because there is no result recheck under the lock.
2. A single worker can paste/submit the nudge, then die after `dispatch_to_tmux()` returns but before `_write_result_atomic()`. The order remains in `orders/` with no result and is sent again after restart.

The “crash” test covers only the safe side of the boundary: a crash **after the result already exists**. The ledger-failure test catches an exception and continues to result creation; it does not simulate process death after real pane I/O. The code comment at `dispatchd.py:249-256` accurately describes the duplicate-send risk, then only mitigates one caught ledger exception, not arbitrary death or concurrent workers.

**Recommended action:** Claim the order atomically before any delivery (rename into an in-flight directory or transactional queue record), acquire the lane lock, and recheck idempotency under that lock. Persist a send intent/nonce that transcript verification can reconcile after crash. Add multiprocessing and kill-point tests around the paste/result boundary.

### 6. HIGH — PR #55's account-aware pause is incomplete, can group unrelated unknown accounts, and explicitly leaves Codex unsolved

**Evidence:** `/opt/polyphony/chitra-main/src/chitra/usage.py:61-97, 248-295, 421-461`; deployed `tools/support/chitra_adapter/bin/chitra-usage-snapshot:27-59`; PR #55, `src/chitra/rate_limit_guard.py:94-106`; PR #55 description assumption 4; supplied recovery findings `RECOVER-transcript-findings.md:80-98`.

**Failure scenarios:**

- Fan-out covers only snapshots that still exist in the scanned directory. A sibling with no file is not paused. A stale sibling whose account changed mid-session can be paused under its old account. This does not satisfy the operator requirement to track session→account continuously and pause every corresponding session.
- `UsageSnapshot.account` defaults to `""`; the sidecar silently falls back to `""` when both status input and `.claude.json` account lookup fail. `evaluate_grouped()` groups by that raw string. Two unrelated unknown-account sessions are therefore treated as one account; one fresh hot unknown can pause every empty-account sibling.
- The Codex probe intentionally emits `tmux_session=""`, and `_session_ref_for()` skips it. The supplied requirement says a Codex account pause fans out to every Codex lane on the host. PR #55 admits this is not implemented.

The existing `usage.py` tests prove propagation for two explicitly named equal accounts. PR #55 adds no sibling fan-out end-to-end test, no missing-file test, no account-change test, no multiple-empty-account test, and no Codex-lane test.

**Recommended action:** Fail closed on unknown account identity as “cannot automate” rather than treating all unknowns as one account. Maintain an authoritative, freshness-bounded lane→account registry, including lane disappearance/account change. Implement or explicitly exclude Codex host-wide fan-out before claiming the requested behavior complete.

### 7. HIGH — PR #55's freeze is racy, globally bypassable, and its documented CLI option is not wired

**Evidence:** PR #55, `src/chitra/dispatchd.py:176-216, 264-288, 467-500`; PR #55, `src/chitra/dispatch.py:218-225`; PR #55, `tests/test_dispatchd.py` four new tests.

**Failure scenarios:**

- The hold is read before the lane lock is acquired. A rate-limit hold can land after the check and before pane paste; the ordinary order is delivered into a newly frozen lane. This is a classic TOCTOU.
- Any JSON queue writer can set `bypass_rate_limit_freeze=true`; `dispatchd` does not restrict bypass to checkpoint/resume task types or authenticate provenance. The field is an unguarded policy escape hatch.
- `build_arg_parser()` exposes `--goals-root`, but `main()` does not pass `args.goals_root` to either `run_once()` or `run_forever()`. A deployment using a non-default root believes it enabled a freeze that the daemon never consults.

The tests monkeypatch `dispatch_to_tmux`, execute one process sequentially, never mutate the hold between check and lock, never test CLI-to-runtime forwarding, and explicitly prove arbitrary bypass works.

**Recommended action:** Check freeze state under the same lane lock immediately before paste; remove the public boolean or require a sealed internal order type/task allowlist; add CLI integration and concurrent hold-vs-send tests.

### 8. HIGH — PR #54 reverses an operator-directed cards decision on cherry-picked evidence, and the restored box path still breaks on ordinary emoji content

**Evidence:** supplied recovery findings `RECOVER-transcript-findings.md:45-72`; PR #54, `src/chitra/board.py` and `src/chitra/goals.py` default-only changes; `/opt/polyphony/chitra-main/src/chitra/board.py:133-176, 285-324`; deployed adapter `tools/support/chitra_adapter/harness/CLAUDE.md:30-31, 107-117, 168-188`.

**Failure scenarios:**

- The PR calls cards an “unreviewed” unilateral regression. The recovered transcript says the opposite: a later checkpoint records the operator's explicit “try a different tack” request and “rejected box table.” It also says the operator's actual pasted examples failed to arrive twice. The later phrase “desired table + colors” creates unresolved ambiguity; it does not erase the explicit rejection. This PR converts ambiguity into certainty without operator confirmation.
- `_wrap_cell()` uses `textwrap.wrap()`'s code-point count, while `_pad()` uses display width. A Goal containing 20 `🧪` characters is treated as a 20-character line but renders 40 columns. I reproduced the full effect read-only: at `COLUMNS=100`, one box roster produced frame widths `{100, 119}`. The existing alignment test uses ASCII `x`/`y`; it exercises the marker emoji but not emoji/CJK in Goal/Now/Needs. The original alignment defect is not generally fixed.
- The adapter doctrine is internally contradictory: it says status is a table at lines 30-31, says cards are default and hand-drawn box characters must stop at lines 107-117, then demands a table and says box drawing is ideal at lines 168-188. PR #54 changes only Chitra's default, leaving the monitor's governing prose inconsistent.
- `render_roster()` is the CLI/text roster relevant to PR #54. If the complaint referred to an already-running monitor, PR #54 does not fix the observed surface.

**Recommended action:** Do not merge on inferred intent. Obtain/recover the actual reference examples or an explicit ruling: cards, box, or Markdown. Then make one canonical format contract apply to Chitra, adapter doctrine, and the actual posted surface. Replace hand-rolled width logic with a tested terminal-width library or display-width-aware wrapper, including emoji sequences, CJK, long unbroken strings, and narrow terminals.

### 9. HIGH — Current goal persistence is atomic per write but not safe against concurrent writers

**Evidence:** hub-host `/opt/polyphony/chitra-main/src/chitra/goals.py:250-275, 281-324`; `/opt/polyphony/chitra-main/tests/test_goals.py:59-67`.

**Failure scenario:** Monitor A reads goals `[A, B]` to update A. Monitor B reads the same snapshot to add an open ask to B. A writes `[B, A']`; B writes `[A, B']`. Whichever `os.replace()` runs last silently erases the other's mutation. `hold_goal`, `resume_goal`, `add_ask`, `resolve_ask`, `update_now`, and PR #55's sweep all use this read-modify-replace store.

The test named `test_store_round_trip_and_atomic_write` proves only that no temporary file remains. It has no concurrent writers and does not prove transaction isolation. Adding an automated rate-limit writer materially increases the likelihood of lost state.

**Recommended action:** Serialize the full read-modify-write transaction with `flock`, or move goals to SQLite with a revision/CAS field. Add two-process lost-update tests and make stale revisions fail loudly.

### 10. MEDIUM — #2208 source is deployed, but its runtime behavior is not; the next relaunch will place a bearer credential in a world-readable file

**Evidence:** PR #2208, `infra/ccr/sol-cc:103-114`; `infra/ccr/claude-code-settings.json:6-15`; read-only hub-host inspection.

Hub-host source checkout is current (`9c98e372`, containing merge `abd54dda`), so “deployed to `/opt/polyphony/deploy-main`” is true only for source. The live runtime file `/opt/polyphony/ccr/claude-code-settings.json` had mtime `2026-07-11 23:40 EDT`, still contained `apiKeyHelper`, and had no `CLAUDE_CODE_OAUTH_TOKEN`, effort flag, or Opus default. It predates the merged source file (`2026-07-12 07:42 EDT`). No active Claude process carried `CCR_CLAUDE_CODE_WRAPPER=1` during inspection. Thus the #2208 behavior has not been proven in the active Chitra session.

On the next `sol-cc` launch, `sed` materializes `CCR_CLAUDE_CLIENT_KEY` into JSON and then `chmod 0644`s it. Even if this is a localhost-only gateway credential rather than an upstream Anthropic secret, every local user can read a bearer that authorizes gateway calls. Moving from an executable helper to `CLAUDE_CODE_OAUTH_TOKEN` also puts the credential in the child environment and common diagnostic surfaces. Dropping the misleading billing badge is not a sufficient reason to weaken local credential handling.

**Recommended action:** Before relaunch, render credential-bearing settings mode 0600 in a user-private directory, or use a helper/credential process that emits a bearer without persisting it. Add a permissions test and an actual launch test that proves the badge/auth behavior without printing the token. Do not describe #2208 as live-verified merely because the source checkout synced.

### 11. MEDIUM — #2208 hardcodes volatile provider/model IDs and validates the minified patch mostly by string presence

**Evidence:** PR #2208, `infra/ccr/agents/{luna,oracle,sol-low}.md`; `infra/ccr/claude-code-settings.json:9-14`; `infra/ccr/sol-cc:124-172`; `infra/ccr/patch-codex-oauth-scope.js` added `requireMarker()`; `infra/ccr/refresh-approved-models.test.mjs` added string assertions.

**Failure scenario:** Rename `codex-api/gpt-5.6-luna`, `gpt-5.6-sol`, `claude-opus-4-7`, or a provider prefix. Custom agents remain pinned to dead IDs. A fresh `sol-cc` launch fails its required-model/preflight gate, which is preferable to silent fallback, but an already-running session has no alias migration or generated update. The patcher's required postcondition checks that a minified string exists, not that a native custom-agent request is routed to the intended provider at the intended effort. The added tests inspect patch source strings; they do not execute the vendored gateway translation.

**Recommended action:** Define stable logical tiers once and generate agent frontmatter/settings from the approved live catalog, with explicit aliases and deprecation. Add a real gateway fixture or integration test proving native `model:` precedence and effort at egress. Keep the legacy tag until that behavioral proof exists if backward compatibility matters.

### 12. MEDIUM — Current operator-facing “green” is self-asserted state, not verification

**Evidence:** `/opt/polyphony/chitra-main/src/chitra/board.py:113-127`; supplied recovery index `RECOVER-index-and-workplan.md:87-94`.

**Failure scenario:** Any goal record with `status="working"` renders green even if `last_verified` is empty, the goal fails `check_specification()`, its pane is dead, or its transcript is stale. The recovered audit found 18/18 live goal records had incomplete intent/scope yet could still render green. The board doctrine says green means “verifiably working RIGHT NOW,” but the renderer never evaluates that fact.

**Recommended action:** Make green conditional on bounded fresh evidence (live pane/transcript progress plus a specification-valid record). Unknown/stale/incomplete should be explicit gray/red, never green. Keep the renderer deterministic by consuming a signed/fresh fact record rather than asking an LLM.

### 13. MEDIUM — The completion gate is optional and self-attested, so “CLEAN is proof” overstates what it establishes

**Resolved in v0.8.2:** turn-end classification is now forced in `watchd`,
`dispatchd` recognizes completion claims without caller opt-in, and typed
evidence retains concrete citations instead of boolean assertions. The text
below describes the pre-v0.8.2 failure that motivated the repair.

**Evidence:** `/opt/polyphony/chitra-main/src/chitra/dispatchd.py:161-176`; `/opt/polyphony/chitra-main/src/chitra/completion_gate.py:128-168`.

**Failure scenario:** A caller omits `completion_todo_items`, so dispatchd skips the gate entirely, or supplies an empty list plus `completion_has_deploy_evidence=true` and `completion_has_live_verify_evidence=true`. The gate returns CLEAN without checking a deployment, a live endpoint, or an evidence artifact. Its own docstring admits evidence determination is the caller's responsibility, but then calls CLEAN “proof.” This catches honest wiring mistakes; it is not an adversarial completion proof.

**Recommended action:** Rename the result to “caller assertions internally consistent,” require evidence references/digests, and make completion-claim classification mandatory at the event boundary if it is meant to be a gate.

### 14. MEDIUM — The 336/356 test claims are plausible counts but weak evidence for the claimed operational guarantees

**Evidence:** PR #54 adds one test to a common base and reports 336; PR #55 adds 17 guard tests plus four dispatchd tests and reports 356. The arithmetic is internally consistent with a 335-test base. No review or CI evidence was visible through the available token; check-runs returned HTTP 403.

**Assessment:** I found no proof the authors fabricated the counts. The problem is meaning:

- PR #54 tests ASCII long content and marker emoji, but not emoji/CJK in text cells, and therefore misses the reproduced width overflow.
- PR #55's “end to end” tests end at a JSON order file. They do not run pane I/O, dispatch confirmation, a scheduler, a daemon restart, a checkpoint result, or a resume result.
- Every new dispatch freeze test monkeypatches `dispatch_to_tmux`; none exercises check-vs-lock races.
- There is no failure injection for state-write/enqueue ordering, no concurrent sweep, no sidecar disappearance after pause, no CLI `--goals-root` forwarding test, no full account sibling sweep, and no Codex path.

**Recommended action:** Do not use the aggregate count in the merge decision. Require targeted fault-injection, concurrency, restart, scheduler, and real or hermetic tmux/transcript tests for the properties claimed.

### 15. MEDIUM — Version numbers are inflated; the package and product maturity are being conflated

**Evidence:** local `/opt/polyphony/chitra-main` is ten commits behind and has `pyproject.toml` 0.5.0; hub-host is current at `e4b627d` and has 0.8.0. Published tags are `v0.2.0` (2026-07-09), `v0.7.0` (2026-07-11 20:16 EDT), and `v0.8.0` (2026-07-11 22:53 EDT). There are no `v0.3.0`–`v0.6.0` tags even though the changelog narrates those versions.

**Assessment:** Yes, the version is inflated. Six minor-version increments in roughly two days do not correspond to six independently hardened maturity steps. Core features are real—pane observation, deterministic dispatch, goal state, usage evaluation, rosters—but transactionality, crash idempotence, deploy activation, evidence-backed status, and autonomous pause/resume are not production-hard. The honest feature maturity is **0.3.2 at most**.

SemVer/package indices cannot safely go backward once `v0.8.0` is published. Deleting/repointing tags would destroy provenance and break consumers; resetting `pyproject.toml` to 0.3.2 on main would also make upgrades non-monotonic.

**Recommended action:** Keep the historical tags immutable, publish a blunt release note that 0.7/0.8 were premature, align active `pyproject.toml` with the latest published line, and freeze minor releases. Ship only `0.8.x` corrective patches until the implementation genuinely reaches the maturity that 0.8 implied. Track product maturity separately as “0.3.2-equivalent.” Only if this is provably private with zero consumers/artifacts should an operator consider deleting tags and rewriting the release line; that is a repository-history migration, not routine version cleanup.

### 16. LOW — The “all failures trace to a live LLM monitor executing prose runbooks” theory is a tidy but false unifier

**Evidence supporting part of it:** Deployed adapter `tools/support/chitra_adapter/harness/CLAUDE.md:252-257, 316-358` tells a live Claude persona to self-schedule with `/loop 10m sweep`, enumerate accounts/lanes, checkpoint, verify stop, hold, due-check, re-arm, verify start, and resume. No deterministic service currently performs that full lifecycle. The recovery findings also document relaunch-gated doctrine and a prompt-governed controller (`RECOVER-transcript-findings.md:101-109, 125`; `RECOVER-index-and-workplan.md:87-94`). Therefore the current rate-limit automation and much higher-level fleet judgment do depend on an LLM faithfully executing prose.

**Evidence refuting the universal claim:**

- The incident report says Chitra's three deterministic daemons stayed active with zero restarts; the observed outage was a CCR credential/backend failure (`INCIDENT-solcc-api-errors.md:7-11, 24-28`).
- CCR token desynchronization, provider duplication, image stripping, config reload windows, and the #2219 repair races are gateway/code defects, not prose-runbook failures.
- Box width overflow is deterministic rendering code.
- Goal lost updates and dispatch double-delivery windows are storage/queue concurrency defects.
- The statusline sidecar silently returns success on most collection failures (`chitra-usage-snapshot:7-59`), creating missing data before any LLM can act.
- Source-deployed/runtime-inactive changes and stale/unsupervised host checkouts are release/topology defects.

**Recommended action:** Keep the useful narrower diagnosis: “several control-loop actions are still prompt-governed and disappear when the monitor session is absent or drifts.” Do not use it to explain away independent gateway, storage, rendering, deployment, or concurrency failures.

## Over-build / over-reach assessment

- **PR #2219:** A five-minute all-model, cross-provider live verification loop is grossly broader than detecting one OAuth-file generation change. It mutates observability, spends tokens, touches unrelated providers, and rewrites shared config. The minimal repair is targeted token-generation reconciliation plus one Codex preflight and backoff.
- **PR #55:** 1,009 added lines across ten files, a public bypass field, a new capability entry, a new CLI, and broad daemon plumbing are not justified while the scheduler is absent/default-disabled, Codex is unsolved, queue semantics are wrong, and transitions are nontransactional. The minimal first increment is a durable pause state machine and deferred-order semantics behind an opt-in timer.
- **PR #54:** Changing two defaults is small, but embedding a contested operator quote into a library docstring and changelog turns disputed transcript interpretation into product doctrine. That is overreach.
- **PR #2208:** Adding `litellm`, `dspy`, and `predict-rlm` to `tools/taste_agent/deploy/requirements-hpc.txt` is unrelated dependency expansion inside a CCR relaunch/badge/model-routing PR. It increases install and supply-chain surface and should have been a separate change with its own verifier.
- **Current Chitra:** Several “proof” abstractions—HMAC ledger absence, optional completion gate, color status—are more elaborate than their evidence foundations. The code often documents limitations honestly, but operator-facing claims outrun those limitations.

## PR verdicts

| PR | Verdict | Reason |
|---|---|---|
| first-polyphony/chitra #54 | **DO-NOT-MERGE** | Operator intent is unresolved and the supplied transcript contradicts the “unreviewed cards regression” premise; box rendering still overflows on emoji text; adapter and actual board surfaces remain unreconciled. |
| first-polyphony/chitra #55 | **DO-NOT-MERGE** | It discards frozen orders, does not stop active work, has nontransactional pause/resume, can strand lanes, leaves Codex unsolved, has account-identity holes, a TOCTOU, an unrestricted bypass, and a broken CLI option. |
| first-polyphony/polyphony #2219 | **DO-NOT-MERGE** | The timer's healthy path is mutating and expensive, unrelated failures trigger broad repair, config writes race, and duplicate cleanup is not implemented. The emergency native fallback itself is a valid CCR bypass, but it does not redeem the reconciler. |
| first-polyphony/polyphony #2208 (merged) | **MERGE-WITH-FIXES** (retrospective) | Native fallback precedence and fail-loud model availability are directionally sound, but source is not active runtime yet, the next relaunch writes a bearer to mode 0644, routing proof is string-based, and model IDs are brittle. Fix before the next `sol-cc` relaunch. |

## Bottom line

Current Chitra is a promising deterministic toolkit wrapped around an LLM-operated control plane, not a mature autonomous fleet controller. Its sensors, tmux safety checks, state models, and test volume are substantial for a very young project, but the load-bearing guarantees are not there: goal updates can be lost, dispatch can duplicate, green is not verified, completion evidence is self-attested, pause/resume is still prose-driven, deploy activation is inconsistent, and the proposed deterministic replacement is unsafe. The honest **product maturity is v0.3.2-equivalent at most**. Preserve the already-published v0.8.0 history, stop minor-version escalation, and use 0.8.x only for hardening until transactionality, idempotence, evidence-backed status, and live deployment closure are proven.
