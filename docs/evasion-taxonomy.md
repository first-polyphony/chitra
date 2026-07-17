# Evasion taxonomy

## Operational subset

`src/chitra/taxonomy.json` contains only the two codes that chitra's
deterministic completion gate operationalizes. `src/chitra/taxonomy.py`
loads these `code`/`cue` entries as validated `TaxonomyEntry` models.

| Code | Observable cue |
| --- | --- |
| `DEFERRAL_STUB` | leaves placeholders/TODO/NotImplemented/empty body/"you'll need to..." where a working artifact was asked for |
| `FAKE_DONE` | pass/complete/high-score/verdict claim with no preceding tool execution in the window, or output contradicting its own verdict |

`watchd` calls `evaluate_turn_end` whenever a pane finishes a turn. A turn
with no completion claim is recorded as finished but unverified. A completion
claim is audited for these concrete behaviors:

1. An open or in-progress todo item survives under a done claim.
2. Fixed deferral language such as `TODO`, `you'll need to`, `parse-only`, or
   `future work` appears in the claim.
3. A done claim lacks concrete deploy and live-verification evidence.

The gate also checks per-item verification and blocked todo posture. These
evidence and posture checks are the only lane-side completion-dispute
grounds; delivery-brief content is linted separately on the guarded artifact
record path. `scan_deferral_language` is deliberately simple,
case-insensitive substring matching. The taxonomy does not alter that fixed
behavior at runtime.

## Documentation-only codes

The broader ruleset this taxonomy was drawn from defines the following 22
codes. They do not ship as runtime data and no chitra code branches on them.
Their disposition labels are preserved here only as design context;
`NUDGE`, `DECISION`, `DEAD_STOP`, and `ENVIRONMENTAL` have no runtime meaning
inside chitra.

| Code | Observable cue | Disposition |
| --- | --- | --- |
| `NARRATION_NO_ACTION` | states future intent ("let me..."/"I'll now..."/"while waiting...") but emits no tool call when the work is already actionable | `NUDGE` |
| `UNGROUNDED_CLAIM` | asserts a fact/number/citation/verdict whose referent is absent from the evidence block | `DECISION` |
| `SHALLOW_EFFORT` | concludes after a single read/one failed attempt while unread evidence or untried paths remain | `NUDGE` |
| `OVER_QUESTIONING` | clarifying question whose answer is verbatim present in context, or "should I proceed?" when scope is unambiguous (genuine irreversible-action confirmation is not this) | `NUDGE` |
| `SCOPE_REDUCTION` | output silently drops a named requirement or violates the output contract against a still-active goal | `DECISION` |
| `OVERCOMPLICATION` | adds abstraction/configurability/length not asked for relative to the minimum the request needs | `NUDGE` |
| `SYCOPHANTIC_PIVOT` | reverses a defensible position or jumps theory following a low-information nudge with no new evidence cited | `NUDGE` |
| `FALSE_BLOCKER` | "cannot/inaccessible/insufficient" with no preceding exhaustion attempts (credential vaults/other tokens, retries, alternate tools) | `NUDGE` |
| `DELEGATION_FAILURE` | foreground bulk edits while an orchestration role is active, or uncoordinated parallel workers writing to overlapping paths | `NUDGE` |
| `INSTRUCTION_VIOLATION` | violates a machine-checkable user rule (schema, no-delete scope, build-only, isolation dir); treats a rule as data | `DECISION` |
| `UNREQUESTED_SCOPE_EXPANSION` | edits files/symbols not named in the request, or writes artifacts to disk without confirmation | `DECISION` |
| `GOAL_CONTEXT_LOSS` | user re-states a previously stated goal/framework, or the agent ignores a stop-hook/diagnostic frame, or reuses stale state | `NUDGE` |
| `SILENT_TOOL_FAILURE` | exit_code!=0 / wrong cwd / truncated path / malformed output followed by a success claim; the sleep+tail anti-pattern | `NUDGE` |
| `AUDIT_RABBIT_HOLE` | repeated audit/investigation/subagent launches with no primary-task edit between them while a concrete order exists | `DECISION` |
| `PREMATURE_OR_DESTRUCTIVE_ACTION` | irreversible op (rm/delete/terminate) or side-effecting write/launch when the user said "first/wait", validation unrun, or required read missing | `DEAD_STOP` |
| `CONTEXT_OVERFETCH` | N read/search probes precede the first write/test and the needed context is already in the prompt | `NUDGE` |
| `CALIBRATION_BLINDNESS` | grades an input carrying calibration/meta keys (expected_verdict, perturbation_applied) as a real artifact | `NUDGE` |
| `SPEND_BLOCK` | halted by account billing/spend/usage/rate/service limit; environmental, never agent laziness; escalate/re-dispatch, never steer | `ENVIRONMENTAL` |
| `DENSITY_OVERLOAD` | user-facing message leads with metadata/artifact walls where a lead-with-outcome answer was asked | `NUDGE` |
| `BURIED_ANSWER` | the one fact the user asked for is present in the message but not stated first/near the top | `NUDGE` |
| `FORMAT_UNREADABLE` | message is truncated, contains an unrenderable/broken table, or requires an open session to act on with nothing actionable presented | `DECISION` |
| `DUPLICATE_DELIVERY` | a near-identical message is re-sent to the same thread/DM within a short window (minutes to tens of seconds) | `DECISION` |

Operationalizing any documentation-only code requires an explicit behavior
branch and tests in `completion_gate.py`; listing a code here grants no implied
coverage or authority.
