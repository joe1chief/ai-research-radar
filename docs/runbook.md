# Operations runbook

## Required production setup

1. Create one Supabase project and apply migrations in `supabase/migrations`.
2. Create one AgentMail inbox with an inbox-scoped API key.
3. Deploy the AgentMail webhook Edge Function and register an inbox-scoped
   webhook for sent, delivered, bounced, rejected, and complained events.
4. Add the secrets and variables documented in `.env.example` to GitHub.
5. Run the workflows manually in shadow mode before enabling live delivery.

`SEC_USER_AGENT` must contain a monitored public operations email and must be
configured as a repository variable. Do not put credentials or a private
recipient address in it. A SEC `403` is non-retryable: verify this identity
before treating the regulator endpoint as unavailable.

Never paste secret values into issues, logs, the public archive, or repository
variables. Recipient addresses are secrets, not variables.

## Shadow-to-live sequence

1. Run a 14-day backfill. Confirm that no historical alert was sent.
2. Keep `DELIVERY_MODE=shadow` for three daily runs. Review the drafts, evidence
   labels, duplicates, and public projection.
3. Set `DELIVERY_MODE=live`, manually run `daily-digest`, and verify the webhook
   reaches `delivered`.

To validate AgentMail independently of a slow collection bootstrap, manually
dispatch `Daily Digest` with `delivery_only=true` while `DELIVERY_MODE=shadow`
and `RADAR_DRY_RUN=true`. This opt-in path skips model validation, collection,
and enrichment, then composes from the current database and creates or updates
the review Draft. Confirm the Draft has no `send_at` and the run reports zero
scheduled, sent, and failed deliveries. Scheduled runs and manual runs with the
default `delivery_only=false` always retain the full collection pipeline; this
mode refuses to run under live/non-dry-run settings and is not a substitute for
fixing or completing a failed paper sweep.

`radar backfill` clears stored HTTP validators and source watermarks only for
the explicit replay, then rebuilds them. It commits usable archive records but
exits non-zero if any source fails, degrades, or reaches a pagination budget;
resolve the named source (for example OpenReview authentication) and replay
before starting the three-day shadow window.

## Delivery uncertainty

If sending a Draft times out, mark the delivery `unknown`. Do not create a new
Draft. Reconcile the original Draft ID, delivery-key label, messages, and
webhook events:

- Draft remains: retry that Draft ID.
- Draft disappeared and a matching message exists: mark sent.
- Neither can be proven: remain fail-closed and surface the problem in the
  operations section of the next digest.

## Source failures

### Collection and database budgets

Radar emits flushed INFO logs for runtime setup, source fetch/persistence/commit,
HTTP attempts and redirects, retry waits, and ingestion progress every 100 items.
HTTPX/httpcore INFO logs are suppressed because they can expose full request URLs.
The collection command also prints its final counts, including `budget_exhausted`.

The default source budget is 120 seconds and each group has a 600-second budget.
These conservative starting limits leave room inside the existing 45/60/90-minute
job limits; they are not measured production SLOs or a guarantee of on-time email.
Configure `RADAR_COLLECT_SOURCE_BUDGET_SECONDS` and
`RADAR_COLLECT_GROUP_BUDGET_SECONDS`; a source may override its budget with the
positive `collection_budget_seconds` field in `configs/sources.yml`. Monitor paper
batch sizes and ingestion progress before increasing a repeatedly exhausted source.
The production workflows forward matching repository variables to these settings;
their cron expressions, shared writer group and job timeouts are unchanged.

The same monotonic budget covers request attempts, streamed response chunks,
redirects, domain throttling, page intervals, and persistence boundaries. Checks
are cooperative: a running parser, socket phase or commit must return before the
next check, so this is not a hard process-kill timer. Database defaults are a
10-second libpq connect timeout, 30-second statement timeout and 5-second lock
timeout, configured via `RADAR_DB_*_TIMEOUT_SECONDS` in `.env.example`. SQL limits
are transaction-local, and active collection statements use the smaller remaining
source budget. These settings do not bound every OS/network failure or provide a
strict deadline for COMMIT. Inspect commit-stage logs before replaying uncertain work.

A source budget failure rolls back its batch without advancing its cursor, then
records failure health outside the expired budget so healthy sources can continue.
This recovery has a separate 10-second cooperative grace budget, configurable
with `RADAR_COLLECT_RECOVERY_BUDGET_SECONDS`; if health persistence also fails,
the transaction is rolled back and its source ID/error class are logged.
An exhausted group stops launching more sources and makes `radar collect` fail.
Valid numeric or HTTP-date `Retry-After` values are never shortened to fit a budget:
the source is deferred instead, and `source_health.metadata.retry_not_before`
prevents even `--force` from requesting it early. No partial-page cursor is committed.

AgentMail uses a 20-second request timeout and a 90-second operation retry budget
(`RADAR_AGENTMAIL_TIMEOUT_SECONDS`, `RADAR_AGENTMAIL_RETRY_BUDGET_SECONDS`). Every
SDK call has `max_retries=0`; only the adapter retries safe operations and explicit
429 rejections. Ambiguous sends, including 408/409/5xx, remain `unknown` for
reconciliation. A long Retry-After ends the current operation without sleeping or
making an early request; do not manually replay it before the provider permits.

For local checks use `pytest tests/backend`; for real database timeout/rollback
checks set `RADAR_TEST_POSTGRES_URL` to an isolated PostgreSQL database and run
`pytest tests/integration`. Never use production credentials for these tests.

A source failure is isolated. After three consecutive failures, record a
degraded source-health state. The nightly job prints the exact failed-source
list and exits non-zero so GitHub Actions raises the operational notification;
the application does not claim a separate per-source paging service.

Use `source_health.last_http_status` to distinguish access/configuration
failures from transport failures. `429` and `5xx` remain retryable; other
`4xx` responses are recorded after one request. Top-level source-failure logs
intentionally contain only source ID, error class, connection-invalidated
state, HTTP status, retryability, and hostname.

Review sources monthly. GitHub may disable scheduled workflows in an inactive
public repository; repository notifications and the documented,
default-branch-only `workflow_dispatch` path are the recovery controls.

## Capacity

- Warn when Postgres reaches 350 MB.
- `radar maintenance` exits non-zero at the 350 MB threshold or when a source
  has failed three consecutive times; these are visible workflow failures.
- Keep raw HTML for at most 14 days and do not store PDFs. If Storage deletion
  fails, maintenance leaves the database pointer intact and fails the job so it
  can be retried safely.
- Embed only new or materially updated, topic-relevant records.
- Export monthly public JSON shards and a 30-day search index.
