# AI Research Radar infrastructure

This directory documents the production boundary around the Python `radar` CLI.
GitHub Actions performs short-lived scheduled work, Supabase is the private state
store, AgentMail owns scheduled delivery, and GitHub Pages serves only sanitized
static JSON.

## 1. Provision Supabase

1. Create a Supabase project in a region appropriate for the operator. Keep the
   project and Storage bucket private.
2. Link and apply the migration:

   ```bash
   supabase link --project-ref YOUR_PROJECT_REF
   supabase db push
   ```

3. Confirm that `radar-raw` is a private bucket and that every table listed in
   the migration has RLS enabled with no `anon` or `authenticated` policy.
4. Production requires hosted Supabase pgvector. The migration adds
   `embedding_vector vector(1024)` and an HNSW cosine index; startup fails closed
   if the vector column is unavailable. SQLite remains the credential-free local path.
5. Use the pooler session connection string for GitHub Actions. Percent-encode
   special characters in the password. Never put this URL in repository
   variables or Pages data.
6. The `radar_runtime` login is intentionally limited to CRUD on the 15 named
   Radar tables. It has one explicit RLS policy per table, no `BYPASSRLS`, no
   membership or object ownership, no non-system function access, and no
   default grants. Validate the live boundary without retaining probe data:

   ```bash
   RADAR_DATABASE_URL=... uv run python infra/scripts/validate-runtime-db.py
   ```

   Any future function in `public` must revoke `EXECUTE` from `PUBLIC` in the
   same migration and explicitly grant only its intended caller. PostgreSQL
   otherwise makes new functions executable by `PUBLIC`; the runtime validator
   treats that drift as a failure. Changing the database-wide function default
   privileges is a separate Supabase governance decision, not part of this
   role's grants.

The server-side exporter uses an explicit ORM field allow-list and validates the
result before writing JSON; the SQL view remains a defense-in-depth audit surface.
Both exclude archived and unconfirmed events and omit raw text, prompts,
recipient data, delivery state, webhook payloads, and storage paths. No browser
receives a Supabase key.

## 2. Deploy the AgentMail webhook

Create an AgentMail webhook secret and store it in Supabase Functions secrets:

```bash
supabase secrets set AGENTMAIL_WEBHOOK_SECRET=whsec_REDACTED
supabase functions deploy agentmail-webhook --no-verify-jwt
```

`SUPABASE_URL` and the hosted `SUPABASE_SECRET_KEYS` JSON dictionary are supplied
by the Edge Function runtime. The webhook reads its `default` new-style secret
key and does not depend on the legacy service-role key. For local serving only,
set `SUPABASE_SECRET_KEY` to an explicit new-style secret key.

Register this URL with AgentMail:

```text
https://YOUR_PROJECT_REF.supabase.co/functions/v1/agentmail-webhook
```

Subscribe to `message.sent`, `message.delivered`, `message.bounced`,
`message.rejected`, and `message.complained`. The endpoint intentionally has no
Supabase JWT gate because AgentMail cannot provide one; it instead verifies the
raw body and all three `svix-*` headers before touching the database. The RPC
stores `event_id` once and applies delivery states monotonically, so webhook
retries and out-of-order events cannot duplicate or regress a delivery.

Local Edge Function checks, when Deno is installed:

```bash
cd supabase/functions/agentmail-webhook
deno task check
deno task test
```

## 3. Configure GitHub

Configure the repository's Pages source as **GitHub Actions**. Add these Actions
secrets:

| Secret | Purpose |
| --- | --- |
| `SUPABASE_DB_URL` | Private Postgres/pooler connection used by `radar` |
| `SUPABASE_URL` | Server-side maintenance/Storage endpoint |
| `SUPABASE_SECRET_KEY` | Server-side maintenance/Storage secret key |
| `DASHSCOPE_API_KEY` | Default DashScope chat and shared embedding provider |
| `YICLOUD_API_KEY` | YiCloud TokenFactory chat; add only after rotating any exposed key |
| `ALPHAXIV_ACCESS_TOKEN` | Optional alphaXiv Top-N enrichment token |
| `OPENREVIEW_ACCESS_TOKEN` | Optional OpenReview token when guest API requests are challenged |
| `AGENTMAIL_API_KEY` | Draft creation, sending, and reconciliation |
| `AGENTMAIL_INBOX_ID` | The v1 `@agentmail.to` sender inbox |
| `DIGEST_RECIPIENT` | The single private recipient address |

