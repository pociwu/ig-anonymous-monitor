import tempfile
import unittest
from pathlib import Path

from ig_monitor.config import AccountConfig
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.sqlite3")
        self.account = AccountConfig("https://insta-stories-viewer.com/a/", True, "a")
        self.db.sync_accounts([self.account])
        self.row = self.db.enabled_accounts()[0]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_failure_alert_only_after_three_runs(self):
        self.db.record_failure(self.row["id"], "a", "err", None)
        self.db.record_failure(self.row["id"], "a", "err", None)
        self.assertEqual(self.db.pending_events(10), [])
        self.db.record_failure(self.row["id"], "a", "err", "CAPTCHA")
        events = self.db.pending_events(10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "failure")

    def test_snapshot_and_initial_event_are_atomic(self):
        snapshot = ProfileSnapshot("a", None, 1, 2, 3, "bio", PrivacyState.PRIVATE,
                                   "https://cdn/a", "hash", "/a.jpg", "now")
        self.db.record_success(self.row["id"], snapshot,
                               [("initial:1", "initial", {"label": "a", "snapshot": snapshot.to_dict()})], [])
        row = self.db.get_account("a")
        self.assertEqual(self.db.snapshot_from_row(row).followers, 2)
        self.assertEqual(len(self.db.pending_events(10)), 1)
        self.assertTrue(self.db.reset_account("a"))
        self.assertIsNone(self.db.snapshot_from_row(self.db.get_account("a")))
        self.assertEqual(self.db.pending_events(10), [])


    def test_identity_and_effective_url_survive_config_sync(self):
        self.db.set_identity(self.row["id"], "123", "renamed")
        self.db.sync_accounts([self.account])
        row = self.db.get_account("a")
        self.assertEqual(row["instagram_profile_id"], "123")
        self.assertEqual(row["effective_url"], "https://insta-stories-viewer.com/renamed/")

    def test_apify_budget_notice_is_once_per_cycle(self):
        self.assertFalse(self.db.budget_notice_sent("2026-07-01"))
        self.db.mark_budget_notice_sent("2026-07-01")
        self.assertTrue(self.db.budget_notice_sent("2026-07-01"))
        self.assertFalse(self.db.budget_notice_sent("2026-08-01"))

    def test_summary_pending_only_counts_downloadable_media(self):
        snapshot = ProfileSnapshot("a", None, 1, 2, 3, "bio", PrivacyState.PUBLIC,
                                   None, None, None, "now")
        media = [
            MediaCandidate("pending", "posts", "image", "https://cdn/pending.jpg"),
            MediaCandidate("failed", "posts", "image", "https://cdn/failed.jpg"),
            MediaCandidate("downloaded", "posts", "image", "https://cdn/downloaded.jpg"),
            MediaCandidate("duplicate", "posts", "image", "https://cdn/duplicate.jpg"),
        ]
        self.db.record_success(self.row["id"], snapshot, [], media)
        rows = {item["media_key"]: item for item in self.db.pending_media(self.row["id"], 10)}
        self.db.mark_media_failed(rows["failed"]["id"], "temporary error")
        self.db.mark_media_downloaded(rows["downloaded"]["id"], "/downloaded.jpg", "hash-1")
        self.db.mark_media_downloaded(rows["duplicate"]["id"], "/duplicate.jpg", "hash-2")
        self.db.mark_media_duplicate(rows["duplicate"]["id"], rows["downloaded"]["id"], "hash-1")

        self.assertEqual(self.db.summary()["pending"], 2)


if __name__ == "__main__":
    unittest.main()
