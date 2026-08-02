from pathlib import Path

import yaml


def test_compose_isolates_collector_and_application_secrets():
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {
        "monitor", "relationship-worker", "member-enrichment-worker", "dashboard"
    }
    collector_environment = services["relationship-worker"]["environment"]
    assert "IG_COLLECTOR_USERNAME" in collector_environment
    assert "TELEGRAM_BOT_TOKEN" not in collector_environment
    assert "IG_COLLECTOR_USERNAME" not in services["monitor"]["environment"]
    assert "IG_COLLECTOR_USERNAME" not in services["dashboard"]["environment"]
    assert "IG_COLLECTOR_USERNAME" not in services["member-enrichment-worker"]["environment"]
    assert any(
        "collector-secrets" in mount
        for mount in services["relationship-worker"]["volumes"]
    )
    assert all(
        not any("collector-secrets" in mount for mount in services[name].get("volumes", []))
        for name in ("monitor", "dashboard", "member-enrichment-worker")
    )
