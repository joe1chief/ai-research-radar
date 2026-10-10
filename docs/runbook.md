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

If collection stops after an HTML HTTP 200, check the following `collector parse`
log before increasing network or workflow timeouts. In the October 2026 Cognition
failure, research metrics such as `best@16` followed by nested CUDA code triggered
catastrophic backtracking in the CSS cleanup regex. CSS at-rules now use a
recognized-name match and a forward brace scan; metrics and incomplete blocks
are retained. Regression tests run those inputs in a subprocess with a wall-clock
timeout so a parser regression fails instead of hanging the test job.

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

## Capacity and read-only maintenance

`radar maintenance` is now diagnostic only, regardless of `RADAR_DRY_RUN` or
`RADAR_RAW_STORAGE_ENABLED`. It does not sync issuers, create schema, delete
Storage objects, clear raw paths, prune the usage ledger, or commit changes.
PostgreSQL uses a read-only transaction; SQLite enforces `query_only`.
The existing schedule, concurrency group and health alerts are unchanged.

- The capacity alert is **350 MiB (367,001,600 bytes)**, an application warning,
  not a verified hosted database hard limit. `capacity_hard_limit_bytes` is null.
- `failure_reasons` enumerates every active alert: capacity, sources with at
  least three consecutive failures, and production expired raw references.
  Exit code 1 still signals these conditions even during a preview.
- `source_failure_details` includes counts, status, last attempt/success, HTTP
  status, latency, enabled/group and next due time. Raw legacy error messages,
  arbitrary metadata and URLs are never printed. A source may retain old
  failures when a group budget prevents visiting it; compare attempt timestamps
  with collection logs before calling it a fresh failure.
- `database_relations` lists up to 20 largest user relations, table/TOAST and
  index bytes, estimated live/dead tuples and autovacuum/analyze timestamps.
  Relation sizes and tuple estimates do not establish reclaimable disk space.
  Restricted/failed catalog reads yield a safe `relation_diagnostics_error`.
- `expired_raw_objects_pending` is a compatibility field counting expired
  **database path references**, not verified objects. Use the explicit reference,
  unique path and storage object fields instead. Skipping enumeration is explicit
  (`raw_storage_disabled`, `not_requested`, or `missing_storage_credentials`).

### Private cleanup preview

Against an existing database with locally supplied credentials:

```bash
radar maintenance --preview > /private/local/path/maintenance-preview.json
# Optional explicit Storage listing; this is read-only even when uploads are disabled:
radar maintenance --preview --include-storage > /private/local/path/storage-preview.json
```

Do not run or publish this detailed preview in a public Actions log: it contains
private object paths and version IDs. `--include-storage` uses the Storage list
API (POST with read-only semantics), never DELETE/PUT/upload. The list must finish
within its page/depth bounds; incomplete or failed listings are reported as
unavailable and fail the preview, with counts remaining unknown. Missing
credentials also fail an explicitly requested Storage preview.

Preview includes the UTC raw cutoff, exact version/path mappings, dated bucket
objects, objects unreferenced by any version, candidate paths and paths protected
by a recent version reference. DB-only candidates are unverified; confirmed
candidates require successful bucket enumeration. Even confirmed candidates are
not authorization to delete. Date-prefix orphan discovery is limited to the
existing YYYY/MM/DD raw layout, not a complete bucket inventory. The preview
can become stale while collection is active.

No apply/delete option is exposed. Before any future deletion, obtain separate
user approval for an exact, fresh manifest, its bucket, cutoff, affected rows,
protected references, expected object-byte impact and verified restore copies
(including version-to-path mapping). The current candidate manifest does not
estimate object bytes and is insufficient on its own to approve deletion.
Revalidate the approved manifest against current state before applying it. Raw
bucket deletion does **not** guarantee a smaller PostgreSQL database; database
retention, VACUUM FULL and purchasing capacity need separate decisions. The
old automatic 60-day ledger pruning is also replaced by a count-only diagnostic.

For evidence and unresolved decisions from the October investigation, see
[Maintenance diagnostics](maintenance-diagnostics.md).
