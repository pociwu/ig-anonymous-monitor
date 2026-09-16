"""Posts retry scheduling through the real monitor, HTTP adapter, and SQLite store."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import httpx
import pytest
from PIL import Image

from ig_monitor import anonymous_store
from ig_monitor.config import load_config
from ig_monitor.db import Database
from ig_monitor.igwatcher import IGWatcherScraper, PATHS
from ig_monitor.models import TerminalState
from ig_monitor.monitor import Monitor


def envelope(data, **extra):
    return {"status": "success", "code": 200, "data": data, **extra}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "accounts:\n"
        "  - url: https://instagram.com/alice/\n"
        "  - url: https://instagram.com/bob/\n"
        "browser:\n  anonymous_source: igwatcher\n",
        encoding="utf-8",
    )
    config = load_config(config_path, require_telegram=False)
    config = replace(
        config,
        telegram=replace(config.telegram, enabled=False),
        heartbeat=replace(config.heartbeat, enabled=False),
        apify=replace(config.apify, enabled=False),
        schedule=replace(config.schedule, media_download_enabled=False,
                         account_delay_min_seconds=0, account_delay_max_seconds=0),
    )
    output = BytesIO()
    Image.new("RGB", (32, 32), (60, 90, 120)).save(output, format="PNG")
    avatar = output.getvalue()
    original_time = anonymous_store._time

    class Runtime:
        now = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)

        def __init__(self):
            self.config = config
            self.path = tmp_path / "state.sqlite3"
            self.db = Database(self.path)
            self.db.sync_accounts(config.accounts)
            self.accounts = {row["account_key"]: row for row in self.db.enabled_accounts()}
            self.posts = {"alice": "gated", "bob": "empty"}
            self.followers = {"alice": 30, "bob": 40}
            self.requests = []
            self.results = {}
            self.block_stories = False

        def run(self):
            self.requests.clear()
            self.results.clear()
            return asyncio.run(Monitor(self.config, self.db).run())

        def reopen(self):
            self.db.close()
            self.db = Database(self.path)

        def state(self, username="alice"):
            return self.db.collection_observations(self.accounts[username]["id"], "igwatcher")["posts"]

        def events(self, kind):
            return [event for event in self.db.pending_events(100)
                    if event["kind"] == kind and event["payload"].get("scope") == "collection"]

        def source_paths(self, username):
            return [request.url.path for request in self.requests
                    if request.url.host == "igwatcher.com"
                    and request.url.params.get("username") == username]

        def respond(self, request):
            self.requests.append(request)
            if request.url.host == "scontent.cdninstagram.com":
                assert request.url.path == "/avatar.png"
                return httpx.Response(200, content=avatar, headers={"content-type": "image/png"})
            assert request.url.host == "igwatcher.com"
            username = request.url.params["username"]
            assert username in self.accounts
            path = request.url.path
            profile_id = "123" if username == "alice" else "456"
            if path == PATHS["profile"]:
                return httpx.Response(200, json=envelope({"user": {
                    "id": profile_id, "pk": profile_id, "username": username, "is_private": False,
                    "media_count": 12, "follower_count": self.followers[username], "following_count": 2,
                    "full_name": username.title(), "biography": "",
                    "profile_pic_url": "https://scontent.cdninstagram.com/avatar.png",
                }}))
            if path == PATHS["stories"] and self.block_stories:
                return httpx.Response(403, content=b"source unavailable")
            if path == PATHS["posts"]:
                mode = self.posts[username]
                if mode == "gated":
                    return httpx.Response(200, json=envelope([], note="posts_gated"))
                if mode == "fetch_failed":
                    return httpx.Response(200, json=envelope([], fetch_failed=True))
                if mode == "media":
                    return httpx.Response(200, json=envelope([{
                        "id": "100_" + profile_id, "media_type": 1,
                        "taken_at_timestamp": 1735689600,
                        "image_url": "https://scontent.cdninstagram.com/post.png",
                    }], has_more=False, nextMaxId=None))
                assert mode == "empty"
            assert path in {PATHS[key] for key in ("stories", "posts", "reels", "highlights")}
            return httpx.Response(200, json=envelope([], has_more=False, nextMaxId=None))

    value = Runtime()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.now.astimezone(tz) if tz else value.now.replace(tzinfo=None)

    class RecordingScraper(IGWatcherScraper):
        async def scrape(self, url, **kwargs):
            result = await super().scrape(url, **kwargs)
            value.results[result.snapshot.username] = result
            return result

    monkeypatch.setattr(anonymous_store, "_time", lambda instant=None:
                        original_time(value.now if instant is None else instant))
    monkeypatch.setattr("ig_monitor.igwatcher.datetime", Clock)
    monkeypatch.setattr("ig_monitor.monitor.ProfileScraper", lambda browser:
                        RecordingScraper(browser, transport=httpx.MockTransport(value.respond)))
    try:
        yield value
    finally:
        value.db.close()


def test_posts_backoff_survives_restart_and_skips_only_affected_account_collection(runtime):
    assert runtime.run() == 1
    first = runtime.state()
    assert first["fail_count"] == 1
    assert first["error_code"] == "posts_gated"
    assert first["next_retry_at"] == "2026-09-16T00:30:00+00:00"
    assert runtime.state("bob")["fail_count"] == 0
    assert runtime.db.source_cooldown("igwatcher") is None
    assert runtime.source_paths("alice").count(PATHS["posts"]) == 1
    assert runtime.source_paths("bob").count(PATHS["posts"]) == 1

    runtime.now += timedelta(minutes=10)
    runtime.followers["alice"] += 1
    runtime.reopen()
    assert runtime.db.collection_backoff(runtime.accounts["alice"]["id"], "igwatcher", "posts")
    runtime.run()
    assert runtime.state() == first
    assert runtime.source_paths("alice") == [PATHS[key] for key in ("profile", "stories", "reels", "highlights")]
    assert runtime.source_paths("bob") == [PATHS[key] for key in ("profile", "stories", "posts", "reels", "highlights")]
    skipped = runtime.results["alice"].collections["posts"]
    assert skipped.state == TerminalState.PARTIAL
    assert skipped.attempted is False
    assert skipped.error_code == "collection_backoff"
    assert skipped.error
    assert all(observation.attempted and observation.error is None
               for category, observation in runtime.results["alice"].collections.items()
               if category != "posts")
    saved = runtime.db.get_account_by_id(runtime.accounts["alice"]["id"])
    assert saved["fail_count"] == 0
    assert runtime.db.snapshot_from_row(saved).followers == 31
    assert runtime.events("recovery") == []

    runtime.now = datetime.fromisoformat(first["next_retry_at"])
    assert runtime.db.collection_backoff(runtime.accounts["alice"]["id"], "igwatcher", "posts") is None
    assert runtime.run() == 1
    assert runtime.source_paths("alice").count(PATHS["posts"]) == 1
    assert runtime.results["alice"].collections["posts"].attempted is True
    assert runtime.state()["fail_count"] == 2
    assert runtime.state()["next_retry_at"] == "2026-09-16T01:30:00+00:00"


@pytest.mark.parametrize("recovery", ["empty", "media"])
def test_gated_then_fetch_failed_keeps_incident_until_one_real_posts_recovery(runtime, recovery):
    since = runtime.now.isoformat(timespec="seconds")
    for count, (mode, minutes) in enumerate(zip(
        ("gated", "gated", "gated", "fetch_failed"), (30, 60, 120, 240), strict=True,
    ), 1):
        runtime.posts["alice"] = mode
        assert runtime.run() == 1
        state = runtime.state()
        assert state["fail_count"] == count
        assert state["error_code"] == ("posts_gated" if mode == "gated" else "fetch_failed")
        assert state["failure_since"] == since
        assert datetime.fromisoformat(state["next_retry_at"]) == runtime.now + timedelta(minutes=minutes)
        assert runtime.source_paths("alice").count(PATHS["posts"]) == 1
        assert runtime.db.get_account_by_id(runtime.accounts["alice"]["id"])["fail_count"] == 0
        assert runtime.db.source_cooldown("igwatcher") is None
        assert runtime.events("recovery") == []
        assert len(runtime.events("failure")) == (1 if count >= 3 else 0)
        if count == 3:
            runtime.now += timedelta(minutes=10)
            runtime.reopen()
            runtime.run()
            assert PATHS["posts"] not in runtime.source_paths("alice")
            assert runtime.state() == state
            assert len(runtime.events("failure")) == 1
            assert runtime.events("recovery") == []
        runtime.now = datetime.fromisoformat(state["next_retry_at"])

    runtime.posts["alice"] = recovery
    assert runtime.run() == 0
    result = runtime.results["alice"]
    assert result.collections["posts"].attempted is True
    assert result.collections["posts"].error is None
    assert result.collections["posts"].complete is False
    assert len([item for item in result.media if item.category == "posts"]) == (1 if recovery == "media" else 0)
    state = runtime.state()
    assert state["fail_count"] == state["failure_notified"] == 0
    assert state["error_code"] is state["next_retry_at"] is state["failure_since"] is None
    assert state["baseline_complete"] is False
    recovered = runtime.events("recovery")
    assert len(recovered) == 1
    assert recovered[0]["account_id"] == runtime.accounts["alice"]["id"]
    assert recovered[0]["payload"]["category"] == "posts"
    assert recovered[0]["payload"]["since"] == since

    runtime.now += timedelta(minutes=1)
    assert runtime.run() == 0
    assert len(runtime.events("recovery")) == 1
    assert len(runtime.events("failure")) == 1


def test_global_cooldown_prevents_all_requests_even_after_posts_retry_is_due(runtime):
    assert runtime.run() == 1
    posts = runtime.state()
    runtime.now = datetime.fromisoformat(posts["next_retry_at"])
    runtime.block_stories = True
    assert runtime.run() == 1
    assert runtime.source_paths("alice") == [PATHS["profile"], PATHS["stories"]]
    assert runtime.source_paths("bob") == []
    assert runtime.db.source_cooldown("igwatcher")
    assert runtime.db.collection_backoff(runtime.accounts["alice"]["id"], "igwatcher", "posts") is None
    assert runtime.state()["fail_count"] == posts["fail_count"]
    assert runtime.events("recovery") == []

    runtime.reopen()
    assert runtime.run() == 0
    assert runtime.requests == []
    assert runtime.results == {}
    assert runtime.state()["fail_count"] == posts["fail_count"]
    assert runtime.events("recovery") == []
