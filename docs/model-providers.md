# Model provider operations

Radar supports two pinned OpenAI-compatible chat providers:

| `LLM_PROVIDER` | Chat base URL | GitHub Actions secret | Default embedding mode |
| --- | --- | --- | --- |
| `dashscope` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | `shared` (`text-embedding-v4`) |
| `yicloud` | `https://token-api.yicloud.com/v1` | `YICLOUD_API_KEY` | `local` (`feature-hash-v1`) |

`yicloud` means **YiCloud TokenFactory**, not the separate YiCloud MaaS
gateway. Workflows derive the host and select the corresponding secret from the
provider; operators cannot supply an arbitrary chat base URL in repository
variables. Runtime validation repeats that host check before a credentialed
request. An unsupported provider fails validation instead of falling through to
DashScope.

The backend still accepts the legacy `DASHSCOPE_*` and `QWEN_*` environment
variable names for existing local DashScope deployments. New configuration
should use the provider-neutral `LLM_*` names in `.env.example`.

## Safely enable YiCloud TokenFactory

Any key pasted into chat, logs, an issue, or a command-line argument is exposed.
Revoke it in YiCloud first, create a replacement, and never reuse the exposed
value. Store the replacement interactively so it is not placed in shell
history:

```bash
gh secret set YICLOUD_API_KEY --repo joe1chief/ai-research-radar
```

Do not set `LLM_PROVIDER=yicloud` yet. First obtain the exact chat model IDs
available to the TokenFactory account and save those non-secret identifiers:

```bash
gh variable set YICLOUD_CLASSIFIER_MODEL \
  --repo joe1chief/ai-research-radar \
  --body 'ACCOUNT_VERIFIED_MODEL_ID'

gh variable set YICLOUD_SUMMARIZER_MODEL \
  --repo joe1chief/ai-research-radar \
  --body 'ACCOUNT_VERIFIED_MODEL_ID'
```

The repository deliberately has no guessed YiCloud model fallback. A missing
model variable fails before the API call.

Run the manual smoke workflow while production still uses DashScope:

```bash
gh workflow run model-provider-smoke.yml \
  --repo joe1chief/ai-research-radar \
  --ref main \
  -f confirm_external_call=true
```

The confirmation is required because this makes real, potentially billable
requests. Optional `classifier_model` and `summarizer_model` inputs can test a
candidate without changing repository variables. JSON response mode defaults to
`true`. If TokenFactory rejects the `response_format` parameter, rerun the same
strict smoke with `-f json_response_format=false`; this omits only that protocol
field and still requires schema-valid JSON from the prompt. The workflow:

- reads `YICLOUD_API_KEY` only in the request step and never prints its value;
- calls the fixed TokenFactory `/chat/completions` endpoint for every distinct
  configured chat model;
- uses the production client's exact chat payload builder and strict JSON
  parser for both classifier and summarizer calls;
- treats `GET /models` as diagnostic only, never as the pass/fail gate;
- omits the DashScope-specific `enable_thinking` extension;
- validates the response shape and JSON body instead of using the backend's
  fail-soft fallback;
- makes no YiCloud embedding request.

If only the `false` run succeeds, persist that non-secret compatibility setting
before activating the provider:

```bash
gh variable set YICLOUD_JSON_RESPONSE_FORMAT \
  --repo joe1chief/ai-research-radar \
  --body false
```

Only after the smoke run succeeds should production be switched:

```bash
gh variable set LLM_PROVIDER \
  --repo joe1chief/ai-research-radar \
  --body yicloud
```

The next Collect, Paper Sweep, or Daily Digest run will select all of these as
one configuration: `YICLOUD_API_KEY`, the fixed TokenFactory host, both
YiCloud model variables, JSON response mode, and intentional local embeddings.

To roll back, first confirm `DASHSCOPE_API_KEY` still exists, then run:

```bash
gh variable set LLM_PROVIDER \
  --repo joe1chief/ai-research-radar \
  --body dashscope
```

## Embedding policy

YiCloud chat compatibility does not establish that TokenFactory provides the
project's required 1,024-dimensional embedding contract. Its production mode is
therefore intentionally `local`: Radar uses deterministic `feature-hash-v1`
vectors and sends no embedding text or credential to YiCloud.

DashScope retains `shared` mode, where chat and embedding use the same fixed
provider endpoint and credential. The backend also supports a future `remote`
mode with `LLM_EMBEDDING_API_KEY`, `LLM_EMBEDDING_BASE_URL`, and
`LLM_EMBEDDING_MODEL`, but production workflows do not enable that mode. A
separate provider review and smoke test is required before doing so.

All modes fix `LLM_EMBEDDING_DIMENSIONS=1024` to match the database schema.
