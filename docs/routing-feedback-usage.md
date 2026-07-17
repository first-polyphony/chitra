# Routing feedback usage report

`tools/routing_feedback/routing_feedback_usage.py` is a separate consumer of
Chitra artifacts. It is not part of the installable `chitra` package and is not
on the dispatch path.

The tool reads:

- `ledger.jsonl`, using Chitra's signed append-only delivery record shape
- an optional `routing.yaml` file with `defaults: {task_type: routing_hint}`

It writes report artifacts only:

- `routing-feedback-usage-report.json`
- `routing-feedback-pr-body.md`
- `routing-feedback.diff`

The diff file is intentionally empty today. Chitra's current ledger proves only
that delivery happened. Its entries include `order_id`, `session_ref`, `tag`,
`routing_hint`, `message_hash`, `sent_at`, and `signature`; they do not include
task outcome, judge score, human override, or `task_type`. Since `routing.yaml`
maps `task_type` to `routing_hint`, the ledger cannot justify a specific config
change by itself.

The script takes a conservative posture: it enforces a freshness window, a
minimum sample count, a maximum dominant hint share, and a maximum
changed-line budget. Because the available telemetry
is delivery/frequency only, those gates can only decide whether the usage report
is meaningful; they cannot produce a success-based routing recommendation.

Example:

```bash
python tools/routing_feedback/routing_feedback_usage.py \
  --ledger-jsonl /var/lib/chitra/ledger.jsonl \
  --routing-yaml docs/routing.yaml.example \
  --output-dir /tmp/chitra-routing-feedback
```
