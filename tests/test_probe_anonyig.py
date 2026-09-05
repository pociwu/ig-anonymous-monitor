from pathlib import Path

import pytest

from scripts import probe_anonyig


@pytest.mark.parametrize("platform,variable,folder,expected", [
    ("linux", None, None, ".cache/ms-playwright"),
    ("linux", "XDG_CACHE_HOME", "xdg", "xdg/ms-playwright"),
    ("linux", "PLAYWRIGHT_BROWSERS_PATH", "browsers", "browsers"),
    ("win32", "LOCALAPPDATA", "local", "local/ms-playwright"),
    ("darwin", None, None, "Library/Caches/ms-playwright"),
])
def test_probe_uses_platform_browser_cache(monkeypatch, tmp_path, platform, variable, folder, expected):
    for name in ("LOCALAPPDATA", "XDG_CACHE_HOME", "PLAYWRIGHT_BROWSERS_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(probe_anonyig.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if variable:
        monkeypatch.setenv(variable, str(tmp_path / folder))
    assert probe_anonyig.browser_cache_path() == tmp_path / expected
