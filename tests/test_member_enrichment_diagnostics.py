"""Attribute actual member source blocks without changing retry behavior."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ig_monitor.config import InstagramEnrichmentConfig
from ig_monitor.db import Database
from ig_monitor.member_enrichment import MemberEnrichmentWorker
from ig_monitor.models import ScrapeFailure


@pytest.fixture
def member_runtime(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    now = datetime(2026, 8, 2, tzinfo=UTC)
    db.conn.execute(
        """INSERT INTO relationship_members(
             instagram_profile_id,username,username_observed_at,created_at,updated_at,avatar_url
           ) VALUES('1','alice',?,?,?,?)""",
        (now.isoformat(), now.isoformat(), now.isoformat(),
         "https://scontent.cdninstagram.com/private-avatar?token=secret"),
    )
    db.conn.commit()
    db.enqueue_member_enrichment("1", "manual", now.isoformat())
    config = InstagramEnrichmentConfig(
        enabled=True, member_limit_per_direction=1000, page_size=200,
        page_delay_min_seconds=10, page_delay_max_seconds=20,
        direction_delay_min_seconds=120, direction_delay_max_seconds=300,
        daily_relationship_jobs=6, minimum_job_interval_minutes=240,
        reconciliation_days=30, observation_hours=72, canary_days=7,
        daily_member_enrichments=66, member_delay_min_seconds=30,
        member_delay_max_seconds=90, member_retry_min_hours=6, member_stale_days=30,
    )
    yield db, now, config
    db.close()


def worker_for(db, config, source_name, failure):
    source = SimpleNamespace(
        browser=SimpleNamespace(anonymous_source=source_name),
        fetch_profile=AsyncMock(side_effect=failure),
    )
    worker = MemberEnrichmentWorker(db, config, source, random_uniform=lambda *_: 30)
    return worker, source


def test_member_block_logs_current_username_and_preserves_retry(member_runtime, caplog):
    db, now, config = member_runtime
    # A queued job may outlive a member rename; attribute its current target.
    db.conn.execute("UPDATE relationship_members SET username='alice_current' WHERE instagram_profile_id='1'")
    db.conn.commit()
    detail = ("來源驗證／限流／拒絕存取 [IGWATCHER-BLOCK-DIAG] "
              "http_status=429 phase=api endpoint=profile redirects=0 signal=http_status")
    failure = ScrapeFailure(detail, "IGWatcher", blocker="source_blocked")
    worker, source = worker_for(db, config, "igwatcher", failure)

    result = worker.run_once(now)

    assert result.status == "incomplete"
    messages = [record.getMessage() for record in caplog.records if "operation=member_profile" in record.getMessage()]
    assert len(messages) == 1
    assert messages[0].startswith("alice_current：")
    assert str(failure) in messages[0]
    for forbidden in ("https://", "private-avatar", "token", "secret"):
        assert forbidden not in messages[0]
    assert source.fetch_profile.call_args.args[1] == "alice_current"
    row = db.conn.execute("SELECT * FROM member_enrichment_jobs WHERE id=?", (result.job_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["available_at"] == (now + timedelta(hours=6)).isoformat(timespec="seconds")
    assert row["last_error"] == "ScrapeFailure"
    assert db.get_meta("member_enrichment_next_at") == (now + timedelta(seconds=30)).isoformat(timespec="seconds")
    assert db.source_cooldown("igwatcher", now)

    caplog.clear()
    assert worker.run_once(now + timedelta(seconds=31)).status == "source_cooldown"
    assert source.fetch_profile.await_count == 1
    assert not caplog.records


@pytest.mark.parametrize("source_name,blocker", [
    ("igwatcher", "source_cooldown"),
    ("igwatcher", None),
    ("anonyig", "source_blocked"),
    ("legacy", "source_blocked"),
])
def test_member_diagnostics_ignore_guards_and_other_sources(member_runtime, caplog, source_name, blocker):
    db, now, config = member_runtime
    worker, source = worker_for(db, config, source_name, ScrapeFailure("未取得回應", "來源", blocker=blocker))

    assert worker.run_once(now).status == "incomplete"
    assert source.fetch_profile.await_count == 1
    assert not caplog.records
    assert bool(db.source_cooldown(source_name, now)) is bool(blocker)


@pytest.mark.parametrize("username", ["alice\nAuthorization: secret", "https://example.test/?token=secret"])
def test_member_diagnostics_do_not_log_malformed_username(member_runtime, caplog, username):
    db, now, config = member_runtime
    db.conn.execute("UPDATE relationship_members SET username=? WHERE instagram_profile_id='1'", (username,))
    db.conn.commit()
    worker, _source = worker_for(
        db, config, "igwatcher", ScrapeFailure("來源要求驗證", "IGWatcher", blocker="source_blocked"),
    )

    assert worker.run_once(now).status == "incomplete"
    message = next(record.getMessage() for record in caplog.records if "operation=member_profile" in record.getMessage())
    assert message.startswith("unknown：")
    assert "\n" not in message
    assert "secret" not in message
    assert "https://" not in message
