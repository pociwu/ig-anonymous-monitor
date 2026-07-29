import tempfile
import unittest
from pathlib import Path

from PIL import Image

from ig_monitor.config import AccountConfig, DedupConfig
from ig_monitor.db import Database
from ig_monitor.dedup import deduplicate_existing_media, fingerprint_file, is_similar, quality_rank
from ig_monitor.media import save_avatar
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


class FakeScraper:
    async def download(self, url, referer):
        return b"\xff\xd8\xffsame-image", "image/jpeg"


class MediaTests(unittest.IsolatedAsyncioTestCase):
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
