from __future__ import annotations

import runpy
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
VALIDATOR = runpy.run_path(str(ROOT / "infra/scripts/validate-infra.py"))
STEP_ENV_CONTRACTS = VALIDATOR["STEP_ENV_CONTRACTS"]
validate_step_env_contracts = VALIDATOR["validate_step_env_contracts"]


def _delivery_mappings() -> str:
    return "\n".join(
        f"          {mapping}"
        for mapping in STEP_ENV_CONTRACTS["uv run radar deliver"]
    )


def test_unnamed_delivery_step_cannot_inherit_previous_step_env():
    workflow = (
        "      - name: Setup\n"
        "        env:\n"
        f"{_delivery_mappings()}\n"
        "        run: echo setup\n"
        "      - run: uv run radar deliver\n"
    )
    failures: list[str] = []

    validate_step_env_contracts("synthetic.yml", workflow, failures)

    assert any(
        "uv run radar deliver" in failure and "RADAR_DATABASE_URL" in failure
        for failure in failures
    )


def test_delivery_mappings_in_run_script_do_not_count_as_step_env():
    workflow = (
        "      - run: |\n"
        "          uv run radar deliver\n"
        f"{_delivery_mappings()}\n"
    )
    failures: list[str] = []

    validate_step_env_contracts("synthetic.yml", workflow, failures)

    assert any("RADAR_DATABASE_URL" in failure for failure in failures)


def test_delivery_mappings_in_multiline_env_value_do_not_count():
    nested_mappings = "\n".join(
        f"            {mapping}"
        for mapping in STEP_ENV_CONTRACTS["uv run radar deliver"]
    )
    workflow = (
        "      - name: Disguised delivery env\n"
        "        env:\n"
        "          PLACEHOLDER: |\n"
        f"{nested_mappings}\n"
        "        run: uv run radar deliver\n"
    )
    failures: list[str] = []

    validate_step_env_contracts("synthetic.yml", workflow, failures)

    assert any("RADAR_DATABASE_URL" in failure for failure in failures)


def _collect_runtime_env(*, sec_user_agent: str | None = None) -> dict[str, str]:
    env = {
        "RADAR_DATABASE_URL": "postgresql://runtime@database.invalid/radar",
        "RADAR_USER_AGENT": "AIResearchRadar/test",
        "SUPABASE_URL": "https://project.supabase.invalid",
        "SUPABASE_SECRET_KEY": "test-only",
        "LLM_API_KEY": "test-only",
    }
    if sec_user_agent is not None:
        env["SEC_USER_AGENT"] = sec_user_agent
    return env


def test_collect_runtime_requires_non_placeholder_sec_identity():
    validator = ROOT / "infra/scripts/validate-runtime-env.sh"

    missing = subprocess.run(
        ["/bin/bash", str(validator), "collect"],
        env=_collect_runtime_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    placeholder = subprocess.run(
        ["/bin/bash", str(validator), "collect"],
        env=_collect_runtime_env(
            sec_user_agent="AIResearchRadar/0.1 contact=you@example.com"
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    valid = subprocess.run(
        ["/bin/bash", str(validator), "collect"],
        env=_collect_runtime_env(
            sec_user_agent="AIResearchRadar/0.1 contact=ops@radar.invalid"
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert missing.returncode == 1
    assert "SEC_USER_AGENT" in missing.stderr
    assert placeholder.returncode == 1
    assert "placeholder" in placeholder.stderr
    assert valid.returncode == 0
