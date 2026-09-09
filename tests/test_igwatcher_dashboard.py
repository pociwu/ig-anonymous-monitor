from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ig_monitor.config import AccountConfig
from ig_monitor.dashboard import account_detail_data, create_app, dashboard_data
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, MediaGroupObservation, PrivacyState, ProfileSnapshot


class IGWatcherDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state.sqlite3"
        self.db = Database(self.db_path)
        self.addCleanup(self.db.close)
        self.db.sync_accounts([AccountConfig("https://insta-stories-viewer.com/nasa/", True, "NASA")])
        self.account_id = self.db.enabled_accounts()[0]["id"]
        self.snapshot = ProfileSnapshot("nasa", "NASA", 12, 20, 30, "", PrivacyState.PUBLIC, "")

    def candidate(self, key, category="posts", group="parent", **metadata):
        return MediaCandidate(
            key, category, "image", f"https://cdn.example/{key}?secret=remote-only",
            source="igwatcher", source_media_id=key,
            parent_id=group if category != "highlights" else None,
            album_id=group if category == "highlights" else None,
            ownership_status="pending", queried_username="nasa", **metadata,
        )

    def save(self, candidates):
        self.db.record_success(self.account_id, self.snapshot, [], candidates)
        result = {}
        for row in self.db.pending_media(self.account_id, 100):
            path = self.root / f"{row['id']}.png"
            path.write_bytes(b"local fixture")
            self.db.mark_media_downloaded(row["id"], str(path), f"hash-{row['id']}")
            result[row["media_key"]] = row["id"]
        return result

    def test_pending_authorship_is_separate_from_ordinary_gallery_and_download_count(self):
        ids = self.save([self.candidate("unknown-author", identity_kind="local", revision_id="rev-a")])

        account, media, counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(media, [])
        self.assertEqual(counts["posts"]["all"], 0)
        self.assertEqual(account["gallery"], [])
        self.assertEqual(account["gallery_counts"]["legacy"]["all"], 0)
        self.assertEqual(account["pending_gallery_counts"]["posts"]["all"], 1)
        self.assertEqual(account["pending_gallery"][0]["children"][0]["id"], ids["unknown-author"])
        overview = dashboard_data(self.db_path, lambda: {})["accounts"][0]
        self.assertEqual(overview["downloaded"], 0)
        self.assertEqual(overview["ownership_pending"], 1)

        response = create_app(self.db_path).test_client().get(f"/account/{self.account_id}")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode()
        self.assertIn('data-source="pending"', html)
        self.assertIn('data-collection="pending"', html)
        self.assertIn("歸屬待確認", html)
        self.assertIn("查詢帳號不等於作者", html)
        self.assertIn("本地子項目識別（非 Instagram ID）", html)
        self.assertIn("IGWatcher", html)
        self.assertNotIn("secret=remote-only", html)

    def test_reordered_carousel_shows_current_order_and_keeps_history_separate(self):
        old = self.save([
            self.candidate("old-a", position=0, revision_id="old", identity_kind="local"),
            self.candidate("old-b", position=1, revision_id="old", identity_kind="local"),
        ])
        self.db.record_group_observations(self.account_id, "igwatcher", [
            MediaGroupObservation("posts", "parent", "old", 2, 2, "2026-09-08T10:00:00+00:00"),
        ])
        new = self.save([
            self.candidate("new-b", position=0, revision_id="new", identity_kind="local"),
            self.candidate("new-a", position=1, revision_id="new", identity_kind="local"),
        ])
        self.db.mark_media_duplicate(new["new-b"], old["old-b"], "same-b")
        self.db.mark_media_duplicate(new["new-a"], old["old-a"], "same-a")
        self.db.record_group_observations(self.account_id, "igwatcher", [
            MediaGroupObservation("posts", "parent", "new", 2, 2, "2026-09-09T10:00:00+00:00"),
            MediaGroupObservation("posts", "parent", "old", 2, 2, "2026-09-08T11:00:00+00:00"),
            MediaGroupObservation("posts", "parent", "new", 2, 2, "2026-09-09T11:00:00+00:00"),
        ])

        account, media, _counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(media, [])
        self.assertEqual(len(account["pending_gallery"]), 1)
        group = account["pending_gallery"][0]
        self.assertEqual([child["id"] for child in group["children"]], [old["old-b"], old["old-a"]])
        self.assertEqual(group["revision_id"], "new")
        self.assertEqual(len(group["history"]), 1)
        self.assertEqual([child["id"] for child in group["history"][0]["children"]], [old["old-a"], old["old-b"]])
        html = create_app(self.db_path).test_client().get(f"/account/{self.account_id}").data.decode()
        self.assertIn("歷史觀測版本（1）", html)
        self.assertIn("不併入目前輪播", html)

    def test_highlight_counts_preserve_old_items_and_incompleteness_after_later_equal_counts(self):
        self.save([self.candidate(f"story-{index}", "highlights", "album", position=index) for index in range(10)])
        self.db.record_group_observations(self.account_id, "igwatcher", [
            MediaGroupObservation("highlights", "album", "missing-one", 10, 9, "2026-09-08T10:00:00+00:00", album_title="NASA 精華"),
        ])
        account, _, _ = account_detail_data(self.db_path, self.account_id)
        album = account["pending_gallery"][0]
        self.assertEqual(len(album["children"]), 10)
        html = create_app(self.db_path).test_client().get(f"/account/{self.account_id}").data.decode()
        self.assertIn("來源宣告 10", html)
        self.assertIn("本次回傳 9", html)
        self.assertIn("本地已保存 10", html)
        self.assertIn("完整性未確認", html)
        self.db.record_group_observations(self.account_id, "igwatcher", [
            MediaGroupObservation("highlights", "album", "later-equal", 9, 9, "2026-09-09T10:00:00+00:00", album_title="NASA 精華"),
        ])
        html = create_app(self.db_path).test_client().get(f"/account/{self.account_id}").data.decode()
        self.assertIn("來源曾有數量缺口", html)
        self.assertIn("不視為已確認刪除", html)
        self.assertIn("本地已保存 10", html)

    def test_metadata_only_group_does_not_claim_returned_items_were_downloaded(self):
        self.db.record_success(self.account_id, self.snapshot, [], [])
        self.db.record_group_observations(self.account_id, "igwatcher", [
            MediaGroupObservation("highlights", "album", "metadata", 10, 9, "2026-09-09T10:00:00+00:00", album_title="僅中繼資料"),
        ])
        account, media, _ = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(media, [])
        self.assertEqual(account["pending_gallery"][0]["children"], [])
        html = create_app(self.db_path).test_client().get(f"/account/{self.account_id}").data.decode()
        self.assertIn("本地已保存 0", html)
        self.assertIn("本次回傳 9", html)
        self.assertNotIn("本地已下載 9", html)

    def test_active_source_status_and_pending_shared_file_do_not_relabel_other_membership(self):
        ordinary = MediaCandidate("ordinary", "posts", "image", "https://cdn.example/ordinary", source="anonyig", source_media_id="trusted", parent_id="trusted-post")
        ids = self.save([ordinary, self.candidate("pending", "reels", "unknown-reel")])
        self.db.mark_media_duplicate(ids["pending"], ids["ordinary"], "same")
        self.db.set_meta(f"anonymous_active_source:{self.account_id}", "igwatcher")
        self.db.record_collection_observations(self.account_id, "nasa", {
            "posts": {"state": "blocked", "error": "old source blocked"},
        }, source="anonyig", now="2026-09-09T11:00:00+00:00")
        self.db.record_collection_observations(self.account_id, "nasa", {
            "posts": {"state": "partial", "error": None, "complete": False},
        }, source="igwatcher", now="2026-09-09T10:00:00+00:00")
        account, media, counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(media[0]["categories"], ["posts"])
        self.assertEqual(counts["reels"]["all"], 0)
        self.assertEqual(account["gallery_counts"]["posts"]["all"], 1)
        self.assertEqual(account["pending_gallery_counts"]["reels"]["all"], 1)
        self.assertEqual(account["collection_observations"]["posts"]["display_state"], "partial")
        self.assertEqual(account["anonymous_source_label"], "IGWatcher")
        overview = dashboard_data(self.db_path, lambda: {})["accounts"][0]
        self.assertEqual(overview["downloaded"], 1)
        self.assertEqual(overview["ownership_pending"], 1)

    def test_matching_counts_remain_unverified_without_claiming_a_count_gap(self):
        self.db.record_success(self.account_id, self.snapshot, [], [])
        self.db.record_group_observations(self.account_id, "igwatcher", [
            MediaGroupObservation("highlights", "album", "equal", 9, 9, "2026-09-09T10:00:00+00:00"),
        ])
        html = create_app(self.db_path).test_client().get(f"/account/{self.account_id}").data.decode()
        self.assertTrue("完整性未確認" in html)
        self.assertFalse("來源曾有數量缺口" in html)

    def test_highlight_reordering_does_not_duplicate_preserved_source_items(self):
        ids = self.save([
            self.candidate("first", "highlights", "album", position=0),
            self.candidate("second", "highlights", "album", position=1),
        ])
        self.save([
            self.candidate("first", "highlights", "album", position=1),
            self.candidate("second", "highlights", "album", position=0),
        ])
        account, _, _ = account_detail_data(self.db_path, self.account_id)
        self.assertEqual([item["id"] for item in account["pending_gallery"][0]["children"]], [ids["second"], ids["first"]])
        self.db.mark_media_duplicate(ids["second"], ids["first"], "same-content-different-items")
        account, _, _ = account_detail_data(self.db_path, self.account_id)
        self.assertEqual([item["id"] for item in account["pending_gallery"][0]["children"]], [ids["first"], ids["first"]])
        self.assertEqual([item["source_media_id"] for item in account["pending_gallery"][0]["children"]], ["second", "first"])


if __name__ == "__main__":
    unittest.main()
