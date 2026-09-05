import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from ig_monitor.anonymous_store import read_collection_observations, read_gallery_memberships
from ig_monitor.config import AccountConfig
from ig_monitor.db import Database
from ig_monitor.models import CollectionObservation, MediaCandidate, PrivacyState, ProfileSnapshot, TerminalState


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "anonymous.sqlite3")
    value.sync_accounts([AccountConfig("https://insta-stories-viewer.com/alice/", True, "Alice")])
    yield value
    value.close()


def record(db, candidates):
    account_id = db.enabled_accounts()[0]["id"]
    snapshot = ProfileSnapshot("alice", "Alice", 12, 20, 30, "", PrivacyState.PUBLIC, "")
    db.record_success(account_id, snapshot, [], candidates)
    return account_id


def candidate(key, category="posts", group="p1", position=0, source_id=None, **kwargs):
    return MediaCandidate(
        key, category, "image", f"https://cdn.example/{key}?changing=1",
        source="anonyig", source_media_id=source_id or key,
        parent_id=group if category != "highlights" else None,
        album_id=group if category == "highlights" else None,
        position=position, **kwargs,
    )


def test_provider_migration_is_in_place_and_reentrant(db):
    original = db.enabled_accounts()[0]
    account_id = record(db, [candidate("image")])
    db.set_identity(account_id, "12345", "renamed")
    db.enqueue_event("retained", "change", {"label": "Alice"}, account_id)
    media_id = db.pending_media(account_id, 1)[0]["id"]
    db.mark_media_downloaded(media_id, "/isolated/image.jpg", "abc")
    db.conn.execute("UPDATE media SET status='quarantined' WHERE id=?", (media_id,))
    db.conn.commit()
    for _ in range(2):
        db.sync_accounts([AccountConfig("https://www.instagram.com/alice/", True, "New label")])
    current = db.enabled_accounts()[0]
    assert current["id"] == original["id"]
    assert current["account_key"] == "alice"
    assert current["effective_url"] == "https://www.instagram.com/renamed/"
    assert current["instagram_profile_id"] == "12345"
    assert len(db.profile_history(account_id)) == 1
    assert db.pending_events(20)[0]["event_key"] == "retained"
    assert db.conn.execute("SELECT status,local_path FROM media").fetchone()[:] == ("quarantined", "/isolated/image.jpg")
    db.sync_accounts([AccountConfig("https://www.instagram.com/renamed/", True, "Renamed")])
    assert db.enabled_accounts()[0]["id"] == account_id
    assert db.enabled_accounts()[0]["account_key"] == "alice"
    db.set_effective_url(account_id, "renamed_again")
    assert db.enabled_accounts()[0]["effective_url"] == "https://www.instagram.com/renamed_again/"
    db.sync_accounts([AccountConfig("https://insta-stories-viewer.com/alice/", True, "Original config")])
    assert db.enabled_accounts()[0]["id"] == account_id
    assert db.enabled_accounts()[0]["effective_url"] == "https://insta-stories-viewer.com/renamed_again/"


def test_ambiguous_identity_rolls_back_all_changes(db):
    db.sync_accounts([
        AccountConfig("https://insta-stories-viewer.com/alice/", True, "Alice"),
        AccountConfig("https://insta-stories-viewer.com/bob/", True, "Bob"),
    ])
    rows = db.enabled_accounts()
    db.set_identity(rows[0]["id"], "123", "bob")
    before = db.enabled_accounts()
    with pytest.raises(ValueError, match="Ambiguous"):
        db.sync_accounts([AccountConfig("https://www.instagram.com/bob/", True, "New")])
    assert db.enabled_accounts() == before
    assert len(db.conn.execute("SELECT * FROM accounts").fetchall()) == 2


def test_duplicate_original_and_renamed_config_is_rejected_atomically(db):
    account = db.enabled_accounts()[0]
    db.set_identity(account["id"], "123", "renamed")
    with pytest.raises(ValueError, match="Multiple configured"):
        db.sync_accounts([
            AccountConfig("https://www.instagram.com/alice/", True, "Alice"),
            AccountConfig("https://www.instagram.com/renamed/", True, "Again"),
        ])
    assert len(db.enabled_accounts()) == 1
    assert db.enabled_accounts()[0]["url"] == account["url"]


