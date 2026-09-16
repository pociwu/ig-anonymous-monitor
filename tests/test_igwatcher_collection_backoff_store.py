import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ig_monitor.anonymous_store import read_collection_observations
from ig_monitor.config import AccountConfig
from ig_monitor.db import Database
from ig_monitor.models import CollectionObservation, TerminalState


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "igwatcher-backoff.sqlite3")
    value.sync_accounts([
        AccountConfig("https://www.instagram.com/alice/", True, "Alice"),
        AccountConfig("https://www.instagram.com/bob/", True, "Bob"),
    ])
    yield value
    value.close()


def test_posts_retry_delay_doubles_to_four_hours_and_expires_at_deadline(db):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    for count, deadline in enumerate((
        "2026-09-16T00:30:00+00:00",
        "2026-09-16T01:30:00+00:00",
        "2026-09-16T03:30:00+00:00",
        "2026-09-16T07:30:00+00:00",
        "2026-09-16T11:30:00+00:00",
    ), 1):
        db.record_collection_observations(account_id, "Alice", {
            "posts": {"state": "failed", "error": "Posts unavailable", "error_code": "posts_gated"},
        }, source="igwatcher", now=moment)
        state = db.collection_observations(account_id, "igwatcher")["posts"]
        assert state["next_retry_at"] == deadline
        assert state["error_code"] == "posts_gated"
        assert state["fail_count"] == count
        assert db.collection_backoff(account_id, "igwatcher", "posts", now=moment) == state
        moment = datetime.fromisoformat(deadline)
        assert db.collection_backoff(account_id, "igwatcher", "posts", now=moment - timedelta(seconds=1))
        assert db.collection_backoff(account_id, "igwatcher", "posts", now=moment) is None
    events = db.pending_events(20)
    assert [event["kind"] for event in events] == ["failure"]
    assert events[0]["payload"]["next_retry_at"] == "2026-09-16T03:30:00+00:00"


def test_skipped_collection_preserves_every_saved_field_and_events_after_reopen(db):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    for offset in range(3):
        db.record_collection_observations(account_id, "Alice", {
            "posts": {
                "state": "partial", "error": "Posts unavailable", "error_code": "fetch_failed",
                "cursor": "saved-page",
            },
        }, source="igwatcher", now=moment + timedelta(hours=offset))
    before = db.collection_observations(account_id, "igwatcher")["posts"]
    events_before = db.pending_events(20)
    reader = Database(db.path)
    try:
        reader.record_collection_observations(account_id, "Alice", {
            "posts": {"state": "media", "complete": True, "cursor": "wrong", "attempted": False},
        }, source="igwatcher", now=moment + timedelta(hours=2, minutes=1))
        assert reader.collection_observations(account_id, "igwatcher")["posts"] == before
        assert reader.collection_backoff(account_id, "igwatcher", "posts", now=moment + timedelta(hours=3)) == before
        assert reader.pending_events(20) == events_before
        unseen = reader.collection_observations(account_id, "igwatcher")["stories"]
        reader.record_collection_observations(account_id, "Alice", {
            "stories": {"state": "unknown", "attempted": False},
        }, source="igwatcher", now=moment)
        assert reader.collection_observations(account_id, "igwatcher")["stories"] == unseen
    finally:
        reader.close()


@pytest.mark.parametrize("state,error", [("private", None), ("blocked", "Source blocked")])
def test_private_and_source_blocked_do_not_resolve_posts_incident(db, state, error):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.MEDIA, cursor="saved-page", complete=True),
    }, source="igwatcher", now=moment)
    for offset in range(1, 4):
        db.record_collection_observations(account_id, "Alice", {
            "posts": {"state": "failed", "error": "Posts unavailable", "error_code": "posts_gated"},
        }, source="igwatcher", now=moment + timedelta(minutes=offset))
    before = db.collection_observations(account_id, "igwatcher")["posts"]
    db.record_collection_observations(account_id, "Alice", {
        "posts": {"state": state, "error": error, "complete": True},
    }, source="igwatcher", now=moment + timedelta(minutes=4))
    after = db.collection_observations(account_id, "igwatcher")["posts"]
    for field in ("fail_count", "failure_since", "failure_notified", "next_retry_at", "cursor", "last_success_at"):
        assert after[field] == before[field]
    assert [event["kind"] for event in db.pending_events(20)] == ["failure"]


def test_old_readonly_schema_supplies_defaults_then_migrates_without_losing_state(db):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.PARTIAL, error="Old failure", cursor="saved-page"),
    }, source="igwatcher", now=moment)
    db.conn.execute("ALTER TABLE anonymous_collection_state DROP COLUMN error_code")
    db.conn.execute("ALTER TABLE anonymous_collection_state DROP COLUMN next_retry_at")
    db.conn.commit()
    reader = sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    try:
        old = read_collection_observations(reader, account_id, "igwatcher")
        assert old["posts"]["error_code"] is None
        assert old["posts"]["next_retry_at"] is None
        assert old["stories"]["error_code"] is None
        assert old["stories"]["next_retry_at"] is None
        assert old["posts"]["fail_count"] == 1
        assert old["posts"]["cursor"] == "saved-page"
    finally:
        reader.close()
    for _ in range(2):
        migrated = Database(db.path)
        try:
            assert migrated.collection_observations(account_id, "igwatcher") == old
            assert migrated.collection_backoff(account_id, "igwatcher", "posts", now=moment) is None
        finally:
            migrated.close()
    db.record_collection_observations(account_id, "Alice", {
        "posts": {"state": "failed", "error": "Posts unavailable", "error_code": "fetch_failed"},
    }, source="igwatcher", now=moment)
    assert db.collection_backoff(account_id, "igwatcher", "posts", now=moment)["next_retry_at"] == "2026-09-16T01:00:00+00:00"


