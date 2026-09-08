"""Static isolation contract for the one-shot IGWatcher HTTP probe."""
from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[1]


def probe_topology():
    return yaml.safe_load((ROOT / "compose.igwatcher-probe.yaml").read_text(encoding="utf-8"))


def test_igwatcher_probe_has_a_separate_project_image_and_single_service():
    topology = probe_topology()
    assert set(topology) == {"name", "services"}
    assert topology["name"] == "ig-monitor-igwatcher-probe"
    assert set(topology["services"]) == {"probe"}
    service = topology["services"]["probe"]
    assert service["image"] == "ig-anonymous-monitor:igwatcher-probe"
    assert service["build"] == {"context": "."}


def test_igwatcher_probe_keeps_all_writes_ephemeral_and_has_no_service_connections():
    service = probe_topology()["services"]["probe"]
    assert set(service) == {
        "build", "image", "init", "user", "working_dir", "read_only", "tmpfs",
        "environment", "entrypoint", "command",
    }
    assert service["init"] is True
    assert service["user"] == "pwuser"
    assert service["working_dir"] == "/tmp"
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp"]
    assert not set(service) & {
        "volumes", "volumes_from", "env_file", "configs", "secrets", "ports",
        "restart", "depends_on", "extends", "privileged", "cap_add", "devices",
        "network_mode", "pid", "ipc", "extra_hosts", "sysctls", "ulimits",
        "shm_size", "healthcheck", "deploy",
    }


def test_igwatcher_probe_environment_has_no_credentials_or_browser_configuration():
    assert probe_topology()["services"]["probe"]["environment"] == {
        "PYTHONUNBUFFERED": "1",
        "TZ": "Asia/Taipei",
        "IG_MONITOR_RUNTIME": "igwatcher-probe",
    }


def test_igwatcher_probe_has_an_outer_time_limit_and_no_browser_or_scheduler_entrypoint():
    service = probe_topology()["services"]["probe"]
    assert service["entrypoint"] == [
        "timeout", "--signal=TERM", "--kill-after=5s", "180s",
        "python", "-m", "ig_monitor.igwatcher_probe",
    ]
    assert service["command"] == []


def test_existing_image_build_packages_igwatcher_http_probe_without_new_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "FROM mcr.microsoft.com/playwright/python:v1.61.0-noble" in dockerfile
    assert "COPY ig_monitor ./ig_monitor" in dockerfile
    assert "RUN python -m pip install --no-cache-dir ." in dockerfile
    assert "httpx>=0.27,<1" in project["project"]["dependencies"]
    assert "ig_monitor*" in project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert (ROOT / "ig_monitor" / "__init__.py").is_file()
    assert (ROOT / "ig_monitor" / "igwatcher_probe.py").is_file()
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not {"ig_monitor", "ig_monitor/", "ig_monitor/igwatcher_probe.py", "*.py"} & set(ignored)


def test_operator_commands_use_absolute_paths_and_capture_the_probe_exit_code():
    guide = (ROOT / "docs" / "igwatcher-probe.md").read_text(encoding="utf-8")
    checkout = "/srv/ig-monitor/ig-anonyig-validation"
    assert f"git -C {checkout} pull --ff-only" in guide
    assert f"docker compose -f {checkout}/compose.igwatcher-probe.yaml build probe" in guide
    assert f"docker compose -f {checkout}/compose.igwatcher-probe.yaml run --rm --no-deps probe\necho $?" in guide
    assert "[IGWATCHER-PROBE]" in guide
    assert "Ubuntu ARM64" in guide