def test_memberships_keep_reused_items_positions_categories_and_albums(db):
    media = [
        candidate("a", position=0), candidate("a", position=1),
        candidate("a", category="reels"),
        candidate("a", category="highlights", group="album-1", album_title="Travel"),
        candidate("a", category="highlights", group="album-2", album_title="Family"),
    ]
    account_id = record(db, media)
    media_id = db.pending_media(account_id, 1)[0]["id"]
    db.mark_media_downloaded(media_id, "/isolated/a.jpg", "a")
    media[0].url = "https://other.example/refreshed?token=2"
    record(db, media)
    memberships = db.gallery_memberships(account_id)
    assert len(memberships) == 5
    assert {m["category"] for m in memberships} == {"posts", "reels", "highlights"}
    assert sorted(m["position"] for m in memberships if m["category"] == "posts") == [0, 1]
    assert {m["album_title"] for m in memberships if m["category"] == "highlights"} == {"Travel", "Family"}
    assert db.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1


def test_duplicate_and_promotion_move_all_memberships(db):
    account_id = record(db, [candidate("low"), candidate("high", "stories", "s1"), candidate("best", "reels", "r1")])
    items = {m["media_key"]: m for m in db.pending_media(account_id, 5)}
    for key, row in items.items():
        db.mark_media_downloaded(row["id"], f"/isolated/{key}.jpg", key)
    db.mark_media_duplicate(items["low"]["id"], items["high"]["id"], "high")
    db.promote_canonical(items["best"]["id"], items["high"]["id"])
    record(db, [candidate("low")])
    memberships = db.gallery_memberships(account_id)
    assert len(memberships) == 3
    assert {m["media_id"] for m in memberships} == {items["best"]["id"]}
    assert {m["category"] for m in memberships} == {"posts", "stories", "reels"}


def test_refreshed_candidate_retries_same_failed_media_without_duplicate_membership(db):
    item = candidate("asset", group="parent", owner_username="alice")
    account_id = record(db, [item])
    media_id = db.pending_media(account_id, 1)[0]["id"]
    db.mark_media_failed(media_id, "expired URL")
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.MEDIA, complete=True),
    })
    assert db.known_source_ids(account_id, "posts") == {"parent"}
    item.url = "https://cdn.example/asset?fresh=2"
    record(db, [item])
    retry = db.pending_media(account_id, 1)[0]
    assert retry["id"] == media_id
    assert retry["url"] == item.url
    assert retry["status"] == "failed"
    assert db.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM media_memberships").fetchone()[0] == 1


def test_legacy_group_is_not_inferred_and_quarantine_stays_hidden(db):
    account_id = record(db, [MediaCandidate("old", "posts", "image", "https://cdn.example/old", logical_id="guess", position=3)])
    media_id = db.pending_media(account_id, 1)[0]["id"]
    db.mark_media_downloaded(media_id, "/isolated/old.jpg", "old")
    assert db.gallery_memberships(account_id)[0]["group_id"] is None
    db.conn.execute("UPDATE media SET status='quarantined' WHERE id=?", (media_id,))
    db.conn.commit()
    assert db.gallery_memberships(account_id) == []


def test_collection_failure_is_independent_preserves_history_and_notifies_once(db):
    account_id = db.enabled_accounts()[0]["id"]
    moment = datetime(2026, 9, 5, tzinfo=UTC)
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.MEDIA, cursor="next", complete=True),
        "stories": CollectionObservation(TerminalState.EMPTY, complete=True),
    }, now=moment)
    for index in range(4):
        db.record_collection_observations(account_id, "Alice", {
            "posts": CollectionObservation(TerminalState.FAILED, error="network", cursor="wrong"),
            "stories": CollectionObservation(TerminalState.EMPTY, complete=True),
        }, now=moment + timedelta(minutes=index + 1))
    states = db.collection_observations(account_id)
    assert states["posts"]["last_success_at"] == moment.isoformat(timespec="seconds")
    assert states["posts"]["cursor"] == "next"
    assert states["posts"]["baseline_complete"]
    assert not states["posts"]["complete"]
    assert states["stories"]["fail_count"] == 0
    failure = db.pending_events(20)
    assert [e["kind"] for e in failure] == ["failure"]
    assert failure[0]["payload"]["scope"] == "collection"
    assert failure[0]["payload"]["category"] == "posts"
    for _ in range(2):
        db.record_collection_observations(account_id, "Alice", {"posts": CollectionObservation(TerminalState.MEDIA, complete=True)}, now=moment + timedelta(hours=1))
    assert [e["kind"] for e in db.pending_events(20)] == ["failure", "recovery"]


