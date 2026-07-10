# Self-tuning a chitra policy

`policy.yaml` is the single mutable vocabulary surface for completion-gate and dispatch matching. The shipped defaults are used when no policy file is configured; see [policy.yaml.example](policy.yaml.example) for every field.

Build a labeled JSONL corpus from your own deployment history. Each completion case records todo items, transcript text, both evidence flags, and an expected `CLEAN` or `COMPLETION_DISPUTE` verdict. Each voice case records a nudge and whether it should be blocked. Keep a separate holdout corpus that the policy editor never sees.

Run the immutable evaluator against a candidate policy:

```bash
python -m chitra.replay_eval --fixtures path/to/fixtures --policy-config path/to/policy.yaml
```

It emits a fenced metric block with overall accuracy, completion and voice accuracy, false-dispute and missed-dispute rates, and a floor-friendly `non_false_dispute` value. Evaluation is deterministic: identical fixtures and policy produce identical output.

Any propose-run-measure-keep harness can edit only `policy.yaml`, rerun this evaluator, and retain a change only when its metric improves without breaching a false-dispute floor. Hold out part of the corpus for the final measurement so a phrase list does not merely memorize the examples used to tune it.
