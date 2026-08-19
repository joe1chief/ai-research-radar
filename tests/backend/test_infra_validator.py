from __future__ import annotations

import runpy
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
VALIDATOR = runpy.run_path(str(ROOT / "infra/scripts/validate-infra.py"))
STEP_ENV_CONTRACTS = VALIDATOR["STEP_ENV_CONTRACTS"]
validate_step_env_contracts = VALIDATOR["validate_step_env_contracts"]
validate_daily_digest_delivery_only = VALIDATOR[
    "validate_daily_digest_delivery_only"
]


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


def _daily_digest_workflow() -> str:
    return (ROOT / ".github/workflows/daily-digest.yml").read_text(encoding="utf-8")


def test_daily_digest_delivery_only_contract_is_valid():
    failures: list[str] = []

    validate_daily_digest_delivery_only(_daily_digest_workflow(), failures)

    assert failures == []


def test_daily_digest_delivery_only_must_default_off():
    workflow = _daily_digest_workflow().replace(
        "        default: false\n", "        default: true\n", 1
    )
    failures: list[str] = []

    validate_daily_digest_delivery_only(workflow, failures)

    assert any("default: false" in failure for failure in failures)


def test_daily_digest_delivery_only_must_be_boolean():
    workflow = _daily_digest_workflow().replace(
        "        type: boolean\n", "        type: string\n", 1
    )
    failures: list[str] = []

    validate_daily_digest_delivery_only(workflow, failures)

    assert any("type: boolean" in failure for failure in failures)


def test_daily_digest_delivery_only_requires_manual_event_guard():
    workflow = _daily_digest_workflow().replace(
        "        if: github.event_name != 'workflow_dispatch' || "
        "inputs.delivery_only != true\n",
        "        if: inputs.delivery_only != true\n",
        1,
    )
    failures: list[str] = []

    validate_daily_digest_delivery_only(workflow, failures)

    assert any("Final incremental collection" in failure for failure in failures)


def test_every_collection_phase_step_requires_the_exact_delivery_only_guard():
    workflow = _daily_digest_workflow()

    for step_name, condition in VALIDATOR["DELIVERY_ONLY_SKIP_CONDITIONS"].items():
        modified = workflow.replace(
            f"      - name: {step_name}\n        if: {condition}\n",
            f"      - name: {step_name}\n",
            1,
        )
        failures: list[str] = []

        validate_daily_digest_delivery_only(modified, failures)

        assert any(step_name in failure for failure in failures), step_name


def test_daily_digest_delivery_steps_cannot_be_skipped_in_delivery_only_mode():
    guard = VALIDATOR["DELIVERY_ONLY_GUARD"]
    workflow = _daily_digest_workflow().replace(
        "      - name: Compose today's digest\n",
        "      - name: Compose today's digest\n"
        f"        if: {guard}\n",
        1,
    )
    failures: list[str] = []

    validate_daily_digest_delivery_only(workflow, failures)

    assert any("Compose today's digest" in failure for failure in failures)


def test_delivery_only_cannot_override_shadow_safety_defaults():
    workflow = _daily_digest_workflow().replace(
        "  DELIVERY_MODE: ${{ vars.DELIVERY_MODE || 'shadow' }}\n",
        "  DELIVERY_MODE: live\n",
        1,
    )
    failures: list[str] = []

    validate_daily_digest_delivery_only(workflow, failures)

    assert any("DELIVERY_MODE" in failure for failure in failures)


def test_delivery_only_review_requires_all_agentmail_secrets():
    workflow = _daily_digest_workflow()

    for mapping in VALIDATOR["DELIVERY_ONLY_REVIEW_ENV"]:
        modified = workflow.replace(f"          {mapping}\n", "", 1)
        failures: list[str] = []

        validate_daily_digest_delivery_only(modified, failures)

        assert any(mapping in failure for failure in failures), mapping


def test_delivery_only_safety_step_must_precede_delivery_commands():
    workflow = _daily_digest_workflow()
    safety = VALIDATOR["workflow_named_step"](
        workflow, "Require shadow mode for delivery-only validation"
    )
    assert safety is not None
    modified = workflow.replace(safety, "", 1).replace(
        "      - name: Create or update the scheduled AgentMail draft\n",
        safety + "      - name: Create or update the scheduled AgentMail draft\n",
        1,
    )
    failures: list[str] = []

    validate_daily_digest_delivery_only(modified, failures)

    assert any("must run before" in failure for failure in failures)


def test_delivery_only_steps_are_bound_to_their_commands():
    workflow = _daily_digest_workflow()

    for step_name, command in VALIDATOR["DELIVERY_ONLY_RUN_COMMANDS"].items():
        block = VALIDATOR["workflow_named_step"](workflow, step_name)
        assert block is not None
        modified = workflow.replace(block, block.replace(command, "echo no-op", 1), 1)
        failures: list[str] = []

        validate_daily_digest_delivery_only(modified, failures)

        assert any(step_name in failure and command in failure for failure in failures)


def _delivery_review_env() -> dict[str, str]:
    return {
        "RADAR_DATABASE_URL": "postgresql://runtime@database.invalid/radar",
        "AGENTMAIL_API_KEY": "test-only",
        "AGENTMAIL_INBOX_ID": "test-inbox",
        "DIGEST_RECIPIENT": "recipient@radar.invalid",
        "DELIVERY_MODE": "shadow",
        "RADAR_DRY_RUN": "true",
    }


def test_delivery_review_runtime_validation_is_fail_closed():
    validator = ROOT / "infra/scripts/validate-runtime-env.sh"
    valid_env = _delivery_review_env()
    valid = subprocess.run(
        ["/bin/bash", str(validator), "delivery-review"],
        env=valid_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0

    for missing_name in (
        "AGENTMAIL_API_KEY",
        "AGENTMAIL_INBOX_ID",
        "DIGEST_RECIPIENT",
    ):
        missing_env = {**valid_env}
        missing_env.pop(missing_name)
        missing = subprocess.run(
            ["/bin/bash", str(validator), "delivery-review"],
            env=missing_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert missing.returncode == 1
        assert missing_name in missing.stderr

    unsafe_env = {**valid_env, "RADAR_DRY_RUN": "false"}
    unsafe = subprocess.run(
        ["/bin/bash", str(validator), "delivery-review"],
        env=unsafe_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unsafe.returncode == 1
    assert "RADAR_DRY_RUN" in unsafe.stderr
