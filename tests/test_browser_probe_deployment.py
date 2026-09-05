"""Static deployment contract for the isolated, one-shot browser comparison."""
from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[1]


def probe_topology():
    return yaml.safe_load((ROOT / "compose.browser-probe.yaml").read_text(encoding="utf-8"))


def test_browser_probe_has_its_own_project_image_and_single_service():
    topology = probe_topology()
    assert topology["name"] == "ig-monitor-anonyig-browser-probe"
    assert set(topology) == {"name", "services"}
    assert set(topology["services"]) == {"probe"}
    service = topology["services"]["probe"]
    assert service["image"] == "ig-anonymous-monitor:browser-probe"
    assert service["build"] == {"context": "."}


def test_browser_probe_has_no_persistent_mounts_credentials_ports_or_restart_policy():
    service = probe_topology()["services"]["probe"]
    assert set(service) == {
        "build", "image", "init", "user", "working_dir", "read_only", "tmpfs",
        "shm_size", "environment", "entrypoint", "command",
    }
    assert service["init"] is True
    assert service["user"] == "pwuser"
    assert service["working_dir"] == "/tmp"
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp"]
    assert service["shm_size"] == "1gb"
    assert not set(service) & {
        "volumes", "volumes_from", "env_file", "configs", "secrets", "ports",
        "restart", "depends_on", "extends", "privileged", "cap_add", "devices",
        "network_mode", "pid", "ipc", "extra_hosts", "sysctls", "ulimits",
    }


def test_browser_probe_runs_one_bounded_headed_session_and_keeps_arguments_overridable():
    service = probe_topology()["services"]["probe"]
    assert service["entrypoint"] == [
        "timeout", "--signal=TERM", "--kill-after=5s", "90s",
        "xvfb-run", "-a", "python", "-m", "ig_monitor.browser_probe",
    ]
    assert service["command"] == ["--observe-seconds", "30"]
    assert service["environment"] == {
        "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
        "PYTHONUNBUFFERED": "1",
        "TZ": "Asia/Taipei",
        "IG_MONITOR_RUNTIME": "browser-probe",
    }


def test_existing_image_build_installs_browser_probe_module_without_a_version_upgrade():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "FROM mcr.microsoft.com/playwright/python:v1.61.0-noble" in dockerfile.splitlines()
    assert "COPY ig_monitor ./ig_monitor" in dockerfile.splitlines()
    assert "RUN python -m pip install --no-cache-dir ." in dockerfile.splitlines()
    assert "playwright>=1.61,<1.62" in project["project"]["dependencies"]
    assert "ig_monitor*" in project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert (ROOT / "ig_monitor" / "__init__.py").is_file()
    assert (ROOT / "ig_monitor" / "browser_probe.py").is_file()
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not {"ig_monitor", "ig_monitor/", "ig_monitor/browser_probe.py", "*.py"} & set(ignored)
