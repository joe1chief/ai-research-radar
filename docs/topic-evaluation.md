# Topic acceptance corpus

The production gate expects UTF-8 JSONL with at least 100 unique, independently
reviewed rows. Do not copy model-generated labels into this file. Sample across
all five technical tracks, industrial-capital event types, multilingual titles,
near misses, and hard negatives.

Each row contains:

```json
{"id":"review-001","title":"Long-running agents with persistent memory","text":"...","event_type":"PAPER","expected_top1":"long_horizon","reviewed_by":"reviewer-handle"}
```

Use `null` for `expected_top1` when the record should remain archive-only. The
allowed positive values are `long_horizon`, `autonomous_agent`, `self_evolving`,
`mechanistic_interpretability`, `safety_governance`, and `industrial_capital`.

Run the production path:

```bash
radar evaluate-topics --dataset labels.jsonl
```

The command reports Top-1 precision, accuracy, confusion pairs, model-call
failures, and each mismatch. It fails closed when there are fewer than 100 rows,
precision is below 85%, any duplicate ID exists, a reviewer handle is empty, or
any Qwen Flash call fails. `--rules-only` is for local diagnostics, not release
acceptance.
