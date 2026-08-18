# Source and editorial policy

## Topic contract

The seed article defines four technical engines plus industrial capital. Topic
keywords live in `configs/topics.yml`; changing that file is an editorial
change and must be reviewed like code.

Generic long-context work is not a Long Horizon Task unless it also contains
an agent, model, robot, planning, or autonomy signal. Generic synthetic data is
not Self-Evolving unless the work contains a closed feedback or self-training
loop. Safety and mechanistic interpretability remain separate subtracks in
storage even though the digest renders them under one top-level section.

## Evidence precedence

1. Regulator, exchange, official standard, paper, official code or benchmark.
2. Company announcement and named investor/partner announcement.
3. Reputable independent media.
4. Community discussion and unattributed reporting.

HTTP collection retries only timeouts, network failures, `429`, and `5xx`
responses. Other `4xx` responses fail immediately so configuration or access
denials are not multiplied. The top-level HTTP failure boundary stores only the
configured source ID, response status, retryability, and hostname; it excludes
response bodies, headers, paths, and query strings. SEC collectors use their
own contact-bearing identity and a shared four-requests-per-second domain
throttle.

For capital events, level 3 and 4 sources are discovery-only. Price movement,
broker commentary, routine monthly returns, and ordinary commits do not create
events. The observation section requires two distinct configured media source
IDs (with publisher-domain fallback only for legacy imports); two URLs from one
publisher never satisfy the gate.

## Materiality

A change is material when it changes at least one of:

- the central claim, result, capability, safety limitation, or artifact;
- paper version, experiment, code availability, or acceptance status;
- model/framework/protocol/benchmark release state;
- financing amount, named participants, transaction state, regulatory result,
  formal filing, compute commitment, or material contract.

Navigation, typography, tracking parameters, mirrored announcements, timestamp
formatting, and ordinary commits are minor changes.

## Score

The deterministic score is capped to `[0, 100]`:

| Component | Maximum |
| --- | ---: |
| Topic fit | 30 |
| Source and evidence | 25 |
| Novelty/materiality | 20 |
| Impact | 15 |
| Actionability | 10 |

Penalties are applied after the positive components: duplicate `-40`, rumor
`-25`, no primary link `-20`, ordinary commit/weight shard `-20`.

Thresholds are `>=80` alert candidate, `65-79` digest priority, `45-64`
ordinary digest, and `<45` archive-only. A score never overrides the evidence
gate.
