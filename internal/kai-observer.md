# The real observer: Kai

The public docs (`README.md`'s "A note on the observer pattern" section, `docs/DESIGN.md`, and `docs/ROADMAP.md`'s self-improvement section) describe an internal read-only observer pattern in deliberately generic terms — "an internal tool, a dashboard, a future OSS project." That's intentional. This file names the real thing, for maintainers only.

The actual, currently live consumer is **Kai**, Crossroads' internal technical-PM agent (backed by Hindsight memory). Kai is genuinely internal fleet infrastructure — it has no relevance to external chitra users, which is why it isn't named in the public-facing docs.

Kai watches three chitra outputs on a read-only basis:

- `ledger.jsonl` — the HMAC-signed delivery ledger written by `chitra/ledger.py`
- `triaged.log` — written by `chitra/triaged.py`
- `events.log`

It mines these for engineering-decision and validation-lesson signal, which feeds Kai's own reflect and wiki-sync timers.

Confirmed live as of 2026-07-09 via the Crossroads wiki page `projects/software/agents/crossroads/monitor-program/memory.md`, which states: "Kai observer LIVE (feed /kai-feed/ on tophand Caddy; Kai-side reflect+wiki timers on trailhead)."

**Do not use this to inform public docs.** README.md, docs/DESIGN.md, and docs/ROADMAP.md should keep describing this pattern generically and should not be edited to name Kai. Kai is internal fleet infrastructure, not something external chitra users need or should know about.
