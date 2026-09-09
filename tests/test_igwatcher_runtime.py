"""Conservative source integration through configuration and the media pipeline."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
import httpx
from PIL import Image

from ig_monitor.config import load_config
from ig_monitor.db import Database
from ig_monitor.media import download_account_media
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot
from ig_monitor.monitor import Monitor
from ig_monitor.anonymous_store import latest_collection_source, read_group_observations
from ig_monitor.telegram import format_event
from ig_monitor.utils import sha256_bytes
from ig_monitor.dashboard import account_detail_data


def test_igwatcher_requires_explicit_configuration_and_keeps_downloads_off(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("accounts:\n  - url: https://instagram.com/nasa/\n", encoding="utf-8")
    original = load_config(path, require_telegram=False)
    assert original.browser.anonymous_source == "anonyig"
    assert original.schedule.media_download_enabled is False
    path.write_text(path.read_text(encoding="utf-8") +
                    "browser:\n  anonymous_source: igwatcher\n", encoding="utf-8")
    selected = load_config(path, require_telegram=False)
    assert selected.browser.anonymous_source == "igwatcher"
    assert selected.schedule.media_download_enabled is False


def test_scraper_factory_selects_http_adapter_only_when_explicit(media_runtime):
    from ig_monitor.igwatcher import IGWatcherScraper
    from ig_monitor.scraper import ProfileScraper
    config, _db, _account, _snapshot = media_runtime
    scraper = ProfileScraper(config.browser)
    assert isinstance(scraper, IGWatcherScraper)
    assert scraper.config == config.browser


@pytest.fixture
def media_runtime(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("accounts:\n  - url: https://instagram.com/nasa/\n"
                    "browser:\n  anonymous_source: igwatcher\n", encoding="utf-8")
    config = load_config(path, require_telegram=False)
    db = Database(tmp_path / "state.sqlite3")
    db.sync_accounts(config.accounts)
    account = db.enabled_accounts()[0]
    snapshot = ProfileSnapshot("nasa", "NASA", 12, 30, 2, "", PrivacyState.PUBLIC,
                               "", observed_at=datetime.now(UTC).isoformat())
    yield config, db, account, snapshot
    db.close()


def pending_item(key="local-one", **kwargs):
    return MediaCandidate(key, "posts", "image", "https://igwatcher.com/one.jpg",
                          source="igwatcher", parent_id="123", identity_kind="local",
                          ownership_status="pending", queried_username="nasa",
                          revision_id="revision-one", **kwargs)


class ByteDownloader:
    media_referer = "https://igwatcher.com/"

    def __init__(self, config, payloads):
        self.config = config.browser
        self.payloads = payloads
        self.calls = []

    async def download(self, url, referer):
        self.calls.append(url)
        return self.payloads[url], "image/png"


def image_bytes(changed=False):
    image = Image.new("RGB", (48, 48), (120, 50, 90))
    if changed:
        image.putpixel((1, 1), (121, 50, 90))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_download_saves_review_content_without_notification_attachments(media_runtime):
    config, db, account, snapshot = media_runtime
    item = pending_item()
    db.record_success(account["id"], snapshot, [], [item])
    downloader = ByteDownloader(config, {item.url: image_bytes()})
    stats = asyncio.run(download_account_media(
        db, downloader, account, config.paths.download_root, 8,
        replace(config.dedup, enabled=False),
    ))
    assert stats["downloaded"] == 1
    assert stats["review_downloaded"] == 1
    assert stats["attachments"] == []
    summary = format_event("media_summary", {"label": "NASA", **stats})
    assert "歸屬待確認：1" in summary
    assert "不代表已確認作者" in summary
    assert len(list(config.paths.download_root.rglob("*.png"))) == 1


def test_review_replacement_preserves_exact_old_bytes_despite_visual_similarity(media_runtime):
    config, db, account, snapshot = media_runtime
    first = pending_item()
    second = replace(first, media_key="local-two", revision_id="revision-two",
                     url="https://igwatcher.com/two.png")
    payloads = {first.url: image_bytes(), second.url: image_bytes(changed=True)}
    downloader = ByteDownloader(config, payloads)
    for item in (first, second):
        db.record_success(account["id"], snapshot, [], [item])
        stats = asyncio.run(download_account_media(
            db, downloader, account, config.paths.download_root, 8, config.dedup,
        ))
        assert stats["downloaded"] == 1
        assert stats["duplicate"] == 0
        assert stats["attachments"] == []
    assert {path.read_bytes() for path in config.paths.download_root.rglob("*.png")} == set(payloads.values())


def test_url_rotation_deduplicates_bytes_without_erasing_membership_history(media_runtime):
    config, db, account, snapshot = media_runtime
    first = pending_item()
    rotated = replace(first, media_key="local-rotated", revision_id="revision-rotated",
                      url="https://igwatcher.com/rotated.png")
    downloader = ByteDownloader(config, {item.url: image_bytes() for item in (first, rotated)})
    for item in (first, rotated, rotated):
        db.record_success(account["id"], snapshot, [], [item])
        stats = asyncio.run(download_account_media(
            db, downloader, account, config.paths.download_root, 8, config.dedup,
        ))
        assert stats["failed"] == 0
        assert stats["attachments"] == []
    assert downloader.calls == [first.url, rotated.url]
    assert len(list(config.paths.download_root.rglob("*.png"))) == 1
    memberships = db.gallery_memberships(account["id"])
    assert len(memberships) == 2
    assert {row["source_media_id"] for row in memberships} == {None}
    assert len({row["media_id"] for row in memberships}) == 1
    assert {row["ownership_status"] for row in memberships} == {"pending"}


def test_new_source_does_not_download_old_provider_pending_rows(media_runtime):
    config, db, account, snapshot = media_runtime
    current = pending_item()
    old = MediaCandidate("old", "posts", "image", "https://old.invalid/image.png",
                         source="anonyig", parent_id="old-parent", source_media_id="old-id")
    db.record_success(account["id"], snapshot, [], [old, current])
    downloader = ByteDownloader(config, {current.url: image_bytes()})
    stats = asyncio.run(download_account_media(
        db, downloader, account, config.paths.download_root, 8, config.dedup,
    ))
    assert downloader.calls == [current.url]
    assert stats["failed"] == 0
    assert [row["media_key"] for row in db.pending_media(account["id"], 8)] == ["old"]


@pytest.mark.parametrize("missing", [True, False])
def test_unavailable_or_modified_canonical_file_cannot_discard_fresh_review_bytes(media_runtime, missing):
    config, db, account, snapshot = media_runtime
    legacy = MediaCandidate("legacy", "posts", "image", "https://old.invalid/image.png")
    db.record_success(account["id"], snapshot, [], [legacy])
    row = db.pending_media(account["id"], 1)[0]
    old_path = config.paths.data_dir / "legacy.png"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    if not missing:
        old_path.write_bytes(b"externally changed file")
    db.mark_media_downloaded(row["id"], str(old_path), sha256_bytes(image_bytes()))
    current = pending_item()
    db.record_success(account["id"], snapshot, [], [current])
    downloader = ByteDownloader(config, {current.url: image_bytes()})
    stats = asyncio.run(download_account_media(
        db, downloader, account, config.paths.download_root, 8, config.dedup,
    ))
    assert stats["downloaded"] == 1
    assert stats["duplicate"] == 0
    assert stats["attachments"] == []
    assert any(path.read_bytes() == image_bytes() for path in config.paths.download_root.rglob("*.png"))
    if not missing:
        assert old_path.read_bytes() == b"externally changed file"


def test_same_target_filename_keeps_changed_file_and_saves_verified_incoming_bytes(media_runtime):
    config, db, account, snapshot = media_runtime
    first = replace(pending_item(), logical_id="parent")
    rotated = replace(first, media_key="rotated", revision_id="rotated",
                      url="https://igwatcher.com/rotated.png")
    downloader = ByteDownloader(config, {first.url: image_bytes(), rotated.url: image_bytes()})
    db.record_success(account["id"], snapshot, [], [first])
    asyncio.run(download_account_media(db, downloader, account, config.paths.download_root, 8, config.dedup))
    existing = next(config.paths.download_root.rglob("*.png"))
    existing.write_bytes(b"changed historical file")
    db.record_success(account["id"], snapshot, [], [rotated])
    stats = asyncio.run(download_account_media(db, downloader, account, config.paths.download_root, 8, config.dedup))
    assert stats["downloaded"] == 1
    assert existing.read_bytes() == b"changed historical file"
    assert {path.read_bytes() for path in config.paths.download_root.rglob("*.png")} == {
        b"changed historical file", image_bytes(),
    }


def test_clean_conservative_observation_resets_operational_failure_without_claiming_complete(media_runtime):
    _config, db, account, _snapshot = media_runtime
    for _ in range(3):
        db.record_collection_observations(account["id"], "NASA", {
            "posts": {"state": "partial", "complete": False, "error": "invalid source item"},
        }, "igwatcher")
    db.record_collection_observations(account["id"], "NASA", {
        "posts": {"state": "partial", "complete": False, "error": None},
    }, "igwatcher")
    observation = db.collection_observations(account["id"], "igwatcher")["posts"]
    assert observation["fail_count"] == 0
    assert observation["failure_notified"] == 0
    assert observation["complete"] is False
    assert observation["baseline_complete"] is False
    assert [event["kind"] for event in db.pending_events(100)] == ["failure", "recovery"]


@pytest.mark.parametrize("bad_download", [False, True])
def test_monitor_persists_conservative_groups_and_disabled_mode_keeps_only_observations(
    media_runtime, monkeypatch, bad_download,
):
    config, db, account, _snapshot = media_runtime
    config = replace(config, telegram=replace(config.telegram, enabled=False),
                     heartbeat=replace(config.heartbeat, enabled=False),
                     apify=replace(config.apify, enabled=False),
                     schedule=replace(config.schedule, media_download_enabled=True))
    now = int(datetime.now(UTC).timestamp())
    envelope = lambda data, **extra: {"status": "success", "code": 200, "data": data, **extra}
    payloads = {
        "/wp-json/igw/v1/search": envelope({"user": {
            "id": "123", "pk": "123", "username": "nasa", "is_private": False,
            "media_count": 12, "follower_count": 30, "following_count": 2,
            "full_name": "NASA", "biography": "", "profile_pic_url": "https://scontent.cdninstagram.com/avatar.png",
        }}),
        "/wp-json/igw/v1/stories": envelope([{
            "id": "500_123", "taken_at": now - 100, "expiring_at": now + 80000,
            "image_url": "https://scontent.cdninstagram.com/story.png",
        }]),
        "/wp-json/igw/v1/posts": envelope([{
            "id": "200_999", "taken_at_timestamp": now - 500,
            "media_type": 8, "is_carousel": True, "carousel_count": 2,
            "children": [{"is_video": False, "image_url": f"https://scontent.cdninstagram.com/{i}.png"}
                         for i in (1, 2)],
        }], has_more=False, nextMaxId=None),
        "/api/reels": envelope([], has_more=False, nextMaxId=None),
        "/wp-json/igw/v1/highlights": envelope([{"id": "highlight:400", "media_count": 2, "title": "Album"}]),
        "/api/highlight-items": envelope([{"id": "300_123", "taken_at": now - 1000,
                                           "image_url": "https://scontent.cdninstagram.com/3.png"}]),
    }

    def respond(request):
        if request.url.host == "igwatcher.com":
            return httpx.Response(200, json=payloads[request.url.path])
        assert request.url.host == "scontent.cdninstagram.com"
        if bad_download and request.url.path != "/avatar.png":
            return httpx.Response(200, content=b"not an image", headers={"content-type": "image/png"})
        return httpx.Response(200, content=image_bytes(), headers={"content-type": "image/png"})

    original_client = httpx.AsyncClient

    class OfflineClient(original_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", OfflineClient)
    assert asyncio.run(Monitor(config, db).run()) == (1 if bad_download else 0)
    assert latest_collection_source(db.conn, account["id"]) == "igwatcher"
    groups = read_group_observations(db.conn, account["id"])
    album = next(group for group in groups if group["category"] == "highlights")
    assert (album["declared_count"], album["received_count"], album["complete"]) == (2, 1, False)
    if bad_download:
        assert db.gallery_memberships(account["id"]) == []
        assert len(db.pending_media(account["id"], 100)) == 4
        return
    assert len(db.gallery_memberships(account["id"])) == 4
    detail, ordinary_media, _counts = account_detail_data(db.path, account["id"])
    assert ordinary_media == []
    story = next(group for group in detail["pending_gallery"] if group["category"] == "stories")
    assert len(story["children"]) == 1
    assert story["children"][0]["source_media_id"] == "500_123"
    events = [event for event in db.pending_events(100) if event["kind"] == "media_summary"]
    assert events and not events[-1]["payload"]["attachments"]
    assert all(not value["baseline_complete"] for value in db.collection_observations(account["id"], "igwatcher").values())

    disabled = replace(config, schedule=replace(config.schedule, media_download_enabled=False))
    other = Database(config.paths.data_dir / "disabled.sqlite3")
    try:
        assert asyncio.run(Monitor(disabled, other).run()) == 0
        other_id = other.enabled_accounts()[0]["id"]
        assert read_group_observations(other.conn, other_id)
        assert other.gallery_memberships(other_id) == []
        assert other.pending_media(other_id, 100) == []
    finally:
        other.close()


def nested_count_profile():
    """Synthetic values with the observed IGWatcher search count structure."""
    return {
        "id": "123", "pk": "123", "username": "nasa", "is_private": False,
        "edge_owner_to_timeline_media": {"count": 12},
        "edge_followed_by": {"count": 32}, "follower_count": 32,
        "edge_follow": {"count": 3}, "following_count": 3,
        "full_name": "NASA", "biography": "",
        "profile_pic_url": "https://scontent.cdninstagram.com/avatar.png",
    }


def run_monitor_with_profile(media_runtime, monkeypatch, user):
    config, db, _account, _snapshot = media_runtime
    config = replace(config, telegram=replace(config.telegram, enabled=False),
                     heartbeat=replace(config.heartbeat, enabled=False),
                     apify=replace(config.apify, enabled=False))
    requests = []
    empty_collections = {
        "/wp-json/igw/v1/stories", "/wp-json/igw/v1/posts",
        "/api/reels", "/wp-json/igw/v1/highlights",
    }

    def respond(request):
        requests.append((request.url.host, request.url.path))
        if request.url.host == "scontent.cdninstagram.com":
            assert request.url.path == "/avatar.png"
            return httpx.Response(200, content=image_bytes(), headers={"content-type": "image/png"})
        assert request.url.host == "igwatcher.com"
        if request.url.path == "/wp-json/igw/v1/search":
            return httpx.Response(200, json={"status": "success", "code": 200, "data": {"user": user}})
        assert request.url.path in empty_collections
        return httpx.Response(200, json={
            "status": "success", "code": 200, "data": [], "has_more": False, "nextMaxId": None,
        })

    original_client = httpx.AsyncClient

    class OfflineClient(original_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(respond)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", OfflineClient)
    return asyncio.run(Monitor(config, db).run()), requests


def test_monitor_persists_observed_nested_profile_counts_and_collects(media_runtime, monkeypatch):
    _config, db, account, _snapshot = media_runtime
    exit_code, requests = run_monitor_with_profile(media_runtime, monkeypatch, nested_count_profile())
    assert exit_code == 0
    saved = db.snapshot_from_row(db.get_account_by_id(account["id"]))
    assert (saved.posts, saved.followers, saved.following) == (12, 32, 3)
    history = db.conn.execute(
        "SELECT posts,followers,following FROM profile_history WHERE account_id=?", (account["id"],),
    ).fetchall()
    assert [tuple(row) for row in history] == [(12, 32, 3)]
    observations = db.collection_observations(account["id"], "igwatcher")
    assert set(observations) == {
        "stories", "posts", "reels", "highlights",
    }
    assert all(item["last_attempt_at"] and item["error"] is None for item in observations.values())
    assert requests == [
        ("igwatcher.com", "/wp-json/igw/v1/search"),
        ("igwatcher.com", "/wp-json/igw/v1/stories"),
        ("igwatcher.com", "/wp-json/igw/v1/posts"),
        ("igwatcher.com", "/api/reels"),
        ("igwatcher.com", "/wp-json/igw/v1/highlights"),
        ("scontent.cdninstagram.com", "/avatar.png"),
    ]
    initial = next(event for event in db.pending_events(100) if event["kind"] == "initial")
    assert tuple(initial["payload"]["snapshot"][key] for key in ("posts", "followers", "following")) == (12, 32, 3)


@pytest.mark.parametrize("count_problem", ["missing", "invalid", "conflicting"])
def test_bad_profile_counts_preserve_good_snapshot_without_zero_events(
    media_runtime, monkeypatch, count_problem,
):
    _config, db, account, snapshot = media_runtime
    db.record_success(account["id"], snapshot, [], [])
    before = db.get_account_by_id(account["id"])["snapshot_json"]
    collections_before = db.collection_observations(account["id"], "igwatcher")
    user = nested_count_profile()
    if count_problem == "missing":
        del user["edge_owner_to_timeline_media"]
    elif count_problem == "invalid":
        user["media_count"] = 12
        user["edge_owner_to_timeline_media"]["count"] = True
    else:
        user["media_count"] = 0

    exit_code, requests = run_monitor_with_profile(media_runtime, monkeypatch, user)
    assert exit_code == 1
    refreshed = db.get_account_by_id(account["id"])
    assert refreshed["snapshot_json"] == before
    assert refreshed["fail_count"] == 1
    assert "計數" in refreshed["last_error"]
    history = db.conn.execute(
        "SELECT posts,followers,following FROM profile_history WHERE account_id=?", (account["id"],),
    ).fetchall()
    assert [tuple(row) for row in history] == [(12, 30, 2)]
    assert db.pending_events(100) == []
    assert db.collection_observations(account["id"], "igwatcher") == collections_before
    assert requests == [("igwatcher.com", "/wp-json/igw/v1/search")]
