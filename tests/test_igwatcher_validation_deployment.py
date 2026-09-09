"""The download trial cannot inherit production storage or credentials."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_validation_is_standalone_bounded_and_credential_free():
    topology = yaml.safe_load((ROOT / "compose.igwatcher-validation.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load((ROOT / "igwatcher-validation.example.yaml").read_text(encoding="utf-8"))
    assert topology["name"] == "ig-monitor-igwatcher-validation"
    assert set(topology["services"]) == {"validate", "dashboard"}
    assert topology["services"]["dashboard"]["ports"] == ["127.0.0.1:8890:8888"]
    for service in topology["services"].values():
        assert service["image"] == "ig-anonymous-monitor:igwatcher-validation"
        assert service["read_only"] is True
        assert service["tmpfs"] == ["/tmp"]
        assert not set(service) & {"env_file", "secrets", "privileged", "restart", "network_mode"}
        assert service["volumes"] == [
            "./igwatcher-validation.example.yaml:/validation-config/settings.yaml:ro",
            "igwatcher-validation-state:/validation",
        ]
    command = topology["services"]["validate"]["command"]
    assert command[:4] == ["timeout", "--signal=TERM", "--kill-after=5s", "600s"]
    assert config["browser"]["anonymous_source"] == "igwatcher"
    assert config["browser"]["retry_count"] == 0
    assert config["schedule"]["media_download_enabled"] is True
    assert config["schedule"]["media_limit_per_account"] == 8
    assert len(config["accounts"]) == 1
    assert config["accounts"][0]["url"] == "https://www.instagram.com/nasa/"
    for feature in ("telegram", "heartbeat", "apify", "instagram_enrichment", "instagram_posts"):
        assert config[feature]["enabled"] is False
    assert all(path.startswith("/validation/") for path in config["paths"].values())