def test_slots_observations_keep_error_code_and_skip_unattempted_result(db):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.FAILED, error="Posts unavailable", error_code="posts_gated"),
    }, source="igwatcher", now=moment)
    before = db.collection_observations(account_id, "igwatcher")["posts"]
    assert before["error_code"] == "posts_gated"
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.EMPTY, complete=True, attempted=False),
    }, source="igwatcher", now=moment + timedelta(minutes=1))
    assert db.collection_observations(account_id, "igwatcher")["posts"] == before


@pytest.mark.parametrize("state", [TerminalState.PARTIAL, TerminalState.MEDIA, TerminalState.EMPTY])
def test_clean_posts_clear_backoff_and_emit_only_one_recovery(db, state):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    for offset in range(3):
        db.record_collection_observations(account_id, "Alice", {
            "posts": CollectionObservation(TerminalState.FAILED, error="Posts unavailable", error_code="fetch_failed"),
        }, source="igwatcher", now=moment + timedelta(hours=offset))
    for offset in range(2):
        db.record_collection_observations(account_id, "Alice", {
            "posts": CollectionObservation(state, error_code="fetch_failed"),
        }, source="igwatcher", now=moment + timedelta(hours=4, minutes=offset))
    result = db.collection_observations(account_id, "igwatcher")["posts"]
    assert result["error"] is None
    assert result["error_code"] is None
    assert result["next_retry_at"] is None
    assert result["fail_count"] == 0
    assert result["failure_since"] is None
    assert result["failure_notified"] == 0
    assert not result["complete"]
    assert db.collection_backoff(account_id, "igwatcher", "posts", now=moment) is None
    assert [event["kind"] for event in db.pending_events(20)] == ["failure", "recovery"]
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.FAILED, error="Posts unavailable", error_code="posts_gated"),
    }, source="igwatcher", now=moment + timedelta(hours=5))
    assert db.collection_observations(account_id, "igwatcher")["posts"]["next_retry_at"] == "2026-09-16T05:30:00+00:00"


def test_posts_backoff_is_independent_of_other_accounts_sources_and_categories(db):
    alice, bob = [account["id"] for account in db.enabled_accounts()]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    failure = CollectionObservation(TerminalState.FAILED, error="Request failed", error_code="fetch_failed")
    for offset in range(3):
        db.record_collection_observations(alice, "Alice", {
            "posts": failure, "stories": failure, "highlights": failure, "reels": failure,
        }, source="igwatcher", now=moment + timedelta(minutes=offset))
        db.record_collection_observations(alice, "Alice", {"posts": failure}, source="anonyig", now=moment)
        db.record_collection_observations(alice, "Alice", {"posts": failure}, source="legacy", now=moment)
    assert db.collection_backoff(bob, "igwatcher", "posts", now=moment) is None
    for source, category in (("anonyig", "posts"), ("legacy", "posts"), ("igwatcher", "stories"),
                             ("igwatcher", "highlights"), ("igwatcher", "reels")):
        assert db.collection_observations(alice, source)[category]["next_retry_at"] is None
        assert db.collection_backoff(alice, source, category, now=moment) is None
    db.record_collection_observations(bob, "Bob", {"posts": failure}, source="igwatcher", now=moment)
    assert db.collection_backoff(bob, "igwatcher", "posts", now=moment)["next_retry_at"] == "2026-09-16T00:30:00+00:00"
    assert db.collection_backoff(alice, "igwatcher", "posts", now=moment)["next_retry_at"] == "2026-09-16T02:02:00+00:00"
    for event in db.pending_events(20):
        payload = event["payload"]
        assert ("next_retry_at" in payload) == (payload["source"] == "igwatcher" and payload["category"] == "posts")
    assert db.source_cooldown("igwatcher", now=moment) is None
    assert all(account["fail_count"] == 0 for account in db.enabled_accounts())


def test_legacy_observation_object_defaults_to_attempted_and_other_private_behavior_is_unchanged(db):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 16, tzinfo=UTC)
    observation = SimpleNamespace(state=TerminalState.FAILED, error="Old caller failure", cursor=None, complete=False)
    db.record_collection_observations(account_id, "Alice", {"posts": observation}, source="igwatcher", now=moment)
    result = db.collection_backoff(account_id, "igwatcher", "posts", now=moment)
    assert result["fail_count"] == 1
    assert result["error_code"] is None
    for _ in range(3):
        db.record_collection_observations(account_id, "Alice", {"posts": observation}, source="anonyig", now=moment)
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.PRIVATE, complete=True),
    }, source="anonyig", now=moment)
    result = db.collection_observations(account_id, "anonyig")["posts"]
    assert result["complete"]
    assert result["fail_count"] == 0
    assert [event["kind"] for event in db.pending_events(20)] == ["failure", "recovery"]
