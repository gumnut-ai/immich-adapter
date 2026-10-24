"""Guard the dependency overlay's privilege and delivery contract in normal CI."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_dependency_overlay_delivery_and_permissions() -> None:
    text = (ROOT / ".github/workflows/dependency-maintenance.md").read_text()
    source = yaml.safe_load(text.split("---")[1])
    compiled = yaml.safe_load(
        (ROOT / ".github/workflows/dependency-maintenance.lock.yml").read_text()
    )
    # PyYAML's YAML 1.1 resolver treats the Actions `on` key as True.
    assert source[True]["schedule"] == [{"cron": "0 15 * * *"}]
    assert "vars." not in source["if"]
    assert set(source["permissions"].values()) == {"read"}
    for scope in ("checks", "statuses"):
        assert source["permissions"][scope] == "read"
        assert compiled["jobs"]["agent"]["permissions"][scope] == "read"
    assert source["tools"]["github"]["allowed-repos"] == "all"
    assert source["tools"]["github"]["read-only"] is True
    assert source["engine"]["harness"]["max-retries"] == 0
    outputs = source["safe-outputs"]
    assert outputs["environment"] == "gumbot-publisher"
    assert len(outputs["steps"]) == 1
    delivery = outputs["steps"][0]["uses"]
    assert delivery.startswith("gumnut-ai/automation/actions/dependency-delivery@")
    assert len(delivery.split("@")[1]) == 40
    assert (
        "DEPENDENCY_MAINTENANCE_TRUSTED"
        in outputs["scripts"]["handoff-dependency-pr"]["script"]
    )
    for tool in ("create-pull-request", "push-to-pull-request-branch"):
        config = outputs[tool]
        assert config["max"] == 1
        assert config["protected-files"]["policy"] == "blocked"
        assert not any(
            path.startswith((".github", ".agents"))
            for path in config["protected-files"]["exclude"]
        )
    jobs = compiled["jobs"]
    assert {name for name, job in jobs.items() if "environment" in job} == {
        "safe_outputs",
        "conclusion",
    }
    assert jobs["pre_activation"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }
    assert any(step.get("uses") == delivery for step in jobs["safe_outputs"]["steps"])
    assert not any(step.get("uses") == delivery for step in jobs["agent"]["steps"])
    assert "secrets.GUMBOT_PRIVATE_KEY" not in str(jobs["agent"])
    assert "services" not in source
    assert "dependency-snapshot" not in text


def test_librarian_daily_schedule_matches_policy_and_lockfile() -> None:
    source = yaml.safe_load(
        (ROOT / ".github/workflows/librarian.md").read_text().split("---")[1]
    )
    policy = yaml.safe_load(
        (ROOT / ".agents/daemons/librarian/DAEMON.md").read_text().split("---")[1]
    )
    compiled = yaml.safe_load(
        (ROOT / ".github/workflows/librarian.lock.yml").read_text()
    )
    assert source[True]["schedule"] == policy["schedule"] == "0 12 * * *"
    assert compiled[True]["schedule"] == [{"cron": "0 12 * * *"}]
