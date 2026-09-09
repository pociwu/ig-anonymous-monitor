"""Opt-in loopback-only QA of pending authorship and preserved carousel versions."""
from __future__ import annotations

from contextlib import chdir, closing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlparse

from werkzeug.serving import make_server

from ig_monitor.config import AccountConfig
from ig_monitor.dashboard import create_app
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, MediaGroupObservation, PrivacyState, ProfileSnapshot


@unittest.skipUnless(os.getenv("IG_MONITOR_BROWSER_TESTS") == "1", "Opt-in local browser QA")
class IGWatcherDashboardBrowserTests(unittest.TestCase):
    def test_pending_filters_versions_and_counts_on_desktop_and_mobile(self):
        from playwright.sync_api import expect, sync_playwright

        screenshots = Path(__file__).resolve().parents[1] / ".pytest-tmp" / "igwatcher-dashboard"
        screenshots.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="igwatcher-dashboard-") as temporary:
            root = Path(temporary)
            db_path = root / "isolated.sqlite3"
            account_id = self._database(db_path, root)
            app = create_app(db_path, lambda: {"monitor": "隔離測試", "timer": "未啟動", "next_run": "無"})
            server = make_server("127.0.0.1", 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with chdir(screenshots), sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(viewport={"width": 1280, "height": 1000})
                        unexpected_requests = []

                        def local_only(route):
                            parsed = urlparse(route.request.url)
                            if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
                                route.continue_()
                            else:
                                unexpected_requests.append(route.request.url)
                                route.abort()

                        context.route("**/*", local_only)
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}/account/{account_id}")
                        cards = page.locator(".gallery-entry:visible")
                        expect(cards).to_have_count(1)
                        expect(cards).to_have_attribute("data-group-id", "ordinary-post")
                        page.locator(".media-photo:visible").click()
                        expect(page.locator("#lightbox-counter")).to_have_text("1 / 1")
                        page.keyboard.press("Escape")

                        page.locator('.source-tabs [data-source="pending"]').click()
                        expect(cards).to_have_count(1)
                        expect(cards).to_have_attribute("data-group-id", "carousel")
                        expect(page.locator('[data-status-source="pending"]')).to_contain_text("查詢帳號不等於作者")
                        expect(cards).to_contain_text("本地子項目識別（非 Instagram ID）")
                        self.assertEqual(cards.locator('.media-child').evaluate_all(
                            "items => items.map(item => Number(item.dataset.position))"
                        ), [0, 1])
                        cards.locator(".media-photo").first.click()
                        expect(page.locator("#lightbox-counter")).to_have_text("1 / 2")
                        page.keyboard.press("Escape")
                        cards.locator(".group-history summary").click()
                        expect(cards.locator(".history-media:visible")).to_have_count(2)
                        cards.locator(".media-photo").first.click()
                        expect(page.locator("#lightbox-counter")).to_have_text("1 / 2")
                        page.keyboard.press("Escape")
                        page.screenshot(path=str(screenshots / "desktop-pending-posts.png"), full_page=True)

                        for category in ("stories", "reels"):
                            page.locator(f'.pending-tabs [data-pending-category="{category}"]').click()
                            expect(cards).to_have_count(1)
                            expect(cards).to_have_attribute("data-pending-category", category)
                        page.locator('.pending-tabs [data-pending-category="highlights"]').click()
                        expect(cards).to_have_count(2)
                        album = page.locator('[data-group-id="album"]')
                        expect(album).to_contain_text("來源宣告 10 · 本次回傳 9 · 本地已保存 2")
                        expect(album).to_contain_text("完整性未確認")
                        expect(album).to_contain_text("來源曾有數量缺口")
                        expect(page.locator('[data-group-id="metadata"]')).to_contain_text("本地已保存 0")
                        page.screenshot(path=str(screenshots / "desktop-pending-highlights.png"), full_page=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                        page.screenshot(path=str(screenshots / "mobile-pending-highlights.png"), full_page=True)

                        page.locator('.source-tabs [data-source="legacy"]').click()
                        expect(cards).to_have_count(0)
                        page.locator('.source-tabs [data-source="stories"]').click()
                        expect(cards).to_have_count(0)
                        expect(page.locator('.pending-tabs')).to_be_hidden()
                        self.assertEqual(errors, [])
                        self.assertEqual(unexpected_requests, [])
                        context.close()
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    @staticmethod
    def _database(db_path, root):
        from PIL import Image

        with closing(Database(db_path)) as db:
            db.sync_accounts([AccountConfig("https://insta-stories-viewer.com/nasa/", True, "NASA")])
            account_id = db.enabled_accounts()[0]["id"]
            snapshot = ProfileSnapshot("nasa", "NASA・隔離展示", 12, 20, 30, "", PrivacyState.PUBLIC, "")

            def save(items):
                db.record_success(account_id, snapshot, [], items)
                ids = {}
                for row in db.pending_media(account_id, 100):
                    path = root / f"{row['id']}.png"
                    Image.new("RGB", (600, 600), ["#375C85", "#A05F6A", "#59877A"][row["id"] % 3]).save(path)
                    db.mark_media_downloaded(row["id"], str(path), f"fixture-{row['id']}")
                    ids[row["media_key"]] = row["id"]
                return ids

            def candidate(key, category="posts", group="carousel", position=0, revision=""):
                return MediaCandidate(
                    key, category, "image", f"https://example.invalid/{key}", source="igwatcher",
                    source_media_id=key, parent_id=group if category != "highlights" else None,
                    album_id=group if category == "highlights" else None,
                    album_title="NASA 精華" if category == "highlights" else None,
                    ownership_status="pending", queried_username="nasa", position=position,
                    identity_kind="local" if category == "posts" else "source", revision_id=revision,
                )

            old = save([
                MediaCandidate("ordinary", "posts", "image", "https://example.invalid/ordinary", source="anonyig", source_media_id="ordinary", parent_id="ordinary-post"),
                candidate("old-a", position=0, revision="old"), candidate("old-b", position=1, revision="old"),
            ])
            new = save([
                candidate("new-b", position=0, revision="current"), candidate("new-a", position=1, revision="current"),
                candidate("story", "stories", "story"), candidate("reel", "reels", "reel"),
                candidate("highlight-a", "highlights", "album"), candidate("highlight-b", "highlights", "album", 1),
            ])
            db.mark_media_duplicate(new["new-b"], old["old-b"], "same-b")
            db.mark_media_duplicate(new["new-a"], old["old-a"], "same-a")
            db.record_group_observations(account_id, "igwatcher", [
                MediaGroupObservation("posts", "carousel", "old", 2, 2, "2026-09-08T10:00:00+00:00"),
                MediaGroupObservation("posts", "carousel", "current", 2, 2, "2026-09-09T10:00:00+00:00"),
                MediaGroupObservation("highlights", "album", "album-current", 10, 9, "2026-09-09T10:00:00+00:00", album_title="NASA 精華"),
                MediaGroupObservation("highlights", "metadata", "metadata-current", 5, 5, "2026-09-09T10:00:00+00:00", album_title="尚未下載的專輯"),
            ])
            db.set_meta(f"anonymous_active_source:{account_id}", "igwatcher")
            return account_id
