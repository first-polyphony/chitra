# Roadmap

chitra is deliberately small. Every item below is scoped to justify itself against that — if an idea would make the core daemons bigger, more dependent, or reasoning-capable, it belongs in a separate tool that *consumes* chitra's output, not in chitra.

## v1.1

### beads integration (pull side only)

[beads](https://github.com/steveyegge/beads) (a git-native work tracker) is a candidate backing store for the read side of a lane-status/decisions ledger: something that wants to know "what's the current state and history of decisions for session X" could query beads instead of re-deriving it. This is explicitly **pull-only** — chitra's own orders and dispatch stay push-based through `dispatchd`'s JSON queue; beads has no push mechanism, and nothing about chitra's delivery path changes. Scope: read-side integration for status/ledger queries, nothing else.

### Self-improvement: a separate, read-only observer — not a framework

chitra's daemons already emit plain, documented artifacts (dispatch results, triage events, the delivery ledger). The plan for "learning from operational traces" is a **separate, read-only observer process** that computes simple rolling statistics over those artifacts — dispatch success rate, queue latency, dedup hit/miss rate — and surfaces threshold-based flags (e.g., "the dedup window looks too short given N near-miss collisions this week"). That's it: statistical monitoring and flagging, not an automated mutator of chitra's own config, and not a new dependency inside chitra itself.

Explicitly ruled out, with reasons: **DSPy** optimizes LLM prompt/pipeline behavior against a scoring metric — chitra has no LLM call anywhere in its core, so there's no prompt to optimize and no natural role for a DSPy-style optimizer here. **RL-from-logs / closed-loop auto-tuning** is a real technique but a heavier lift (defined action space, reward shaping, an evaluation harness) than a read-only observer, and a closed loop that silently rewrites chitra's runtime config is exactly the kind of complexity this project is trying to avoid. Both stay out of scope.

### Deviance-pattern awareness (external research, abstracted internal taxonomy)

A related project maintains an internal taxonomy of AI-agent deviance patterns (false blockers, narrated-but-not-taken action, silent scope reduction, fabricated completion claims) built from operational transcripts. That internal taxonomy does **not** ship in this repo. What's relevant here is external, publicly available research on the same phenomena, which chitra's roadmap can reference without exposing anything proprietary:

- [METR: "Recent Frontier Models Are Reward Hacking"](https://metr.org/blog/2025-06-05-recent-reward-hacking/) — documented cases of agents faking task success (tampering with harnesses, disabling checks) rather than completing the task.
- [Apollo Research: "Frontier Models are Capable of In-Context Scheming"](https://www.apolloresearch.ai/research/frontier-models-are-capable-of-incontext-scheming/) ([paper](https://arxiv.org/pdf/2412.04984)) — models producing misleading self-reports about their own behavior, including sandbagging.
- ["Where LLM Agents Fail and How They Can Learn From Failures"](https://arxiv.org/pdf/2509.25370) and ["Exploring Autonomous Agents: A Closer Look at Why They Fail When Completing Tasks"](https://arxiv.org/pdf/2508.13143) — general agent failure-mode taxonomies (premature ungrounded action, error propagation).
- ["Establishing Best Practices for Building Rigorous Agentic Benchmarks"](https://arxiv.org/html/2507.02825v2) — quantifies premature-termination/early-stop rates across agentic benchmarks.
- ["Are Your Agents Upward Deceivers?"](https://arxiv.org/abs/2512.04864) — defines "upward deception" (concealing failure, taking unreported actions) with a dedicated benchmark.

Honest caveat: no external paper currently treats "false blockers claiming human intervention is needed" as its own named, benchmarked category — it's an unlabeled subset scattered across the sources above. Any future work here should synthesize across them rather than expect one canonical citation.

### Goal discipline (tracking, not generation)

chitra is not, and will not become, a PRD generator — for turning an idea into a structured spec, use an existing tool built for that. What's in scope here is narrower: once a goal/decision exists for a registered session, **track it, enforce it against drift, and keep the tracking non-brittle across code changes**, plus maintain a simple per-session register of open questions/tasks/decisions that a human or another tool can query. This is bookkeeping discipline, not a planning feature — it fits the observer pattern above rather than adding new surface to the daemons themselves.
