"""The download trial cannot inherit production storage or credentials."""
from pathlib import Path
import errno

import pytest
import yaml

from ig_monitor.config import load_config
from ig_monitor.db import Database
from ig_monitor.monitor import Monitor


ROOT = Path(__file__).resolve().parents[1]


def test_http_monitor_starts_with_read_only_config_mount_and_no_browser_cache(tmp_path, monkeypatch):
    """Replay the shipped YAML at the real startup boundary, without network."""
    mounted_config = tmp_path / "validation-config"
    mounted_config.mkdir()
    writable_volume = tmp_path / "validation"
    settings = yaml.safe_load((ROOT / "igwatcher-validation.example.yaml").read_text(encoding="utf-8"))
    # Remap only the container volume into the test's real temporary filesystem.
    settings["paths"] = {key: str(writable_volume / Path(value).name)
                         for key, value in settings["paths"].items()}
    settings_path = mounted_config / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    config = load_config(settings_path)
    assert config.browser.browsers_path == mounted_config / "data" / "ms-playwright"
    original_mkdir = Path.mkdir

    def container_mkdir(path, *args, **kwargs):
        if path == mounted_config or mounted_config in path.parents:
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return original_mkdir(path, *args, **kwargs)

    # Windows chmod is not a Linux read-only mount: model EROFS at the filesystem
    # boundary while leaving load_config, Database and Monitor initialization real.
    monkeypatch.setattr(Path, "mkdir", container_mkdir)
    db = Database(config.paths.data_dir / "state.sqlite3")
    try:
        Monitor(config, db)
        assert config.paths.data_dir.is_dir()
        assert config.paths.download_root.is_dir()
        assert config.paths.diagnostics_dir.is_dir()
        assert not config.browser.browsers_path.exists()
    finally:
        db.close()


@pytest.mark.parametrize("source", ["legacy", "anonyig"])
def test_browser_sources_still_prepare_their_browser_directory(tmp_path, source):
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({
        "accounts": [{"url": "https://www.instagram.com/nasa/"}],
        "browser": {"anonymous_source": source},
        "telegram": {"enabled": False}, "apify": {"enabled": False},
    }), encoding="utf-8")
    config = load_config(settings)
    db = Database(config.paths.data_dir / "state.sqlite3")
    try:
        Monitor(config, db)
        assert config.browser.browsers_path.is_dir()
        assert config.paths.download_root.is_dir()
        assert config.paths.diagnostics_dir.is_dir()
    finally:
        db.close()


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
