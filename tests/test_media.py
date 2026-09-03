import tempfile
import unittest
from pathlib import Path

from PIL import Image

from ig_monitor.config import AccountConfig, DedupConfig
from ig_monitor.db import Database
from ig_monitor.dedup import (
    MediaFingerprint,
    delete_quarantined_media,
    deduplicate_existing_media,
    fingerprint_file,
    is_similar,
    quality_rank,
    quarantine_cross_account_media,
    restore_quarantined_media,
)
from ig_monitor.media import save_avatar
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


class FakeScraper:
    async def download(self, url, referer):
        return b"\xff\xd8\xffsame-image", "image/jpeg"


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_account_quarantine_blocks_pending_shared_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "state.sqlite3")
            try:
                accounts = [
                    AccountConfig(f"https://insta-stories-viewer.com/p{index}/", True, f"p{index}")
                    for index in range(3)
                ]
                db.sync_accounts(accounts)
                snapshot = ProfileSnapshot("p", None, 1, 1, 1, "", PrivacyState.PUBLIC, "")
                for index, account in enumerate(db.enabled_accounts()):
                    db.record_success(account["id"], snapshot, [], [MediaCandidate(
                        f"pending-{index}", "posts", "video",
                        "https://cdn.example.test/unrelated-global-video.mp4",
                    )])

                report = quarantine_cross_account_media(db, apply=True, min_accounts=3)

                self.assertEqual(report["media_rows"], 3)
                statuses = [row[0] for row in db.conn.execute("SELECT status FROM media ORDER BY id")]
                self.assertEqual(statuses, ["quarantined"] * 3)
            finally:
                db.close()

    async def test_cross_account_quarantine_only_hides_videos_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "state.sqlite3")
            try:
                accounts = [
                    AccountConfig(f"https://insta-stories-viewer.com/a{index}/", True, f"a{index}")
                    for index in range(4)
                ]
                db.sync_accounts(accounts)
                rows = db.enabled_accounts()
                snapshot = ProfileSnapshot("a", None, 1, 1, 1, "", PrivacyState.PUBLIC, "")
                video_paths = []
                image_paths = []
                for index, account in enumerate(rows):
                    candidates = [
                        MediaCandidate(f"video-{index}", "posts", "video", f"https://cdn/{index}.mp4"),
                        MediaCandidate(f"image-{index}", "posts", "image", f"https://cdn/{index}.jpg"),
                    ]
                    db.record_success(account["id"], snapshot, [], candidates)
                    media = {
                        row["kind"]: row for row in db.conn.execute(
                            "SELECT * FROM media WHERE account_id=?", (account["id"],)
                        )
                    }
                    path = root / f"video-{index}.mp4"
                    path.write_bytes(b"shared-video" if index < 3 else b"unique-video")
                    video_paths.append(path)
                    db.mark_media_downloaded(
                        media["video"]["id"], str(path),
                        "shared-video-hash" if index < 3 else "unique-video-hash",
                    )
                    image_path = root / f"image-{index}.jpg"
                    image_path.write_bytes(b"shared-image" if index < 3 else b"unique-image")
                    image_paths.append(image_path)
                    db.mark_media_downloaded(
                        media["image"]["id"], str(image_path),
                        "shared-image-hash" if index < 3 else "unique-image-hash",
                    )

                preview = quarantine_cross_account_media(db, apply=False, min_accounts=3)
                self.assertEqual(preview["groups"], 1)
                self.assertEqual(preview["media_rows"], 3)
                self.assertEqual(preview["files"], 3)
                self.assertEqual(preview["kind"], "video")
                self.assertTrue(all(path.exists() for path in video_paths + image_paths))

                applied = quarantine_cross_account_media(db, apply=True, min_accounts=3)
                self.assertEqual(applied["media_rows"], 3)
                video_rows = db.conn.execute(
                    "SELECT id,status,local_path FROM media WHERE kind='video' ORDER BY id"
                ).fetchall()
                image_rows = db.conn.execute(
                    "SELECT status,local_path FROM media WHERE kind='image' ORDER BY id"
                ).fetchall()
                self.assertEqual([row["status"] for row in video_rows[:3]], ["quarantined"] * 3)
                self.assertEqual([row["local_path"] for row in video_rows[:3]], [str(p) for p in video_paths[:3]])
                self.assertEqual([row["status"] for row in image_rows], ["downloaded"] * 4)
                self.assertTrue(all(path.exists() for path in video_paths + image_paths))

                self.assertTrue(restore_quarantined_media(db, video_rows[0]["id"]))
                restored = db.conn.execute(
                    "SELECT status,local_path FROM media WHERE id=?", (video_rows[0]["id"],)
                ).fetchone()
                self.assertEqual((restored["status"], restored["local_path"]), ("downloaded", str(video_paths[0])))
                self.assertTrue(video_paths[0].exists())

                deleted = delete_quarantined_media(db, video_rows[1]["id"])
                self.assertTrue(deleted["deleted"])
                removed = db.conn.execute(
                    "SELECT status,local_path FROM media WHERE id=?", (video_rows[1]["id"],)
                ).fetchone()
                self.assertEqual((removed["status"], removed["local_path"]), ("deleted", None))
                self.assertFalse(video_paths[1].exists())
            finally:
                db.close()

    async def test_cross_account_quarantine_detects_reencoded_video_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "state.sqlite3")
            try:
                db.sync_accounts([
                    AccountConfig(f"https://insta-stories-viewer.com/f{index}/", True, f"f{index}")
                    for index in range(3)
                ])
                snapshot = ProfileSnapshot("f", None, 1, 1, 1, "", PrivacyState.PUBLIC, "")
                fingerprints = [
                    MediaFingerprint(
                        "video", 1080, 1920, 1000 + index, duration_seconds=12.0,
                        bitrate=800_000 + index, frame_phashes=("0f0f", "3333", "aaaa"),
                    )
                    for index in range(3)
                ]
                for index, account in enumerate(db.enabled_accounts()):
                    db.record_success(account["id"], snapshot, [], [MediaCandidate(
                        f"reencoded-{index}", "posts", "video", f"https://cdn/{index}-different.mp4",
                    )])
                    media_id = db.conn.execute(
                        "SELECT id FROM media WHERE account_id=?", (account["id"],)
                    ).fetchone()[0]
                    path = root / f"reencoded-{index}.mp4"
                    path.write_bytes(f"different-{index}".encode())
                    fingerprint = fingerprints[index]
                    db.mark_media_downloaded(
                        media_id, str(path), f"different-hash-{index}", fingerprint.to_json(),
                        fingerprint.width, fingerprint.height, fingerprint.size_bytes,
                        fingerprint.duration_seconds, fingerprint.bitrate,
                    )

                report = quarantine_cross_account_media(
                    db, apply=False, min_accounts=3,
                    dedup=DedupConfig(True, 4, 1.0, 1.0, 1.0),
                )

                self.assertEqual(report["groups"], 1)
                self.assertEqual(report["media_rows"], 3)
                self.assertEqual(report["samples"][0]["signal"], "perceptual")
            finally:
                db.close()

    async def test_unchanged_avatar_reuses_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_hash, first_path = await save_avatar(FakeScraper(), Path(tmp), "a", "https://cdn/a", "https://site/a")
            second_hash, second_path = await save_avatar(FakeScraper(), Path(tmp), "a", "https://cdn/a2", "https://site/a")
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_path, second_path)
            self.assertEqual(len(list((Path(tmp) / "a" / "avatar").iterdir())), 1)

    async def test_perceptual_dedup_keeps_highest_resolution_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            low = root / "low.png"
            high = root / "high.png"
            Image.new("RGB", (32, 32), "red").save(low)
            Image.new("RGB", (128, 128), "red").save(high)
            config = DedupConfig(True, 4, 1.0, 1.0, 1.0)
            low_fp = fingerprint_file(low, "image")
            high_fp = fingerprint_file(high, "image")
            self.assertTrue(is_similar(low_fp, high_fp, config))
            self.assertGreater(quality_rank(high_fp), quality_rank(low_fp))

            db = Database(root / "state.sqlite3")
            try:
                account = AccountConfig("https://insta-stories-viewer.com/a/", True, "a")
                db.sync_accounts([account])
                account_row = db.enabled_accounts()[0]
                snapshot = ProfileSnapshot("a", None, 0, 0, 0, "", PrivacyState.PUBLIC, "")
                candidates = [
                    MediaCandidate("low", "posts", "image", "https://cdn/low"),
                    MediaCandidate("high", "stories", "image", "https://cdn/high"),
                ]
                db.record_success(account_row["id"], snapshot, [], candidates)
                pending = db.pending_media(account_row["id"], 10)
                by_key = {item["media_key"]: item for item in pending}
                db.mark_media_downloaded(by_key["low"]["id"], str(low), "low-hash")
                db.mark_media_downloaded(by_key["high"]["id"], str(high), "high-hash")

                preview = deduplicate_existing_media(db, config, apply=False)
                self.assertEqual(preview["duplicate_rows"], 1)
                self.assertTrue(low.is_file())
                applied = deduplicate_existing_media(db, config, apply=True)
                self.assertEqual(applied["duplicate_rows"], 1)
                rows = db.conn.execute("SELECT id,media_key,status,duplicate_of_id FROM media ORDER BY id").fetchall()
                self.assertEqual(rows[0]["status"], "duplicate")
                self.assertEqual(rows[1]["status"], "downloaded")
                self.assertEqual(rows[0]["duplicate_of_id"], rows[1]["id"])
                self.assertFalse(low.exists())
                sources = {
                    row[0] for row in db.conn.execute(
                        "SELECT category FROM media_sources WHERE media_id=?", (rows[1]["id"],)
                    )
                }
                self.assertEqual(sources, {"posts", "stories"})
            finally:
                db.close()