def test_observation_progress_disabled_does_not_create_baseline_or_skip_content(db):
    account_id = record(db, [candidate("child", group="parent")])
    observation = {"posts": CollectionObservation(TerminalState.MEDIA, cursor="next", complete=True)}
    db.record_collection_observations(account_id, "Alice", observation, persist_progress=False)
    state = db.collection_observations(account_id)["posts"]
    assert state["complete"]
    assert not state["baseline_complete"]
    assert state["cursor"] is None
    assert db.known_source_ids(account_id, "posts") == set()
    db.record_collection_observations(account_id, "Alice", observation)
    assert db.known_source_ids(account_id, "posts") == {"parent"}


def test_private_observation_does_not_establish_public_media_baseline(db):
    account_id = db.enabled_accounts()[0]["id"]
    private = {"posts": CollectionObservation(TerminalState.PRIVATE, complete=True)}
    db.record_collection_observations(account_id, "Alice", private)
    state = db.collection_observations(account_id)["posts"]
    assert state["complete"]
    assert not state["baseline_complete"]
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.EMPTY, complete=True),
    })
    db.record_collection_observations(account_id, "Alice", private)
    assert db.collection_observations(account_id)["posts"]["baseline_complete"]


def test_membership_owner_evidence_is_retained_and_schema_upgrade_is_reentrant(db):
    # Simulate the first migration schema, before ownership evidence was added.
    db.conn.execute("ALTER TABLE media_memberships DROP COLUMN owner_id")
    db.conn.execute("ALTER TABLE media_memberships DROP COLUMN owner_username")
    db.conn.commit()
    db._init_anonymous_schema()
    db._init_anonymous_schema()
    account_id = record(db, [candidate("a", owner_id="12345", owner_username="alice")])
    record(db, [candidate("a")])
    reopened = Database(db.path)
    try:
        membership = reopened.conn.execute(
            "SELECT owner_id,owner_username FROM media_memberships WHERE account_id=?", (account_id,),
        ).fetchone()
        assert tuple(membership) == ("12345", "alice")
        assert reopened.conn.execute("SELECT COUNT(*) FROM media_memberships").fetchone()[0] == 1
    finally:
        reopened.close()


def test_partial_and_blocked_results_do_not_erase_progress_or_increment_failures(db):
    account_id = db.enabled_accounts()[0]["id"]
    db.record_collection_observations(account_id, "Alice", {
        "highlights": CollectionObservation(TerminalState.PARTIAL, cursor="album-2"),
    })
    for _ in range(4):
        db.record_collection_observations(account_id, "Alice", {
            "highlights": CollectionObservation(TerminalState.BLOCKED, error="captcha"),
        })
    state = db.collection_observations(account_id)["highlights"]
    assert state["cursor"] == "album-2"
    assert not state["baseline_complete"]
    assert state["fail_count"] == 0
    assert db.pending_events(20) == []


