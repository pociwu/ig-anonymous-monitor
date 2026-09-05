from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ig_monitor.config import account_username, canonical_account_url, load_config, normalize_account_url
from ig_monitor.db import Database
from ig_monitor.models import (
    COLLECTIONS, CollectionObservation, MediaCandidate, PrivacyState, ProfileSnapshot,
    ScrapeFailure, ScrapeResult, TerminalState,
)
from ig_monitor.monitor import Monitor
from ig_monitor.telegram import format_event


@pytest.fixture
def runtime(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
accounts:
  - url: https://www.instagram.com/alpha/
  - url: https://www.instagram.com/beta/
schedule:
  account_delay_min_seconds: 0
  account_delay_max_seconds: 0
  media_download_enabled: true
heartbeat:
  enabled: false
telegram:
  enabled: false
apify:
  enabled: false
""", encoding="utf-8")
    config = load_config(config_path)
    db = Database(config.paths.data_dir / "state.sqlite3")
    db.sync_accounts(config.accounts)
    yield config, db
    db.close()


def result_for(username="alpha", *, failed=False, blocked=False):
    snapshot = ProfileSnapshot(username, username, 12, 3, 4, "", PrivacyState.PUBLIC,
                               "https://cdn.example.test/avatar", observed_at=datetime.now(UTC).isoformat())
    observations = {key: CollectionObservation(TerminalState.EMPTY, complete=True) for key in COLLECTIONS}
    observations["posts"] = CollectionObservation(TerminalState.MEDIA, complete=True)
    if failed:
        observations["stories"] = CollectionObservation(TerminalState.FAILED, "temporary failure")
    if blocked:
        observations["stories"] = CollectionObservation(TerminalState.BLOCKED, "CAPTCHA")
    media = [MediaCandidate("asset", "posts", "image", "https://cdn.example.test/image",
                            source="anonyig", source_media_id="media1", parent_id="post1",
                            owner_id="id-" + username, owner_username=username)]
    return ScrapeResult(snapshot, media, collections=observations,
                        source="anonyig", profile_id="id-" + username)


class FakeScraper:
    supports_progress = True
    media_referer = "https://anonyig.com/"
    calls = []
    results = {}

    def __init__(self, config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def scrape(self, url, **kwargs):
        self.calls.append((url, kwargs))
        username = account_username(url)
        result = self.results.get(username) or result_for(username)
        if isinstance(result, Exception):
            raise result
        return result

    async def download(self, *args):
        return b"avatar", "image/jpeg"


def run_monitor(config, db, results):
    import asyncio
    FakeScraper.calls = []
    FakeScraper.results = results
    downloads = AsyncMock(return_value={"downloaded": 0, "failed": 0, "duplicate": 0, "pending": 0})
    with patch("ig_monitor.monitor.ProfileScraper", FakeScraper), \
            patch("ig_monitor.monitor.download_account_media", downloads):
        status = asyncio.run(Monitor(config, db).run())
    return status, downloads


def test_partial_categories_keep_successful_media_and_notify_once(runtime):
    config, db = runtime
    for _ in range(4):
        status, downloads = run_monitor(config, db, {"alpha": result_for(failed=True)})
        assert status == 1
        assert downloads.await_count == 2
    row = db.get_account("alpha")
    observations = db.collection_observations(row["id"])
    assert observations["posts"]["baseline_complete"]
    assert observations["stories"]["fail_count"] == 4
    assert db.conn.execute("SELECT COUNT(*) FROM media WHERE account_id=?", (row["id"],)).fetchone()[0] == 1
    incidents = [e for e in db.pending_events(100) if e["payload"].get("scope") == "collection"]
    assert [e["kind"] for e in incidents] == ["failure"]
    assert run_monitor(config, db, {})[0] == 0
    incidents = [e for e in db.pending_events(100) if e["payload"].get("scope") == "collection"]
    assert [e["kind"] for e in incidents] == ["failure", "recovery"]


def test_source_block_stops_other_accounts_and_downloads(runtime):
    config, db = runtime
    status, downloads = run_monitor(config, db, {"alpha": result_for(blocked=True)})
    assert status == 1
    assert len(FakeScraper.calls) == 1
    downloads.assert_not_awaited()
    row = db.get_account("alpha")
    assert db.conn.execute("SELECT COUNT(*) FROM media WHERE account_id=?", (row["id"],)).fetchone()[0] == 1
    assert db.get_account("beta")["fail_count"] == 0
    assert db.source_cooldown("anonyig")
    assert run_monitor(config, db, {})[0] == 0
    assert FakeScraper.calls == []


def test_exception_block_has_one_source_incident_not_account_failures(runtime):
    config, db = runtime
    result = ScrapeFailure("429", "profile", blocker="rate_limit")
    assert run_monitor(config, db, {"alpha": result})[0] == 1
    assert len(FakeScraper.calls) == 1
    assert db.get_account("alpha")["fail_count"] == 0
    events = [e for e in db.pending_events(100) if e["payload"].get("scope") == "source"]
    assert len(events) == 1
    assert "暫停請求" in format_event(events[0]["kind"], events[0]["payload"])


@pytest.mark.parametrize("stage", ["profile", "collection"])
def test_source_diagnostic_reaches_logs_file_and_db_without_duplicate_incidents(runtime, caplog, stage):
    config, db = runtime
    diagnostic = ('AnonyIG 來源暫停 [ANONYIG-DIAG] '
                  '{"source":"anonyig","endpoint":"stories","http_status":429,'
                  '"block_type":"http_rate_limit","observed_at":"2026-09-05T08:01:46+00:00"}')
    if stage == "profile":
        result = ScrapeFailure(diagnostic, "AnonyIG 個人檔案", blocker="CAPTCHA/網站限流")
    else:
        result = result_for(blocked=True)
        result.collections["stories"].error = diagnostic
    assert run_monitor(config, db, {"alpha": result})[0] == 1
    assert any(diagnostic in record.getMessage() for record in caplog.records)
    assert diagnostic in db.source_cooldown("anonyig")["error"]
    files = list(config.paths.diagnostics_dir.glob("alpha/*.txt"))
    assert len(files) == 1
    assert diagnostic in files[0].read_text(encoding="utf-8")
    assert run_monitor(config, db, {})[0] == 0
    assert FakeScraper.calls == []
    incidents = [event for event in db.pending_events(100) if event["payload"].get("scope") == "source"]
    assert len(incidents) == 1
    assert diagnostic in format_event(incidents[0]["kind"], incidents[0]["payload"])
    assert len(list(config.paths.diagnostics_dir.glob("alpha/*.txt"))) == 1


def test_failed_diagnostic_write_cannot_discard_successful_collections(runtime, caplog):
    config, db = runtime
    with patch("ig_monitor.monitor.save_diagnostic", side_effect=OSError("PRIVATE_DIAGNOSTIC_PATH")):
        status, downloads = run_monitor(config, db, {"alpha": result_for(blocked=True)})
    assert status == 1
    downloads.assert_not_awaited()
    alpha = db.get_account("alpha")
    assert alpha["snapshot_json"] is not None
    assert alpha["fail_count"] == 0
    assert db.collection_observations(alpha["id"])["posts"]["complete"]
    assert db.conn.execute("SELECT COUNT(*) FROM media WHERE account_id=?", (alpha["id"],)).fetchone()[0] == 1
    assert db.source_cooldown("anonyig")
    assert len(FakeScraper.calls) == 1
    assert "PRIVATE_DIAGNOSTIC_PATH" not in caplog.text


def test_avatar_challenge_preserves_verified_collections_and_stops_source(runtime):
    config, db = runtime
    with patch.object(FakeScraper, "download", AsyncMock(
        side_effect=ScrapeFailure("互動驗證", "下載媒體", blocker="captcha"),
    )):
        status, downloads = run_monitor(config, db, {})
    assert status == 1
    downloads.assert_not_awaited()
    assert len(FakeScraper.calls) == 1
    alpha = db.get_account("alpha")
    assert alpha["fail_count"] == 0
    assert db.collection_observations(alpha["id"])["posts"]["complete"]
    assert db.conn.execute("SELECT COUNT(*) FROM media WHERE account_id=?", (alpha["id"],)).fetchone()[0] == 1
    assert db.source_cooldown("anonyig")


@pytest.mark.parametrize("status,body,blocker", [
    (200, b"<html>Verify you are human</html>", "captcha"),
    (403, b"<html><script src='/cdn-cgi/challenge-platform/x'></script></html>", "captcha"),
    (200, b"<html>Too many requests</html>", "rate_limit"),
    (429, b"", "rate_limit"),
])
def test_media_response_challenges_are_source_blocks(runtime, status, body, blocker):
    import asyncio
    from types import SimpleNamespace
    from ig_monitor.scraper import ProfileScraper
    config, _db = runtime
    scraper = ProfileScraper(config.browser)
    response = SimpleNamespace(status=status, ok=status == 200,
                               headers={"content-type": "text/html"}, body=AsyncMock(return_value=body))
    scraper._context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    with pytest.raises(ScrapeFailure) as caught:
        asyncio.run(scraper.download("https://media.example.test/image", "https://anonyig.com/"))
    assert caught.value.blocker == blocker


def test_expired_media_url_is_not_a_source_block(runtime):
    import asyncio
    from types import SimpleNamespace
    from ig_monitor.scraper import ProfileScraper
    config, _db = runtime
    scraper = ProfileScraper(config.browser)
    response = SimpleNamespace(status=403, ok=False, headers={"content-type": "text/html"},
                               body=AsyncMock(return_value=b"<html>URL signature expired</html>"))
    scraper._context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        asyncio.run(scraper.download("https://media.example.test/image", "https://anonyig.com/"))


def test_disabled_recording_does_not_advance_baseline(runtime):
    config, db = runtime
    config = replace(config, schedule=replace(config.schedule, media_download_enabled=False))
    status, downloads = run_monitor(config, db, {})
    assert status == 0
    downloads.assert_not_awaited()
    assert db.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert not db.collection_observations(db.get_account("alpha")["id"])["posts"]["baseline_complete"]
    assert all(call[1] == {"cursors": {}, "known_ids": {}, "completed_categories": set()}
               for call in FakeScraper.calls)


def test_completed_empty_baseline_is_passed_to_incremental_scrape(runtime):
    config, db = runtime
    empty = result_for()
    empty.media = []
    empty.collections["posts"] = CollectionObservation(TerminalState.EMPTY, complete=True)
    assert run_monitor(config, db, {"alpha": empty})[0] == 0
    assert run_monitor(config, db, {})[0] == 0
    alpha_call = FakeScraper.calls[0][1]
    assert alpha_call["known_ids"]["posts"] == set()
    assert "posts" in alpha_call["completed_categories"]


def test_identity_mismatch_preserves_snapshot_and_rejects_media(runtime):
    config, db = runtime
    run_monitor(config, db, {})
    row = db.get_account("alpha")
    bad = result_for()
    bad.profile_id = "different-owner"
    bad.snapshot.posts = 999
    assert run_monitor(config, db, {"alpha": bad})[0] == 1
    assert db.snapshot_from_row(db.get_account("alpha")).posts == 12


def test_source_recovery_waits_until_run_has_no_new_block(runtime):
    config, db = runtime
    db.record_source_block("anonyig", "429", datetime.now(UTC) - timedelta(hours=1))
    run_monitor(config, db, {"beta": result_for("beta", blocked=True)})
    events = [e for e in db.pending_events(100) if e["payload"].get("scope") == "source"]
    assert [e["kind"] for e in events] == ["failure"]
    assert db.source_cooldown("anonyig")["block_count"] == 2


def test_identity_conflict_does_not_mask_simultaneous_source_block(runtime):
    config, db = runtime
    result = result_for("unrelated", blocked=True)
    assert run_monitor(config, db, {"alpha": result})[0] == 1
    assert len(FakeScraper.calls) == 1
    assert db.source_cooldown("anonyig")
    assert db.get_account("alpha")["snapshot_json"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_cooldown_from_other_worker_is_checked_after_account_delay(runtime):
    config, db = runtime

    async def another_worker_blocked(_delay):
        db.record_source_block("anonyig", "another worker received 429")

    with patch("ig_monitor.monitor.asyncio.sleep", another_worker_blocked):
        assert run_monitor(config, db, {})[0] == 0
    assert len(FakeScraper.calls) == 1
    assert db.get_account("beta")["snapshot_json"] is None
    assert db.conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "cooldown"


def test_shared_cooldown_stops_each_subsequent_media_download(runtime):
    import asyncio
    from ig_monitor.media import download_account_media
    config, db = runtime
    row = db.get_account("alpha")
    first = result_for().media[0]
    second = replace(first, media_key="second", source_media_id="second")
    db.record_success(row["id"], result_for().snapshot, [], [first, second])

    class MidDownloadBlock:
        calls = 0

        async def download(self, *_args):
            self.calls += 1
            db.record_source_block("anonyig", "another worker received 429")
            return b"downloaded-image", "image/jpeg"

    scraper = MidDownloadBlock()
    scraper.config = config.browser
    with pytest.raises(ScrapeFailure, match="冷卻"):
        asyncio.run(download_account_media(db, scraper, row, config.paths.download_root, 8,
                                           replace(config.dedup, enabled=False)))
    assert scraper.calls == 1
    assert db.conn.execute("SELECT COUNT(*) FROM media WHERE status='downloaded'").fetchone()[0] == 1


def test_validation_topology_has_no_production_mounts_or_credentials():
    import yaml
    root = Path(__file__).resolve().parents[1]
    topology = yaml.safe_load((root / "compose.validation.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load((root / "validation.example.yaml").read_text(encoding="utf-8"))
    excluded = set((root / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {".env", "config.yaml", "collector-secrets", "data", "downloads",
            ".pytest-tmp", ".validation"} <= excluded
    assert set(topology["services"]) == {"validate", "dashboard"}
    assert topology["services"]["dashboard"]["ports"] == ["127.0.0.1:8889:8888"]
    for service in topology["services"].values():
        assert service["volumes"] == ["./validation.example.yaml:/validation-config/settings.yaml:ro",
                                      "validation-state:/validation"]
        assert not any("TOKEN" in key or "SECRET" in key or "PASSWORD" in key for key in service["environment"])
    assert config["schedule"]["media_download_enabled"]
    assert all(value.startswith("/validation/") for value in config["paths"].values())
    for name in ("telegram", "heartbeat", "apify", "instagram_enrichment", "instagram_posts"):
        assert config[name]["enabled"] is False


def test_identity_urls_are_provider_independent_and_strict():
    assert canonical_account_url("ALPHA") == "https://www.instagram.com/alpha/"
    assert account_username("https://instagram.com/Alpha/?igsh=public-share") == "alpha"
    assert normalize_account_url("https://insta-stories-viewer.com/Alpha/").endswith("/alpha/")
    for url in ("http://instagram.com/a", "https://instagram.com:443/a", "https://x@instagram.com/a",
                "https://instagram.com/p/id", "https://instagram.com/a%2fb", "https://evil.test/a"):
        with pytest.raises(ValueError):
            normalize_account_url(url)


def test_duplicate_accounts_across_providers_are_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("accounts:\n  - url: https://www.instagram.com/alpha/\n"
                    "  - url: https://insta-stories-viewer.com/ALPHA/\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重複網址"):
        load_config(path, require_telegram=False)
