# Operations runbook

## Required production setup

1. Create one Supabase project and apply migrations in `supabase/migrations`.
2. Create one AgentMail inbox with an inbox-scoped API key.
3. Deploy the AgentMail webhook Edge Function and register an inbox-scoped
   webhook for sent, delivered, bounced, rejected, and complained events.
4. Add the secrets and variables documented in `.env.example` to GitHub.
5. Run the workflows manually in shadow mode before enabling live delivery.

Never paste secret values into issues, logs, the public archive, or repository
variables. Recipient addresses are secrets, not variables.

## Shadow-to-live sequence

1. Run a 14-day backfill. Confirm that no historical alert was sent.
2. Keep `DELIVERY_MODE=shadow` for three daily runs. Review the drafts, evidence
   labels, duplicates, and public projection.
3. Set `DELIVERY_MODE=live`, manually run `daily-digest`, and verify the webhook
   reaches `delivered`.

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

A source failure is isolated. After three consecutive failures, record a
degraded source-health state. The nightly job prints the exact failed-source
list and exits non-zero so GitHub Actions raises the operational notification;
the application does not claim a separate per-source paging service.

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
