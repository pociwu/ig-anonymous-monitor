import sqlite3

import pytest

from ig_monitor.config import AccountConfig
from ig_monitor.anonymous_store import latest_collection_source, read_gallery_memberships, read_group_observations
from ig_monitor.db import Database
from ig_monitor.models import CollectionObservation, MediaCandidate, MediaGroupObservation, PrivacyState, ProfileSnapshot, TerminalState


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "igwatcher.sqlite3")
    database.sync_accounts([AccountConfig("https://www.instagram.com/alice/", True, "Alice")])
    yield database
    database.close()


def record(db, candidates):
    account_id = db.enabled_accounts()[0]["id"]
    db.record_success(account_id, ProfileSnapshot("alice", "Alice", 12, 20, 30, "", PrivacyState.PUBLIC, ""), [], candidates)
    return account_id


def local_item(key="local-a", position=0, revision="revision-a", **kwargs):
    return MediaCandidate(
        key, "posts", "image", f"https://cdn.example/{key}", parent_id="post-1",
        position=position, source="igwatcher", source_media_id=None,
        identity_kind="local", ownership_status="pending", queried_username="alice",
        revision_id=revision, **kwargs,
    )


def download_pending(db, account_id):
    rows = db.pending_media(account_id, 100)
    for row in rows:
        db.mark_media_downloaded(row["id"], f"/isolated/{row['media_key']}.jpg", row["media_key"])
    return {row["media_key"]: row["id"] for row in rows}


def test_local_membership_stays_local_and_pending_after_repeat_and_reopen(db):
    account_id = record(db, [local_item()])
    download_pending(db, account_id)
    record(db, [local_item()])
    reopened = Database(db.path)
    try:
        membership, = reopened.gallery_memberships(account_id)
        assert membership["identity_kind"] == "local"
        assert membership["source_media_id"] is None
        assert membership["ownership_status"] == "pending"
        assert membership["queried_username"] == "alice"
        assert membership["revision_id"] == "revision-a"
        assert membership["is_current"] is True
    finally:
        reopened.close()


def test_reordered_carousel_retains_old_positions_and_exact_file_memberships(db):
    account_id = record(db, [local_item("first-a", 0), local_item("first-b", 1)])
    originals = download_pending(db, account_id)
    first = MediaGroupObservation("posts", "post-1", "revision-a", 2, 2, "2026-09-09T01:00:00+00:00")
    db.record_group_observations(account_id, "igwatcher", [first])
    record(db, [local_item("second-b", 0, "revision-b"), local_item("second-a", 1, "revision-b")])
    pending = {row["media_key"]: row["id"] for row in db.pending_media(account_id, 10)}
    db.mark_media_duplicate(pending["second-b"], originals["first-b"], "first-b")
    db.mark_media_duplicate(pending["second-a"], originals["first-a"], "first-a")
    second = MediaGroupObservation("posts", "post-1", "revision-b", 2, 2, "2026-09-09T02:00:00+00:00")
    db.record_group_observations(account_id, "igwatcher", [second])
    db.record_group_observations(account_id, "igwatcher", [second])
    rows = db.gallery_memberships(account_id)
    current = sorted((row for row in rows if row["is_current"]), key=lambda row: row["position"])
    previous = sorted((row for row in rows if not row["is_current"]), key=lambda row: row["position"])
    assert [row["media_id"] for row in current] == [originals["first-b"], originals["first-a"]]
    assert [row["media_id"] for row in previous] == [originals["first-a"], originals["first-b"]]
    assert all(row["ownership_status"] == "pending" for row in rows)
    history = read_group_observations(db.conn, account_id)
    assert len(history) == 2
    assert [row["revision_id"] for row in history if row["is_current"]] == ["revision-b"]


def test_file_dedup_and_promotion_never_promote_ownership_evidence(db):
    verified = MediaCandidate("verified", "stories", "image", "https://cdn.example/verified",
                              source="anonyig", source_media_id="source-1", ownership_status="verified")
    account_id = record(db, [local_item(), verified])
    ids = download_pending(db, account_id)
    assert db.media_requires_review(ids["local-a"])
    assert not db.media_notification_allowed(ids["local-a"])
    assert db.media_notification_allowed(ids["verified"])
    db.mark_media_duplicate(ids["local-a"], ids["verified"], "same-bytes")
    assert db.media_requires_review(ids["verified"])
    assert db.media_requires_review(ids["local-a"])
    record(db, [MediaCandidate("promoted", "reels", "image", "https://cdn.example/promoted")])
    promoted = download_pending(db, account_id)["promoted"]
    db.promote_canonical(promoted, ids["verified"])
    rows = db.gallery_memberships(account_id)
    pending, = [row for row in rows if row["source"] == "igwatcher"]
    assert pending["ownership_status"] == "pending"
    assert pending["media_id"] == promoted
    assert db.media_requires_review(promoted)
    assert db.media_requires_review(ids["local-a"])