def test_partial_error_preserves_valid_checkpoint_but_failed_result_cannot_replace_it(db):
    account_id = db.enabled_accounts()[0]["id"]
    db.record_collection_observations(account_id, "Alice", {
        "highlights": CollectionObservation(TerminalState.PARTIAL, error="One album failed", cursor="retry-album-2"),
    })
    state = db.collection_observations(account_id)["highlights"]
    assert state["cursor"] == "retry-album-2"
    assert state["fail_count"] == 1
    assert not state["baseline_complete"]
    db.record_collection_observations(account_id, "Alice", {
        "highlights": CollectionObservation(TerminalState.FAILED, error="Network error", cursor="invalid"),
    })
    assert db.collection_observations(account_id)["highlights"]["cursor"] == "retry-album-2"
    db.record_collection_observations(account_id, "Alice", {
        "highlights": CollectionObservation(TerminalState.PARTIAL, error="Still incomplete", cursor="not-recorded"),
    }, persist_progress=False)
    assert db.collection_observations(account_id)["highlights"]["cursor"] == "retry-album-2"


def test_source_backoff_persists_between_connections_and_cooldown_skips(db):
    start = datetime(2026, 9, 5, tzinfo=UTC)
    moment = start
    for count, minutes in enumerate((30, 60, 120, 240, 240), 1):
        result = db.record_source_block("anonyig", "rate limit", now=moment)
        assert result["block_count"] == count
        assert datetime.fromisoformat(result["next_allowed_at"]) == moment + timedelta(minutes=minutes)
        reader = Database(db.path)
        try:
            assert reader.source_cooldown("anonyig", now=moment)["block_count"] == count
            assert reader.record_source_block("anonyig", "already blocked", now=moment)["block_count"] == count
        finally:
            reader.close()
        moment += timedelta(minutes=minutes)
        assert db.source_cooldown("anonyig", now=moment) is None
    assert [e["kind"] for e in db.pending_events(20)] == ["failure"]
    db.record_source_recovery("anonyig", now=moment)
    db.record_source_recovery("anonyig", now=moment)
    events = db.pending_events(20)
    assert [e["kind"] for e in events] == ["failure", "recovery"]
    assert all(e["payload"]["scope"] == "source" for e in events)
    assert db.source_cooldown("anonyig", now=moment) is None
    assert db.record_source_block("anonyig", "again", now=moment + timedelta(minutes=1))["block_count"] == 1


def test_readonly_projections_and_repeat_initialization(db):
    account_id = record(db, [candidate("a")])
    media_id = db.pending_media(account_id, 1)[0]["id"]
    db.mark_media_downloaded(media_id, "/isolated/a.jpg", "a")
    for _ in range(2):
        reopened = Database(db.path)
        reopened.close()
    read = sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True)
    read.row_factory = sqlite3.Row
    try:
        assert len(read_gallery_memberships(read, account_id)) == 1
        assert set(read_collection_observations(read, account_id)) == {"posts", "stories", "highlights", "reels"}
    finally:
        read.close()
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    try:
        assert read_gallery_memberships(old, account_id) == []
        assert read_collection_observations(old, account_id)["reels"]["state"] == "unknown"
    finally:
        old.close()


def test_concurrent_workers_share_one_source_incident(db):
    barrier = Barrier(2)
    moment = datetime(2026, 9, 5, tzinfo=UTC)

    def blocked_worker():
        worker = Database(db.path)
        try:
            barrier.wait(timeout=5)
            return worker.record_source_block("anonyig", "captcha", now=moment)["block_count"]
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: blocked_worker(), range(2)))
    assert results == [1, 1]
    assert len(db.pending_events(20)) == 1


def test_recovery_cannot_erase_another_workers_new_cooldown(db):
    moment = datetime(2026, 9, 5, tzinfo=UTC)
    # Monitor observes no cooldown, then a separate worker receives a 429.
    assert db.source_cooldown("anonyig", now=moment) is None
    worker = Database(db.path)
    try:
        blocked = worker.record_source_block("anonyig", "HTTP 429", now=moment)
        db.record_source_recovery("anonyig", now=moment + timedelta(seconds=1))
        assert worker.source_cooldown("anonyig", now=moment + timedelta(seconds=1)) == blocked
        assert [event["kind"] for event in db.pending_events(20)] == ["failure"]
        db.record_source_recovery("anonyig", now=moment + timedelta(minutes=30))
        assert worker.source_cooldown("anonyig", now=moment + timedelta(minutes=30)) is None
        assert [event["kind"] for event in db.pending_events(20)] == ["failure", "recovery"]
    finally:
        worker.close()
