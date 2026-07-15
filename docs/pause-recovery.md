# Pause recovery

Every managed pause that reaches the verified `held` phase adds one record to
`$CHITRA_STATE_DIR/pause_recovery.json`. When `chitra-rate-limit-guard` is run
with `--goals-root`, the file lives under that state root instead. The document
is updated under an exclusive lock with an atomic replacement, preserves prior
pause records after their transactions resume, and deduplicates a retried
held transition by its stable pause ID.

Each record contains:

- the session ref and hold reason;
- the transcript path already proved by dispatch and used for quiescence
  verification, without taking a second pane capture;
- a human-readable resume note built from the lane's stored `GoalRecord` goal,
  current-work context, and completion condition;
- the scheduled `resume_at` time and the time the pause was verified.

To reconstruct a pause, find the newest record for the session ref, inspect its
`transcript_path` for the last work before quiescence, and use `resume_note` as
the work contract. The normal and safest resume path is the same transaction
machine: after `resume_at`, ensure the usage sidecar has emitted a fresh `ok`
verdict, keep `dispatchd` running, and invoke the configured
`chitra-rate-limit-guard --usage-dir ... --host ...` sweep (including the same
`--goals-root` and `--queue-dir` overrides used for the pause). Subsequent
sweeps advance `held → resume_requested → resume_sent`, confirm delivery of the
goal-derived re-arm nudge, clear the hold, and requeue deferred work.

An attached tmux client does not change pause eligibility; attachment is only
a delivery-method liveness signal. The sole never-pause refs are Chitra's own
Hub-host monitor/harness sessions, `hub-host:monitor:*` and
`hub-host:boomtown:*`.
