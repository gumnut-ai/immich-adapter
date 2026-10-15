"""Guard compiled librarian settings that CI cannot exercise before activation."""

import json
import os
import shlex
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def librarian_jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/librarian.lock.yml").read_text()
    )
    return workflow["jobs"]


def firewall_config(steps: list[dict[str, Any]]) -> dict[str, Any]:
    config_line = next(
        line.strip()
        for step in steps
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("printf") and "awf-config.json" in line
    )
    return json.loads(shlex.split(config_line)[2])


@pytest.fixture
def native_budget_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the gh-aw helper installed by the runner's setup step."""
    helper = tmp_path / "gh-aw/actions/ai_credits_context.cjs"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "exports.parseMaxAICreditsExceededFromAuditLog = "
        "() => process.env.TEST_BUDGET_EXCEEDED === 'true';\n"
    )
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("TEST_BUDGET_EXCEEDED", "false")


def test_librarian_delivery_readback() -> None:
    subprocess.run(
        ["node", "--test", ".github/librarian/verify-delivery.test.cjs"],
        cwd=ROOT,
        check=True,
    )


def test_librarian_job_allows_inference_timeout_finalization() -> None:
    job = librarian_jobs()["agent"]
    inference = next(
        step for step in job["steps"] if step.get("id") == "agentic_execution"
    )
    assert inference["timeout-minutes"] == 30
    assert job["timeout-minutes"] >= inference["timeout-minutes"] + 10


def test_librarian_disables_whole_agent_harness_retries() -> None:
    frontmatter = (ROOT / ".github/workflows/librarian.md").read_text().split("---")[1]
    source = yaml.safe_load(frontmatter)
    assert source["engine"]["harness"]["max-retries"] == 0
    for job_name in ("agent", "detection"):
        execution = next(
            step
            for step in librarian_jobs()[job_name]["steps"]
            if step.get("id") in ("agentic_execution", "detection_agentic_execution")
        )
        assert str(execution["env"]["GH_AW_HARNESS_MAX_RETRIES"]) == "0"


def test_librarian_firewall_downloads_match_source_pins() -> None:
    frontmatter = (ROOT / ".github/workflows/librarian.md").read_text().split("---")[1]
    pins = yaml.safe_load(frontmatter)["sandbox"]["agent"]["images"]
    for job_name in ("agent", "detection"):
        steps = librarian_jobs()[job_name]["steps"]
        download = next(
            step for step in steps if step.get("name") == "Download container images"
        )
        assert set(pins.values()) <= set(shlex.split(download["run"]))
        config = firewall_config(steps)
        assert config["container"]["images"] == pins
        assert "imageTag" not in config["container"]


def test_librarian_budgets_reach_native_admission_and_both_proxies() -> None:
    frontmatter = (ROOT / ".github/workflows/librarian.md").read_text().split("---")[1]
    source = yaml.safe_load(frontmatter)
    jobs = librarian_jobs()
    daily = str(source["max-daily-ai-credits"])
    assert jobs["activation"]["env"]["GH_AW_MAX_DAILY_AI_CREDITS"] == daily
    admission = next(
        step
        for step in jobs["activation"]["steps"]
        if "GH_AW_MAX_AI_CREDITS" in step.get("env", {})
    )
    assert admission["env"]["GH_AW_MAX_DAILY_AI_CREDITS"] == daily
    assert admission["env"]["GH_AW_MAX_AI_CREDITS"] == str(source["max-ai-credits"])
    limits = {
        "agent": source["max-ai-credits"],
        "detection": source["safe-outputs"]["threat-detection"]["max-ai-credits"],
    }
    for job_name, limit in limits.items():
        config = firewall_config(jobs[job_name]["steps"])
        assert config["apiProxy"]["enabled"] is True
        assert config["apiProxy"]["maxAiCredits"] == limit


def test_librarian_luna_pricing_reaches_both_proxies_and_run_metadata() -> None:
    frontmatter = (ROOT / ".github/workflows/librarian.md").read_text().split("---")[1]
    providers = yaml.safe_load(frontmatter)["models"]["providers"]
    cost = providers["openai"]["models"]["gpt-6-luna"]["cost"]
    # Official standard USD/token, not USD/million or AWF's stale Astra rates.
    assert {key: Decimal(str(value)) for key, value in cost.items()} == {
        "input": Decimal("0.0000001"),
        "output": Decimal("0.0000005"),
        "cache_read": Decimal("0.00000001"),
        "cache_write": Decimal("0.000000125"),
    }
    jobs = librarian_jobs()
    for job_name in ("agent", "detection"):
        proxy = firewall_config(jobs[job_name]["steps"])["apiProxy"]
        assert proxy["providers"] == providers
        prices = proxy["providers"]["openai"]["models"]["gpt-6-luna"]["cost"]
        usd = 18514 * Decimal(prices["input"]) + 452 * Decimal(prices["output"])
        assert usd / Decimal("0.01") == Decimal("0.20774")
        assert usd / Decimal("0.01") < Decimal(str(proxy["maxAiCredits"]))
    info = next(
        step
        for step in jobs["activation"]["steps"]
        if "GH_AW_INFO_MODEL_COSTS" in step.get("env", {})
    )
    assert json.loads(info["env"]["GH_AW_INFO_MODEL_COSTS"])["providers"] == providers