def test_provider_switch_downloads_only_that_sources_pending_items(db):
    account_id = record(db, [local_item(), MediaCandidate("old", "posts", "image", "https://old.example/item")])
    assert {row["media_key"] for row in db.pending_media(account_id, 10, source="igwatcher")} == {"local-a"}
    assert {row["media_key"] for row in db.pending_media(account_id, 10, source="legacy")} == {"old"}
    assert {row["media_key"] for row in db.pending_media(account_id, 10)} == {"local-a", "old"}


def test_highlight_counts_cannot_erase_missing_item_or_past_incompleteness(db):
    items = [MediaCandidate(
        f"story-{index}", "highlights", "image", f"https://cdn.example/story-{index}",
        source="igwatcher", source_media_id=f"{index}_123", album_id="album-1",
        album_title="Travel", position=index, ownership_status="pending", queried_username="alice",
    ) for index in range(10)]
    account_id = record(db, items)
    ids = download_pending(db, account_id)
    db.record_group_observations(account_id, "igwatcher", [
        MediaGroupObservation("highlights", "album-1", "full", 10, 10, "2026-09-09T00:00:00Z", complete=True),
        MediaGroupObservation("highlights", "album-1", "partial", 10, 9, "2026-09-09T01:00:00Z"),
    ])
    record(db, items[:9])
    db.record_group_observations(account_id, "igwatcher", [
        MediaGroupObservation("highlights", "album-1", "equal", 9, 9, "2026-09-09T02:00:00Z", complete=True),
    ])
    db.record_group_observations(account_id, "igwatcher", [])
    current, = [row for row in db.group_observations(account_id, "igwatcher") if row["is_current"]]
    assert (current["declared_count"], current["received_count"], current["complete"], current["ever_incomplete"]) == (9, 9, False, True)
    memberships = db.gallery_memberships(account_id)
    assert len(memberships) == 10
    assert all(row["is_current"] for row in memberships)
    assert ids["story-9"] in {row["media_id"] for row in memberships}
    assert len(db.group_observations(account_id, "igwatcher")) == 3


def test_readonly_source_selection_prefers_explicit_active_source_and_old_database_is_safe(db):
    account_id = db.enabled_accounts()[0]["id"]
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.PARTIAL),
    }, source="igwatcher", now="2026-09-09T01:00:00Z")
    assert latest_collection_source(db.conn, account_id) == "igwatcher"
    db.record_collection_observations(account_id, "Alice", {
        "posts": CollectionObservation(TerminalState.FAILED),
    }, source="anonyig", now="2026-09-09T02:00:00Z")
    db.set_meta(f"anonymous_active_source:{account_id}", "igwatcher")
    reader = sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    try:
        assert latest_collection_source(reader, account_id) == "igwatcher"
        assert read_group_observations(reader, account_id) == []
        assert read_gallery_memberships(reader, account_id) == []
    finally:
        reader.close()
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    old.execute("PRAGMA query_only=ON")
    try:
        assert latest_collection_source(old, account_id) == "anonyig"
        assert read_group_observations(old, account_id) == []
        assert read_gallery_memberships(old, account_id) == []
    finally:
        old.close()


def test_legacy_schema_read_then_repeated_migration_preserves_media_and_memberships(db):
    original = MediaCandidate("legacy-item", "posts", "image", "https://cdn.example/original",
                              source="anonyig", source_media_id="source-original", parent_id="original-post")
    account_id = record(db, [original])
    ids = download_pending(db, account_id)
    for column in ("identity_kind", "ownership_status", "queried_username", "revision_id"):
        db.conn.execute(f"ALTER TABLE media_memberships DROP COLUMN {column}")
    db.conn.execute("DROP TABLE anonymous_group_observations")
    db.conn.commit()
    reader = sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    try:
        old, = read_gallery_memberships(reader, account_id)
        assert (old["ownership_status"], old["revision_id"], old["is_current"]) == ("unspecified", "", True)
        assert read_group_observations(reader, account_id) == []
    finally:
        reader.close()
    for _ in range(2):
        reopened = Database(db.path)
        try:
            existing, = reopened.gallery_memberships(account_id)
            assert existing["media_id"] == ids["legacy-item"]
            assert existing["source_media_id"] == "source-original"
            assert existing["local_path"] == "/isolated/legacy-item.jpg"
            assert reopened.media_notification_allowed(ids["legacy-item"])
        finally:
            reopened.close()


