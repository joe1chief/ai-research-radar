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


def fail(message: str, failures: list[str]) -> None:
    failures.append(message)


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