def test_librarian_report_rejects_failed_postprocessing(
    tmp_path: Path, native_budget_parser: None
) -> None:
    report = next(
        step
        for step in librarian_jobs()["agent"]["steps"]
        if step.get("name") == "Report librarian outcome"
    )
    assert report["env"]["JOB_STATUS"] == "${{ job.status }}"
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-c", report["run"]],
        env={
            **os.environ,
            "UV_SETUP": "success",
            "LINTER_SETUP": "success",
            "TRUSTED_LINTER": "success",
            "SANDBOX_CHECK": "success",
            "INFERENCE": "success",
            "AGENT_TIMEOUT": "false",
            "JOB_STATUS": "failure",
            "GITHUB_SHA": "test-revision",
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Librarian result: postprocessing-failed" in summary.read_text()


def test_librarian_report_does_not_call_failed_status_probe_clean(
    tmp_path: Path, native_budget_parser: None
) -> None:
    report = next(
        step
        for step in librarian_jobs()["agent"]["steps"]
        if step.get("name") == "Report librarian outcome"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    awf = binaries / "awf"
    awf.write_text(
        '#!/bin/bash\nwhile [[ "$1" != -- ]]; do shift; done\nshift\nexec "$@"\n'
    )
    awf.chmod(0o755)
    git = binaries / "git"
    git.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == status ]]; then exit 128; fi\n'
        'if [[ "$1" == rev-parse ]]; then echo test-revision; exit 0; fi\n'
        "exit 1\n"
    )
    git.chmod(0o755)
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-c", report["run"]],
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "UV_SETUP": "success",
            "LINTER_SETUP": "success",
            "TRUSTED_LINTER": "success",
            "SANDBOX_CHECK": "success",
            "INFERENCE": "success",
            "AGENT_TIMEOUT": "false",
            "JOB_STATUS": "success",
            "GITHUB_SHA": "test-revision",
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_WORKSPACE": str(tmp_path),
            "RUNNER_TEMP": str(tmp_path),
            "UV_CACHE_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Librarian result: validation-failed" in summary.read_text()


def test_librarian_report_rejects_successful_budget_abort(
    tmp_path: Path, native_budget_parser: None
) -> None:
    report = next(
        step
        for step in librarian_jobs()["agent"]["steps"]
        if step.get("name") == "Report librarian outcome"
    )
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-c", report["run"]],
        env={
            **os.environ,
            "TEST_BUDGET_EXCEEDED": "true",
            "UV_SETUP": "success",
            "LINTER_SETUP": "success",
            "TRUSTED_LINTER": "success",
            "SANDBOX_CHECK": "success",
            "INFERENCE": "success",
            "AGENT_TIMEOUT": "false",
            "JOB_STATUS": "success",
            "GITHUB_SHA": "test-revision",
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Librarian result: budget-interrupted" in summary.read_text()


def test_librarian_python_matches_repository_pin() -> None:
    steps = librarian_jobs()["agent"]["steps"]
    python = [
        step
        for step in steps
        if step.get("uses", "").startswith("actions/setup-python@")
    ]
    assert len(python) == 1
    assert (
        python[0]["with"]["python-version"]
        == (ROOT / ".python-version").read_text().strip()
    )


def test_librarian_publisher_uses_only_app_credentials_and_no_issues() -> None:
    jobs = librarian_jobs()
    assert "detection" in jobs
    assert "GH_AW_SAFE_OUTPUTS_STAGED" not in jobs["safe_outputs"]["env"]
    assert "needs.agent.result == 'success'" in jobs["safe_outputs"]["if"]
    assert (
        "needs.detection.outputs.detection_success == 'true'"
        in jobs["safe_outputs"]["if"]
    )
    assert "issues" not in jobs["safe_outputs"]["permissions"]
    for job_name in ("conclusion", "safe_outputs"):
        for step in jobs[job_name].get("steps", []):
            token = step.get("with", {}).get("github-token")
            if token is not None:
                assert token == "${{ steps.safe-outputs-app-token.outputs.token }}"
            for key, value in step.get("env", {}).items():
                if key.endswith(("CREATE_ISSUE", "REPORT_AS_ISSUE")):
                    assert value == "false"


def test_librarian_no_work_is_owned_by_native_processing() -> None:
    jobs = librarian_jobs()
    report = next(
        step
        for step in jobs["agent"]["steps"]
        if step.get("name") == "Report librarian outcome"
    )
    assert "outputs.jsonl" not in report["run"]
    assert "outcome=no-work" not in report["run"]
    assert jobs["conclusion"]["outputs"]["noop_message"] == (
        "${{ steps.noop.outputs.noop_message }}"
    )
    assert any(
        step.get("id") == "noop" and step.get("name") == "Process no-op messages"
        for step in jobs["conclusion"]["steps"]
    )
    assert jobs["safe_outputs"]["outputs"]["process_safe_outputs_status"] == (
        "${{ steps.process_safe_outputs.outputs.status }}"
    )


def handler_config() -> dict[str, Any]:
    handler = next(
        step
        for step in librarian_jobs()["safe_outputs"]["steps"]
        if step.get("id") == "process_safe_outputs"
    )
    return json.loads(handler["env"]["GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG"])


def test_librarian_pull_request_policy() -> None:
    handlers = handler_config()
    assert "push_to_pull_request_branch" not in handlers
    config = handlers["create_pull_request"]
    assert config["max"] == 1
    assert config["allowed_branches"] == ["librarian/*"]
    assert config["preserve_branch_name"] is True
    assert config["fallback_as_issue"] is False
    assert config["protected_files_policy"] == "blocked"
    assert config["protect_top_level_dot_folders"] is True
    assert not {"AGENTS.md", "README.md"} & set(config["protected_files"])
    assert config["allowed_files"] == [
        "README.md",
        "AGENTS.md",
        "**/AGENTS.md",
        "docs/**",
    ]