def test_missing_provenance_on_reobservation_cannot_release_pending_membership(db):
    account_id = record(db, [local_item()])
    ids = download_pending(db, account_id)
    repeated = local_item()
    repeated.ownership_status = "unspecified"
    record(db, [repeated])
    membership, = db.gallery_memberships(account_id)
    assert membership["ownership_status"] == "pending"
    assert not db.media_notification_allowed(ids["local-a"])


def test_pre_membership_legacy_gallery_survives_later_pending_exact_copy_and_reopen(db, tmp_path):
    from ig_monitor.dashboard import account_detail_data, dashboard_data

    original = MediaCandidate("old-legacy", "posts", "image", "https://old.invalid/a")
    account_id = record(db, [original])
    original.category = "reels"
    record(db, [original])
    media_id = db.pending_media(account_id, 1)[0]["id"]
    path = tmp_path / "old-legacy.jpg"
    path.write_bytes(b"original bytes")
    db.mark_media_downloaded(media_id, str(path), "same-file")
    # Schema fixture for a database written before membership recording existed.
    db.conn.execute("DELETE FROM media_memberships")
    db.conn.execute("DELETE FROM meta WHERE key='anonymous_legacy_memberships_backfilled_v1'")
    db.conn.commit()
    upgraded = Database(db.path)
    try:
        record(upgraded, [local_item()])
        pending_id = upgraded.pending_media(account_id, 1)[0]["id"]
        upgraded.mark_media_duplicate(pending_id, media_id, "same-file")
    finally:
        upgraded.close()
    for _ in range(2):
        reopened = Database(db.path)
        try:
            memberships = reopened.gallery_memberships(account_id)
            legacy = [row for row in memberships if row["source"] == "legacy"]
            assert len(legacy) == 2
            assert {row["category"] for row in legacy} == {"posts", "reels"}
            assert all(row["group_id"] is None and row["source_media_id"] is None
                       and row["owner_id"] is None and row["owner_username"] is None for row in legacy)
            assert len([row for row in memberships if row["ownership_status"] == "pending"]) == 1
            account, media, _ = account_detail_data(db.path, account_id)
            assert account["gallery_counts"]["legacy"]["all"] == 1
            assert account["pending_gallery_counts"]["posts"]["all"] == 1
            assert [row["id"] for row in media] == [media_id]
            assert dashboard_data(db.path, lambda: {})["accounts"][0]["downloaded"] == 1
            assert path.read_bytes() == b"original bytes"
        finally:
            reopened.close()


def test_same_second_highlight_reordering_uses_latest_observed_positions(db, tmp_path, monkeypatch):
    from ig_monitor.dashboard import account_detail_data

    monkeypatch.setattr("ig_monitor.db.utc_now", lambda: "2026-09-09T08:00:00+00:00")

    def observe(order):
        return record(db, [MediaCandidate(
            key, "highlights", "image", f"https://cdn.example/{key}",
            source="igwatcher", source_media_id=key, album_id="album", position=position,
            ownership_status="pending", queried_username="alice",
        ) for position, key in enumerate(order)])

    account_id = observe(["a", "b"])
    ids = {}
    for row in db.pending_media(account_id, 2):
        path = tmp_path / f"{row['media_key']}.jpg"
        path.write_bytes(row["media_key"].encode())
        db.mark_media_downloaded(row["id"], str(path), row["media_key"])
        ids[row["media_key"]] = row["id"]
    observe(["b", "a"])
    observe(["a", "b"])
    account, _, _ = account_detail_data(db.path, account_id)
    album, = account["pending_gallery"]
    assert [child["id"] for child in album["children"]] == [ids["a"], ids["b"]]


@pytest.mark.parametrize("orphan_category,expected_legacy", [("posts", 0), ("reels", 1)])
def test_old_orphan_duplicate_backfills_only_unrepresented_category(db, orphan_category, expected_legacy):
    account_id = record(db, [
        MediaCandidate("known", "posts", "image", "https://cdn.example/known",
                       source="anonyig", source_media_id="known-source", parent_id="known-parent"),
        MediaCandidate("orphan", orphan_category, "image", "https://cdn.example/orphan"),
    ])
    ids = download_pending(db, account_id)
    db.mark_media_duplicate(ids["orphan"], ids["known"], "same")
    db.conn.execute("DELETE FROM media_memberships WHERE source='legacy'")
    db.conn.execute("DELETE FROM meta WHERE key='anonymous_legacy_memberships_backfilled_v1'")
    db.conn.commit()
    reopened = Database(db.path)
    try:
        rows = reopened.gallery_memberships(account_id)
        legacy = [row for row in rows if row["source"] == "legacy"]
        assert len(legacy) == expected_legacy
        assert len([row for row in rows if row["source"] == "anonyig"]) == 1
        assert all(row["media_id"] == ids["known"] for row in rows)
        assert all(row["group_id"] is None and row["source_media_id"] is None for row in legacy)
    finally:
        reopened.close()
