#!/usr/bin/env python3
"""Dependency-free structural checks for the deployment contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

EXPECTED_CRONS = {
    "collect-alert.yml": "17 */4 * * *",
    "paper-sweep.yml": "43 4 * * *",
    "daily-digest.yml": "17 5 * * *",
    "delivery-reconcile.yml": "7 6 * * *",
    "maintenance.yml": "27 18 * * *",
}

PRODUCTION_WORKFLOWS = {*EXPECTED_CRONS, "pages.yml"}
MODEL_WORKFLOWS = (
    "collect-alert.yml",
    "paper-sweep.yml",
    "daily-digest.yml",
)
MODEL_ENV_KEYS = (
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_CLASSIFIER_MODEL",
    "LLM_SUMMARIZER_MODEL",
    "LLM_JSON_RESPONSE_FORMAT",
    "LLM_MAX_TOKENS",
    "LLM_EMBEDDING_MODE",
    "LLM_EMBEDDING_MODEL",
    "LLM_EMBEDDING_DIMENSIONS",
)

REQUIRED_TABLES = {
    "sources",
    "source_cursors",
    "items",
    "item_versions",
    "events",
    "event_revisions",
    "evidence",
    "deliveries",
    "webhook_events",
    "source_health",
    "usage_ledger",
}

STEP_ENV_CONTRACTS = {
    "infra/scripts/validate-runtime-env.sh delivery": (
        "RADAR_DATABASE_URL: ${{ secrets.SUPABASE_DB_URL }}",
        "AGENTMAIL_API_KEY: ${{ secrets.AGENTMAIL_API_KEY }}",
        "AGENTMAIL_INBOX_ID: ${{ secrets.AGENTMAIL_INBOX_ID }}",
        "DIGEST_RECIPIENT: ${{ secrets.DIGEST_RECIPIENT }}",
    ),
    "uv run radar compose": (
        "RADAR_DATABASE_URL: ${{ secrets.SUPABASE_DB_URL }}",
        "DIGEST_RECIPIENT: ${{ secrets.DIGEST_RECIPIENT }}",
    ),
    "uv run radar deliver": (
        "RADAR_DATABASE_URL: ${{ secrets.SUPABASE_DB_URL }}",
        "AGENTMAIL_API_KEY: ${{ secrets.AGENTMAIL_API_KEY }}",
        "AGENTMAIL_INBOX_ID: ${{ secrets.AGENTMAIL_INBOX_ID }}",
        "DIGEST_RECIPIENT: ${{ secrets.DIGEST_RECIPIENT }}",
    ),
    "uv run radar reconcile": (
        "RADAR_DATABASE_URL: ${{ secrets.SUPABASE_DB_URL }}",
        "AGENTMAIL_API_KEY: ${{ secrets.AGENTMAIL_API_KEY }}",
        "AGENTMAIL_INBOX_ID: ${{ secrets.AGENTMAIL_INBOX_ID }}",
        "DIGEST_RECIPIENT: ${{ secrets.DIGEST_RECIPIENT }}",
    ),
}


def fail(message: str, failures: list[str]) -> None:
    failures.append(message)


def workflow_step_blocks(text: str) -> list[str]:
    """Return top-level GitHub Actions step blocks without a YAML dependency."""

    starts = [match.start() for match in re.finditer(r"(?m)^      - ", text)]
    return [
        text[start : starts[index + 1] if index + 1 < len(starts) else len(text)]
        for index, start in enumerate(starts)
    ]


def workflow_step_env(block: str) -> set[str]:
    """Extract exact mappings from a step-local env block."""

    mappings: set[str] = set()
    in_env = False
    for line in block.splitlines():
        if line == "        env:":
            in_env = True
            continue
        if not in_env:
            continue
        if re.match(r"^ {10}\S", line):
            mappings.add(line.strip())
            continue
        if line.startswith("          "):
            # Nested/multiline env values are not top-level mappings.
            continue
        if line.strip():
            break
    return mappings


def validate_step_env_contracts(filename: str, text: str, failures: list[str]) -> None:
    for block in workflow_step_blocks(text):
        for command, required_mappings in STEP_ENV_CONTRACTS.items():
            if command not in block:
                continue
            step_name = re.search(r"(?m)^      - name:\s*(.+)$", block)
            label = step_name.group(1).strip() if step_name else command
            step_env = workflow_step_env(block)
            for mapping in required_mappings:
                if mapping not in step_env:
                    fail(
                        f"{filename}: step {label!r} is missing scoped env mapping {mapping}",
                        failures,
                    )


def main() -> int:
    failures: list[str] = []
    workflows = ROOT / ".github" / "workflows"

    for filename, cron in EXPECTED_CRONS.items():
        path = workflows / filename
        if not path.is_file():
            fail(f"missing workflow: {filename}", failures)
            continue
        text = path.read_text(encoding="utf-8")
        if f'cron: "{cron}"' not in text:
            fail(f"{filename}: expected UTC cron {cron}", failures)
        for token in ("workflow_dispatch:", "concurrency:", "cancel-in-progress:"):
            if token not in text:
                fail(f"{filename}: missing {token}", failures)

    model_mappings: dict[str, dict[str, str]] = {}
    for filename in MODEL_WORKFLOWS:
        path = workflows / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        header = text.split("\njobs:", 1)[0]
        if not re.search(
            r"(?m)^  SEC_USER_AGENT:\s*\$\{\{ vars\.SEC_USER_AGENT \}\}$",
            header,
        ):
            fail(
                f"{filename}: missing dedicated SEC_USER_AGENT repository-variable mapping",
                failures,
            )
        mapping: dict[str, str] = {}
        for key in MODEL_ENV_KEYS:
            match = re.search(rf"(?m)^  {re.escape(key)}:\s*(.+)$", header)
            if match is None:
                fail(f"{filename}: missing provider mapping for {key}", failures)
            else:
                mapping[key] = match.group(1).strip()
        model_mappings[filename] = mapping

        for token in (
            "https://token-api.yicloud.com/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "vars.YICLOUD_CLASSIFIER_MODEL",
            "vars.YICLOUD_SUMMARIZER_MODEL",
            "required-yicloud-classifier-model",
            "required-yicloud-summarizer-model",
            "if: env.LLM_PROVIDER != 'dashscope' && env.LLM_PROVIDER != 'yicloud'",
            "if: env.LLM_PROVIDER == 'dashscope'",
            "if: env.LLM_PROVIDER == 'yicloud'",
            "LLM_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}",
            "LLM_API_KEY: ${{ secrets.YICLOUD_API_KEY }}",
            "infra/scripts/validate-runtime-env.sh",
        ):
            if token not in text:
                fail(f"{filename}: missing safe provider-routing token {token}", failures)

        if re.search(
            r"secrets\.YICLOUD_API_KEY[^\n]*secrets\.DASHSCOPE_API_KEY|"
            r"secrets\.DASHSCOPE_API_KEY[^\n]*secrets\.YICLOUD_API_KEY",
            text,
        ):
            fail(
                f"{filename}: provider secrets must never share a fallback expression",
                failures,
            )
        for secret in ("DASHSCOPE_API_KEY", "YICLOUD_API_KEY"):
            if text.count(f"LLM_API_KEY: ${{{{ secrets.{secret} }}}}") != 2:
                fail(
                    f"{filename}: {secret} must be scoped to its validation and model-call steps",
                    failures,
                )

    if model_mappings:
        reference_name = MODEL_WORKFLOWS[0]
        reference = model_mappings.get(reference_name)
        if reference is not None:
            for filename, mapping in model_mappings.items():
                if mapping != reference:
                    fail(
                        f"{filename}: provider mapping drifted from {reference_name}",
                        failures,
                    )

    smoke = workflows / "model-provider-smoke.yml"
    if not smoke.is_file():
        fail("missing manual model-provider smoke workflow", failures)
    else:
        smoke_text = smoke.read_text(encoding="utf-8")
        smoke_header = smoke_text.split("\njobs:", 1)[0]
        for token in (
            "workflow_dispatch:",
            "confirm_external_call:",
            "github.event.repository.default_branch",
            "LLM_PROVIDER: yicloud",
            "LLM_BASE_URL: https://token-api.yicloud.com/v1",
            "LLM_EMBEDDING_MODE: local",
            "json_response_format:",
            "LLM_API_KEY: ${{ secrets.YICLOUD_API_KEY }}",
            "infra/scripts/validate-runtime-env.sh model",
            "uv run radar model-smoke",
        ):
            if token not in smoke_text:
                fail(f"model-provider-smoke.yml: missing {token}", failures)
        if "schedule:" in smoke_header:
            fail("model-provider-smoke.yml: smoke must remain manual-only", failures)
        if "secrets." in smoke_header:
            fail("model-provider-smoke.yml: secret must be step-scoped", failures)
        if "DASHSCOPE_API_KEY" in smoke_text:
            fail(
                "model-provider-smoke.yml: YiCloud smoke must not receive DashScope credentials",
                failures,
            )
        if "urllib" in smoke_text or "/chat/completions" in smoke_text:
            fail(
                "model-provider-smoke.yml: smoke must use the production client "
                "via radar model-smoke",
                failures,
            )

    runtime_validator = ROOT / "infra" / "scripts" / "validate-runtime-env.sh"
    if not runtime_validator.is_file():
        fail("missing runtime environment validator", failures)
    else:
        runtime_text = runtime_validator.read_text(encoding="utf-8")
        for token in (
            'DASHSCOPE_HOST="https://dashscope.aliyuncs.com/compatible-mode/v1"',
            'YICLOUD_HOST="https://token-api.yicloud.com/v1"',
            "LLM_PROVIDER must be dashscope or yicloud",
            "LLM_EMBEDDING_MODE must be local for yicloud",
            "validate_boolean LLM_JSON_RESPONSE_FORMAT",
            "DASHSCOPE_API_KEY must be unset when LLM_PROVIDER=yicloud",
            "validate_sec_identity",
            "require_env SEC_USER_AGENT",
            "model)",
        ):
            if token not in runtime_text:
                fail(f"validate-runtime-env.sh: missing {token}", failures)

    pages = workflows / "pages.yml"
    if not pages.is_file():
        fail("missing Pages deployment workflow", failures)
    else:
        pages_text = pages.read_text(encoding="utf-8")
        for token in (
            "actions/upload-pages-artifact@",
            "actions/deploy-pages@",
            "pages: write",
            "id-token: write",
            "radar export-web",
            "pnpm build",
        ):
            if token not in pages_text:
                fail(f"pages.yml: missing {token}", failures)

    for path in workflows.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        validate_step_env_contracts(path.name, text, failures)
        if path.name in PRODUCTION_WORKFLOWS and "github.event.repository.default_branch" not in text:
            fail(f"{path.name}: production job is not restricted to the default branch", failures)
        workflow_header = text.split("\njobs:", 1)[0]
        if "secrets." in workflow_header:
            fail(f"{path.name}: secrets must be scoped to the consuming step", failures)
        # Secrets belong in job/workflow env, not directly in shell source where
        # quoting and accidental expansion are harder to reason about.
        if re.search(r"run:\s*[^\n]*\$\{\{\s*secrets\.", text):
            fail(f"{path.name}: secret expression embedded directly in run", failures)
        for action, reference in re.findall(r"uses:\s*([^@\s]+)@([^\s#]+)", text):
            if not re.fullmatch(r"[a-f0-9]{40}", reference):
                fail(
                    f"{path.name}: {action} must be pinned to a full commit SHA",
                    failures,
                )

    migration = ROOT / "supabase" / "migrations" / "202607120001_initial_radar.sql"
    if not migration.is_file():
        fail("missing initial Supabase migration", failures)
    else:
        sql = migration.read_text(encoding="utf-8").lower()
        for table in REQUIRED_TABLES:
            if f"create table public.{table}" not in sql:
                fail(f"migration: missing table {table}", failures)
            if f"alter table public.{table} enable row level security" not in sql:
                fail(f"migration: RLS not enabled for {table}", failures)
        if re.search(r"grant\s+select[\s\S]{0,200}\bto\s+anon\b", sql):
            fail("migration: must not grant operational/public view reads to anon", failures)
        if "vector(1024)" not in sql or "float4[]" not in sql:
            fail("migration: missing native-vector plus portable embedding strategy", failures)

    config = ROOT / "supabase" / "config.toml"
    if not config.is_file() or "verify_jwt = false" not in config.read_text(encoding="utf-8"):
        fail("AgentMail webhook must disable JWT gate and verify Svix in-handler", failures)

    ci = workflows / "ci.yml"
    if not ci.is_file():
        fail("missing CI workflow", failures)
    else:
        ci_text = ci.read_text(encoding="utf-8")
        for token in ("pytest -q", "pnpm test", "pnpm build", "deno task check", "deno task test"):
            if token not in ci_text:
                fail(f"ci.yml: missing {token}", failures)

    if failures:
        for message in failures:
            print(f"ERROR: {message}", file=sys.stderr)
        return 1

    print("Infrastructure contract checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