Add these repository variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `RADAR_USER_AGENT` | required | General identity for non-SEC collection requests |
| `SEC_USER_AGENT` | required | Dedicated SEC identity containing a monitored public operations email |
| `LLM_PROVIDER` | `dashscope` | `dashscope` or `yicloud`; switch to YiCloud only after its smoke test passes |
| `YICLOUD_CLASSIFIER_MODEL` | required for YiCloud | Account-verified TokenFactory classifier model ID |
| `YICLOUD_SUMMARIZER_MODEL` | required for YiCloud | Account-verified TokenFactory summarizer model ID |
| `YICLOUD_JSON_RESPONSE_FORMAT` | `true` | Set `false` only if the strict prompt-only smoke test passes |
| `LLM_MAX_TOKENS` | `1200` | Shared production and smoke response limit (`64`–`4096`) |
| `DELIVERY_MODE` | `shadow` | Use `live` only after the three-day review |
| `RADAR_DRY_RUN` | `true` | Set `false` together with live delivery |
| `RADAR_PAGES_BASE_PATH` | `/<repo>/` | Set `/` for an organization/user Pages root |

`GITHUB_TOKEN` is provided automatically with read-only contents permission.
Secrets are scoped to the single step that consumes them; dependency install and
frontend build steps never receive database, mail, model, or recipient secrets.
No command interpolates a secret into shell source. All Actions are pinned to a
full commit SHA, and scheduled workflows run only from the default branch.

Set `SEC_USER_AGENT` as a repository variable, not a secret, in the form
`AIResearchRadar/0.1 contact=<public-operations-email>`, replacing the bracketed
placeholder with the monitored address. The runtime rejects a missing identity
or the documented placeholder. SEC requests use this identity exclusively and
share a four-requests-per-second domain throttle; ordinary collectors continue
to use `RADAR_USER_AGENT`.

Production workflows pair `LLM_PROVIDER` with a fixed host, the corresponding
provider secret, and provider-specific model variables. YiCloud always starts
with intentional local `feature-hash-v1` embeddings; it never receives an
embedding request. See [the model provider runbook](../docs/model-providers.md)
for secure key rotation, the manual billable smoke test, activation, and
rollback commands.

## 4. Schedule and failure semantics

All cron expressions are UTC; product times are Asia/Shanghai:

| Workflow | UTC cron | Product time |
| --- | --- | --- |
| Collect and Alert | `17 */4 * * *` | every 4 hours at `:17` |
| Paper Sweep | `43 4 * * *` | 12:43 daily |
| Daily Digest | `17 5 * * *` | 13:17; AgentMail sends at 13:45 |
| Delivery Reconcile | `7 6 * * *` | 14:07 daily |
| Maintenance | `27 18 * * *` | 02:27 daily |

Every workflow also supports `workflow_dispatch`. Database writers share one
concurrency group. Collection continues across source groups and composes from
healthy results, then marks the workflow failed if any group failed. The Pages
workflow has only the permissions needed for Pages and runs after successful
Daily Digest or Maintenance workflows.

Start in shadow mode. Inspect generated Drafts and the public archive for three
days, then change `DELIVERY_MODE=live` and `RADAR_DRY_RUN=false`. Historical
backfills must remain shadow-only so they never emit old alerts.

## 5. Validation and operations

Run the dependency-free infrastructure checks before pushing:

```bash
python3 infra/scripts/validate-infra.py
```

If a scheduled job fails, rerun it with `workflow_dispatch`; deterministic event
revisions and delivery keys make replays safe. For a delivery timeout, run
Delivery Reconcile before attempting another send. Do not delete an `unknown`
delivery or create a new Draft by hand.

Rotate a leaked key immediately, then rerun the affected workflow. A failed
Svix signature returns `400`; a verified event that cannot be committed returns
`503` so AgentMail retries it. Use `webhook_events` and `source_health` for audit
and incident diagnosis, but never export either table.
