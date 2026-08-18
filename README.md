# AI Research Radar

AI Research Radar is a daily, evidence-layered monitor for frontier AI research,
agent systems, self-evolving training, safety/governance, and material industry
capital events. It discovers and versions public sources, enriches relevant
records with Qwen and optional alphaXiv MCP reading, sends idempotent AgentMail
Drafts, and publishes a sanitized searchable archive to GitHub Pages.

The default configuration is safe to run without credentials: SQLite, rules,
deterministic embeddings, local shadow delivery, and a clearly labeled public
demo dataset all work offline. Production services activate only when their
environment variables are present.

## What is implemented

- paginated arXiv Atom discovery, OAI-PMH version reconciliation, OpenReview,
  ACL Anthology, and PMLR proceedings discovery;
- official RSS/Atom and XML sitemaps, conservative HTML listings, GitHub
  Releases, Hugging Face model-card updates, SEC filing bodies, HKEX/IR pages,
  the SSE bulletin API, and CNInfo statutory announcements;
- five technical query tracks plus material capital-event taxonomy;
- 80 configured source adapters (79 enabled; the blocked xAI HTML endpoint has
  an official Hugging Face fallback) spanning the requested core/extended labs,
  release ecosystems, five media-discovery surfaces, and public issuers;
- exact identity, content revisions, 14-day semantic clustering, corroborating
  evidence, concrete field-level change summaries, and material/minor update
  states;
- Qwen Flash JSON classification, capped Qwen Plus editorial cards, and
  `text-embedding-v4` vectors with deterministic offline fallback;
- optional alphaXiv Streamable-HTTP MCP `get_paper_content` enrichment for the
  five highest-scoring papers, isolated from the canonical arXiv path;
- evidence-gated scoring and alert selection;
- AgentMail deterministic Draft outbox, shadow review, scheduled 13:45 digest,
  fail-closed ambiguous-send handling, and signed delivery webhooks;
- revision-ledger digest selection that carries post-13:17 discoveries into
  the next run, plus embedding-outage withholding and recovery re-clustering;
- fail-closed backfill replay: HTTP validators are cleared, the arXiv page
  budget scales with the window, and any failed/degraded/truncated source makes
  the command exit non-zero after committing usable archive data;
- Supabase migration, RLS, compressed private HTML snapshots with 14-day
  deletion, sanitized static export, pgvector candidate search, and GitHub Actions schedules;
- responsive React archive with Top 3, five sections, Chinese/English search,
  filters, shareable URLs, timelines, and runtime private-field rejection.

## Local quick start

Requirements are Python 3.12 and Node.js 22+ with pnpm. `uv` is recommended.

```bash
cp .env.example .env
uv sync --locked
uv run radar init-db

# A real, read-only smoke run; no API key is required.
uv run radar collect --group papers
uv run radar enrich --limit 100 --summary-limit 0
uv run radar compose --date "$(TZ=Asia/Shanghai date +%F)" --kind digest
uv run radar deliver                  # shadow by default; does not send
uv run radar export-web --output web/public/data/latest.json

cd web
pnpm install --frozen-lockfile
pnpm dev
```

Without `uv`, create a Python 3.12 virtual environment and run
`python -m pip install -e .`; the same `radar` commands then apply.

The operational CLI is:

```text
radar collect --group papers|tech|capital|standards
radar enrich [--limit N] [--summary-limit N]
radar compose --date YYYY-MM-DD --kind digest|alert
radar deliver
radar reconcile
radar backfill --days 14
radar export-web [--output PATH]
radar maintenance
radar evaluate-topics --dataset labels.jsonl
```

## Configuration and trust

The editable product contract lives in:

- `configs/topics.yml` — hard terms, weak-only terms, co-occurrence gates, and
  cross-tags;
- `configs/sources.yml` — allow-listed URLs, parser, evidence type, cadence,
  and cursor strategy;
- `configs/issuers.yml` — issuer aliases, markets, tickers, and CIKs.

Fetched content is untrusted. It cannot select tools, recipients, source URLs,
or delivery thresholds. Company announcements are `company_claim` unless the
event is independently corroborated; media-only capital reports remain private
and cannot alert. Paper PDFs and paywalled bodies are not stored.

See [architecture](docs/architecture.md), [source policy](docs/source-policy.md),
the [topic evaluation protocol](docs/topic-evaluation.md), the
[model provider runbook](docs/model-providers.md), and the [operations
runbook](docs/runbook.md) for the full contract.

## Production setup

Apply `supabase/migrations/202607120001_initial_radar.sql`, deploy the
`agentmail-webhook` Edge Function, and configure GitHub Pages to use GitHub
Actions. The exact commands and permissions are in [infra/README.md](infra/README.md).

Required GitHub secrets:

- `SUPABASE_DB_URL`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`;
- `DASHSCOPE_API_KEY` for the default provider, or `YICLOUD_API_KEY` after its
  manual smoke test succeeds;
- optional `ALPHAXIV_ACCESS_TOKEN` and `OPENREVIEW_ACCESS_TOKEN`;
- `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX_ID`, `DIGEST_RECIPIENT`.

`LLM_PROVIDER` defaults to `dashscope`. YiCloud TokenFactory requires
account-verified `YICLOUD_CLASSIFIER_MODEL` and `YICLOUD_SUMMARIZER_MODEL`
repository variables; it uses the fixed TokenFactory host and intentional
local embeddings. Do not switch the provider until the manual
`model-provider-smoke.yml` workflow passes. Exact rotation and rollback commands
are in the [model provider runbook](docs/model-providers.md).

Keep repository variables `DELIVERY_MODE=shadow` and `RADAR_DRY_RUN=true` for
the 14-day backfill and three review days. After verifying generated Drafts and
the public archive, switch both variables together to `live` and `false`.

Scheduled product times are Asia/Shanghai: four-hour collection at `:17`, paper
sweep at 12:43, digest composition at 13:17, AgentMail send at 13:45, delivery
reconciliation at 14:07, and maintenance at 02:27. Cron expressions in GitHub
Actions are the corresponding UTC values.

## Verification

```bash
uv run pytest -q
python3 infra/scripts/validate-infra.py
bash -n infra/scripts/validate-runtime-env.sh

cd web
pnpm test
VITE_BASE_PATH=/ai-research-radar/ pnpm build
```

Supabase Edge Function tests are configured in its `deno.json` and can be run
with `deno task check && deno task test` when Deno is installed.

The production topic gate deliberately does not ship a synthetic green result.
Prepare at least 100 independently reviewed JSONL rows with `id`, `title`,
optional `text`/`event_type`, `expected_top1`, and non-empty `reviewed_by`, then
run `radar evaluate-topics --dataset labels.jsonl` with the selected provider's
neutral `LLM_*` variables set. Legacy `DASHSCOPE_*` and `QWEN_*` names remain
accepted for DashScope deployments. The command evaluates the actual
rules-plus-model path and exits non-zero
unless the corpus has at least 100 unique reviewed rows and Top-1 precision is
at least 85%. `--rules-only` is an offline diagnostic and is explicitly labeled
as such in its JSON result.
