from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from ig_monitor.config import AccountConfig, DedupConfig
from ig_monitor.db import Database
from ig_monitor.dedup import deduplicate_existing_media, sha256_file
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


@pytest.fixture
def archive(tmp_path):
    db = Database(tmp_path / "state.sqlite3")
    db.sync_accounts([AccountConfig("https://instagram.com/alice/", True, "Alice")])
    yield db, tmp_path, db.enabled_accounts()[0]["id"]
    db.close()


def picture(size=48):
    output = BytesIO()
    Image.new("RGB", (size, size), (120, 50, 90)).save(output, format="PNG")
    return output.getvalue()


def save(archive, key, payload, *, pending=True):
    db, directory, account_id = archive
    item = MediaCandidate(
        key, "posts", "image", f"https://cdn.example/{key}", parent_id="parent",
        source="igwatcher" if pending else "legacy", ownership_status="pending" if pending else "unspecified",
        identity_kind="local" if pending else "source", revision_id=key if pending else "",
    )
    db.record_success(account_id, ProfileSnapshot("alice", "Alice", 12, 20, 30, "", PrivacyState.PUBLIC, ""), [], [item])
    row, = db.pending_media(account_id, 100)
    path = directory / f"{key}.png"
    path.write_bytes(payload)
    db.mark_media_downloaded(row["id"], str(path), sha256_file(path))
    return row["id"], path


def test_maintenance_preserves_visually_similar_pending_observation_bytes(archive):
    db, directory, account_id = archive
    first, old_path = save(archive, "old", picture())
    second, new_path = save(archive, "new", picture(96))
    report = deduplicate_existing_media(db, DedupConfig(True, 4, 1.0, 1.0, 1.0), apply=True)
    assert report["duplicate_rows"] == 0
    assert old_path.read_bytes() == picture()
    assert new_path.read_bytes() == picture(96)
    assert {row["media_id"] for row in db.gallery_memberships(account_id)} == {first, second}


def test_later_pending_exact_match_cannot_join_a_preformed_mixed_byte_legacy_group(archive):
    db, directory, account_id = archive
    save(archive, "legacy-low", picture(), pending=False)
    save(archive, "legacy-high", picture(96), pending=False)
    pending, _ = save(archive, "pending-last", picture())
    report = deduplicate_existing_media(db, DedupConfig(True, 4, 1.0, 1.0, 1.0), apply=True)
    review, = [row for row in db.gallery_memberships(account_id) if row["ownership_status"] == "pending"]
    assert Path(review["local_path"]).read_bytes() == picture()
    assert {path.read_bytes() for path in directory.glob("*.png")} == {picture(), picture(96)}
    assert report["duplicate_rows"] == 1


def test_maintenance_rechecks_actual_bytes_instead_of_trusting_stale_hashes(archive):
    db, directory, account_id = archive
    _, original = save(archive, "original", picture())
    _, changed = save(archive, "changed-after-download", picture())
    changed.write_bytes(picture(96))
    report = deduplicate_existing_media(db, DedupConfig(True, 4, 1.0, 1.0, 1.0), apply=True)
    assert report["duplicate_rows"] == 0
    assert original.read_bytes() == picture()
    assert changed.read_bytes() == picture(96)
