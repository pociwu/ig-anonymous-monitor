from __future__ import annotations

from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ig_monitor.config import AccountConfig
from ig_monitor.dashboard import (
    _collection_name, _collection_statuses, _format_taipei_time, _group_gallery,
    account_detail_data, create_app,
)
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


class GalleryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.current = None
        self.tabs = []
        self.external_assets = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "button" and "data-source" in attributes:
            self.tabs.append(attributes["data-source"])
        if tag == "article" and "gallery-entry" in attributes.get("class", "").split():
            self.current = {**attributes, "children": []}
            self.cards.append(self.current)
        if "data-media-id" in attributes and self.current is not None:
            self.current["children"].append(
                (int(attributes["data-media-id"]), int(attributes["data-position"]))
            )
        if tag in {"img", "video"} and attributes.get("src", "").startswith("http"):
            self.external_assets.append(attributes["src"])

    def handle_endtag(self, tag):
        if tag == "article":
            self.current = None


class AnonymousDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state.sqlite3"
        self.db = Database(self.db_path)
        self.addCleanup(self.db.close)
        self.db.sync_accounts([AccountConfig("https://insta-stories-viewer.com/gallery/", True, "gallery")])
        self.account_id = self.db.enabled_accounts()[0]["id"]
        self.snapshot = ProfileSnapshot(
            "gallery", "相簿測試", 12, 20, 30, "", PrivacyState.PUBLIC, "",
            observed_at="2026-09-05T00:00:00+00:00",
        )

    def candidate(self, key, category, parent=None, position=0, kind="image", **metadata):
        return MediaCandidate(
            key, category, kind, f"https://cdn.example/{key}?signed=secret",
            source="anonyig", source_media_id=metadata.pop("source_media_id", key),
            parent_id=parent, position=position, **metadata,
        )

    def save(self, candidates):
        self.db.record_success(self.account_id, self.snapshot, [], candidates)
        ids = {}
        for row in self.db.pending_media(self.account_id, 100):
            path = self.root / f"{row['id']}.bin"
            path.write_bytes(str(row["id"]).encode())
            self.db.mark_media_downloaded(row["id"], str(path), f"hash-{row['id']}")
            ids[row["media_key"]] = row["id"]
        return ids

    def page(self):
        return create_app(self.db_path).test_client().get(f"/account/{self.account_id}")

    def test_four_collections_keep_ordered_carousels_and_shared_canonical_memberships(self):
        ids = self.save([
            self.candidate("shared", "posts", "post-a", 2, source_media_id="child-2"),
            self.candidate("video", "posts", "post-a", 1, kind="video"),
            self.candidate("shared", "posts", "post-a", 0, source_media_id="child-0"),
            self.candidate("shared", "posts", "post-b"),
            self.candidate("video", "reels", "reel-a", kind="video"),
            self.candidate("shared", "stories", "story-a"),
            self.candidate("shared", "highlights", album_id="album-a", album_title="旅遊"),
            self.candidate("video", "highlights", position=1, kind="video", album_id="album-a", album_title="旅遊"),
            self.candidate("shared", "highlights", album_id="album-b", album_title="日常"),
        ])

        account, media, counts = account_detail_data(self.db_path, self.account_id)

        self.assertEqual(len(media), 2)
        self.assertEqual(counts["posts"]["all"], 2)
        self.assertEqual(counts["reels"]["all"], 1)
        self.assertEqual(account["gallery_counts"]["posts"], {"all": 2, "image": 2, "video": 1})
        self.assertEqual(account["gallery_counts"]["highlights"], {"all": 2, "image": 2, "video": 1})
        self.assertEqual(account["gallery_counts"]["stories"]["all"], 1)
        self.assertEqual(account["gallery_counts"]["reels"]["all"], 1)
        self.assertEqual(account["gallery_counts"]["legacy"]["all"], 0)
        post = next(group for group in account["gallery"] if group["group_id"] == "post-a")
        self.assertEqual([(child["id"], child["position"]) for child in post["children"]], [
            (ids["shared"], 0), (ids["video"], 1), (ids["shared"], 2),
        ])
        self.assertEqual(post["kinds"], ["image", "video"])

        response = self.page()
        self.assertEqual(response.status_code, 200)
        parser = GalleryParser()
        html = response.data.decode()
        parser.feed(html)
        self.assertEqual(parser.tabs, ["posts", "stories", "highlights", "reels", "legacy"])
        self.assertEqual(len(parser.cards), 6)
        self.assertEqual(parser.external_assets, [])
        self.assertNotIn("signed=secret", html)
        self.assertIn("旅遊", html)
        self.assertIn("日常", html)
        self.assertIn("el.dataset.kinds.split(' ').includes(selectedKind)", html)
        self.assertEqual(next(card for card in parser.cards if card["data-group-id"] == "post-a")["children"], [
            (ids["shared"], 0), (ids["video"], 1), (ids["shared"], 2),
        ])

    def test_unknown_legacy_grouping_is_not_inferred_and_categories_remain_accessible(self):
        ids = self.save([
            MediaCandidate("old", "posts", "image", "https://cdn.example/old", logical_id="unproven", position=0),
            MediaCandidate("old", "reels", "image", "https://cdn.example/old", logical_id="unproven", position=0),
            MediaCandidate("other", "posts", "image", "https://cdn.example/other", logical_id="unproven", position=1),
        ])
        account, media, _counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(len(media), 2)
        self.assertEqual(account["gallery_counts"]["posts"]["all"], 0)
        self.assertEqual(account["gallery_counts"]["legacy"]["all"], 2)
        old = next(group for group in account["gallery"] if group["children"][0]["id"] == ids["old"])
        self.assertEqual(old["categories"], ["posts", "reels"])
        self.assertTrue(all(len(group["children"]) == 1 for group in account["gallery"]))
        self.assertIn("舊版媒體 2", self.page().data.decode())
        self.assertEqual(_collection_name("reel"), "reels")
        self.assertEqual(_collection_name("unrecognized"), "legacy")

    def test_legacy_category_membership_survives_when_another_category_is_grouped(self):
        self.save([
            self.candidate("shared", "posts", "post-a"),
            MediaCandidate("shared", "highlights", "image", "https://cdn.example/shared"),
        ])
        account, _media, _counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(account["gallery_counts"]["posts"]["all"], 1)
        legacy = next(group for group in account["gallery"] if group["category"] == "legacy")
        self.assertEqual(legacy["categories"], ["highlights"])

    def test_quarantined_and_missing_files_stay_out_of_all_galleries(self):
        ids = self.save([
            self.candidate("safe", "posts", "post-a"),
            self.candidate("quarantine", "reels", "reel-a", kind="video"),
            self.candidate("missing", "highlights", album_id="album-a"),
            self.candidate("review", "posts", "post-review", kind="video"),
        ])
        self.db.conn.execute("UPDATE media SET status='quarantined' WHERE id=?", (ids["quarantine"],))
        self.db.conn.execute(
            """INSERT INTO media_quarantine(media_id,reason,signal,original_status,quarantined_at)
               VALUES(?, 'review', 'test', 'downloaded', '2026-09-05')""",
            (ids["review"],),
        )
        self.db.conn.commit()
        (self.root / f"{ids['missing']}.bin").unlink()
        account, media, _counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual([item["id"] for item in media], [ids["safe"]])
        self.assertEqual(len(account["gallery"]), 1)
        html = self.page().data.decode()
        self.assertNotIn(f'/media/{ids["quarantine"]}"', html)
        self.assertNotIn(f'/media/{ids["missing"]}"', html)
        self.assertNotIn(f'/media/{ids["review"]}"', html)

    def test_failed_partial_and_blocked_results_preserve_media_and_last_success(self):
        self.save([self.candidate("safe", "posts", "post-a")])
        success_at = "2026-09-05T01:00:00+00:00"
        self.db.record_collection_observations(self.account_id, "gallery", {
            category: {"state": "media", "complete": True}
            for category in ("posts", "stories", "highlights", "reels")
        }, now=success_at)
        self.db.record_collection_observations(self.account_id, "gallery", {
            "posts": {"state": "partial", "complete": False, "error": "第二頁失敗"},
            "stories": {"state": "blocked", "complete": False, "error": "驗證"},
            "highlights": {"state": "failed", "complete": False, "error": "逾時"},
            "reels": {"state": "empty", "complete": True},
        }, now="2026-09-05T02:00:00+00:00")
        account, media, _counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(len(media), 1)
        states = account["collection_observations"]
        for category, expected in (("posts", "partial"), ("stories", "blocked"), ("highlights", "error")):
            self.assertEqual(states[category]["display_state"], expected)
            self.assertEqual(states[category]["last_success_at"], success_at)
            self.assertIn("不能判定來源沒有內容", states[category]["empty_message"])
        self.assertEqual(states["reels"]["display_state"], "empty")
        html = self.page().data.decode()
        self.assertIn("來源確認無內容", html)
        self.assertIn("部分完成", html)
        self.assertIn("來源受阻", html)
        self.assertIn("巡檢失敗", html)
        self.assertIn(success_at, html)
        self.assertIn('datetime="2026-09-05T01:00:00+00:00">2026-09-05 09:00</time>', html)
        self.assertIn("台北時間（UTC+08:00）", html)

    def test_metadata_only_updates_do_not_hide_existing_files_or_claim_empty(self):
        self.save([self.candidate("saved", "posts", "post-a")])
        self.db.record_collection_observations(self.account_id, "gallery", {
            "posts": {"state": "media", "complete": True},
        }, persist_progress=False)
        account, media, _counts = account_detail_data(self.db_path, self.account_id)
        self.assertEqual(len(media), 1)
        self.assertEqual(account["gallery_counts"]["posts"]["all"], 1)
        self.assertNotEqual(account["collection_observations"]["posts"]["display_state"], "empty")

    def test_caption_album_and_error_are_escaped_without_remote_media_urls(self):
        attack = '</script><img src="https://example.invalid" onerror="alert(1)">'
        self.save([self.candidate("image", "highlights", album_id="album-a", album_title=attack, caption=attack)])
        self.db.record_collection_observations(self.account_id, "gallery", {
            "highlights": {"state": "failed", "error": attack},
        })
        html = self.page().data.decode()
        self.assertNotIn(attack, html)
        self.assertIn("&lt;/script&gt;", html)
        parser = GalleryParser()
        parser.feed(html)
        self.assertEqual(parser.external_assets, [])

    def test_get_before_new_schema_migration_is_read_only_with_legacy_fallback(self):
        self.save([MediaCandidate("old", "post", "image", "https://cdn.example/old")])
        self.db.conn.executescript("DROP TABLE media_memberships; DROP TABLE anonymous_collection_state;")
        self.assertEqual(self.page().status_code, 200)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('media_memberships','anonymous_collection_state')"
            ).fetchone()[0], 0)

    def test_pure_grouping_deduplicates_only_identical_membership_not_shared_file(self):
        path = self.root / "photo"
        path.write_bytes(b"photo")
        membership = {
            "source": "anonyig", "category": "posts", "group_id": "post-a",
            "source_media_id": "child", "position": 0, "media_id": 1,
            "kind": "image", "local_path": str(path),
        }
        gallery, counts = _group_gallery([], [membership, membership, {**membership, "position": 1}])
        self.assertEqual(len(gallery[0]["children"]), 2)
        self.assertEqual(counts["posts"]["all"], 1)
        self.assertEqual(_collection_statuses({})["posts"]["display_state"], "unknown")

    def test_incomplete_or_contradictory_empty_observation_does_not_claim_empty(self):
        for observation in (
            {"state": "empty", "complete": False},
            {"state": "empty", "complete": True, "error": "取得失敗"},
        ):
            status = _collection_statuses({"posts": observation})["posts"]
            self.assertNotEqual(status["display_state"], "empty")
            self.assertIn("不能判定來源沒有內容", status["empty_message"])

    def test_timestamp_formatter_uses_taipei_minutes_without_guessing_legacy_timezones(self):
        cases = {
            "2026-09-05T01:23:45+00:00": "2026-09-05 09:23",
            "2026-09-05T17:00:00Z": "2026-09-06 01:00",
            "2026-09-05T09:23:59+08:00": "2026-09-05 09:23",
            "2026-09-05T23:30:00-05:00": "2026-09-06 12:30",
            "2026-09-05T09:23:00": "2026-09-05T09:23:00",
            "unknown": "unknown",
            None: "—",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(_format_taipei_time(original), expected)


if __name__ == "__main__":
    unittest.main()
