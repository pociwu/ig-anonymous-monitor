"""Opt-in, loopback-only gallery QA with generated media and an isolated database.

Run with IG_MONITOR_BROWSER_TESTS=1. No production configuration or data is read.
Screenshots are written under .pytest-tmp/anonymous-dashboard for visual review.
"""
from __future__ import annotations

import base64
from contextlib import chdir
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
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


@unittest.skipUnless(os.getenv("IG_MONITOR_BROWSER_TESTS") == "1", "Opt-in local browser QA")
class AnonymousDashboardBrowserTests(unittest.TestCase):
    def test_local_gallery_desktop_and_mobile(self):
        from playwright.sync_api import expect, sync_playwright

        screenshots = Path(__file__).resolve().parents[1] / ".pytest-tmp" / "anonymous-dashboard"
        screenshots.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ig-dashboard-browser-") as temporary:
            root = Path(temporary)
            db_path = root / "fake.sqlite3"
            # Chromium child-process diagnostics use the launch working directory.
            with chdir(screenshots), sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    self._make_video_fixture(browser, root / "demo.webm")
                    account_id = self._make_database(db_path, root)
                    app = create_app(db_path, lambda: {
                        "monitor": "隔離示範", "timer": "未啟動", "next_run": "無排程",
                    })
                    server = make_server("127.0.0.1", 0, app, threaded=True)
                    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                    server_thread.start()
                    try:
                        context = browser.new_context(viewport={"width": 1280, "height": 1000})
                        forbidden_requests = []

                        def loopback_only(route):
                            parsed = urlparse(route.request.url)
                            if parsed.hostname == "127.0.0.1" and parsed.port == server.server_port:
                                route.continue_()
                            else:
                                forbidden_requests.append(route.request.url)
                                route.abort()

                        context.route("**/*", loopback_only)
                        page = context.new_page()
                        script_errors = []
                        page.on("pageerror", lambda error: script_errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{server.server_port}/account/{account_id}")
                        expect(page.get_by_role("heading", name="隔離示範帳號")).to_be_visible()
                        expect(page.get_by_text("正式媒體下載已啟用", exact=False)).to_be_visible()

                        cards = page.locator(".gallery-entry:visible")
                        expect(cards).to_have_count(2)
                        carousel = page.locator('[data-group-id="post-carousel"]')
                        self.assertEqual(carousel.locator(".media-child").evaluate_all(
                            "items => items.map(item => Number(item.dataset.position))"
                        ), [0, 1, 2])
                        expect(page.locator('[data-status-source="posts"]')).to_contain_text("部分完成")
                        expect(page.locator('[data-status-source="posts"]')).to_contain_text("2026-09-05 09:00")
                        expect(page.locator('[data-status-source="posts"] time').first).to_have_attribute("datetime", "2026-09-05T01:00:00+00:00")
                        self._capture(page, screenshots / "desktop-posts.png")

                        previous_item = carousel.locator('[data-carousel-step="-1"]')
                        next_item = carousel.locator('[data-carousel-step="1"]')
                        expect(previous_item).to_be_disabled()
                        next_item.click()
                        expect(previous_item).to_be_enabled()
                        page.wait_for_function("""() => {
                            const track=document.querySelector('[data-group-id="post-carousel"] .media-children');
                            return track.scrollLeft >= track.clientWidth - 3;
                        }""")
                        previous_item.click()
                        expect(previous_item).to_be_disabled()

                        # Mixed-media filtering retains every ordered child in a matching card.
                        page.locator('.kind-tabs [data-kind="video"]').click()
                        expect(cards).to_have_count(1)
                        expect(carousel.locator(".media-child")).to_have_count(3)
                        expect(page.locator('.kind-tabs [data-kind="video"]')).to_have_text("影片 1")
                        page.locator('.kind-tabs [data-kind="all"]').click()
                        carousel.locator(".media-photo").first.click()
                        expect(page.locator("#photo-lightbox")).to_be_visible()
                        expect(page.locator("#lightbox-counter")).to_have_text("2 / 3")
                        first_image = page.locator("#lightbox-image").get_attribute("src")
                        page.keyboard.press("ArrowRight")
                        expect(page.locator("#lightbox-counter")).to_have_text("3 / 3")
                        self.assertNotEqual(page.locator("#lightbox-image").get_attribute("src"), first_image)
                        page.locator('.lightbox-nav[data-lightbox-action="previous"]').click()
                        expect(page.locator("#lightbox-counter")).to_have_text("2 / 3")
                        page.locator('.lightbox-nav[data-lightbox-action="next"]').click()
                        expect(page.locator("#lightbox-counter")).to_have_text("3 / 3")
                        page.locator("#lightbox-slideshow").click()
                        expect(page.locator("#lightbox-slideshow")).to_have_attribute("aria-pressed", "true")
                        page.locator("#lightbox-slideshow").click()
                        self._capture(page, screenshots / "desktop-lightbox.png", full_page=False)
                        page.keyboard.press("Escape")
                        expect(page.locator("#photo-lightbox")).to_be_hidden()

                        for category, expected_count, state_text in (
                            ("stories", 1, "來源受阻"),
                            ("highlights", 2, "已更新"),
                            ("reels", 1, "巡檢失敗"),
                            ("legacy", 1, "缺少可靠貼文或專輯歸屬"),
                        ):
                            page.locator(f'.source-tabs [data-source="{category}"]').click()
                            expect(cards).to_have_count(expected_count)
                            self.assertEqual(cards.evaluate_all(
                                "items => [...new Set(items.map(item => item.dataset.collection))]"
                            ), [category])
                            expect(page.locator(f'[data-status-source="{category}"]')).to_contain_text(state_text)
                            expect(page.locator(f'.source-tabs [data-source="{category}"]')).to_have_attribute("aria-pressed", "true")
                            self._capture(page, screenshots / f"desktop-{category}.png")

                        # A no-match media filter reports the filter condition, not an empty source.
                        page.locator('.kind-tabs [data-kind="video"]').click()
                        expect(cards).to_have_count(0)
                        expect(page.locator("#gallery-empty")).to_have_text("目前沒有符合此媒體類型的卡片。")
                        page.locator('.kind-tabs [data-kind="all"]').click()

                        page.set_viewport_size({"width": 390, "height": 844})
                        for category in ("posts", "highlights"):
                            page.locator(f'.source-tabs [data-source="{category}"]').click()
                            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                            boxes = cards.evaluate_all("items => items.map(item => ({x: item.getBoundingClientRect().x, width: item.getBoundingClientRect().width}))")
                            self.assertTrue(all(box["x"] >= 0 and box["x"] + box["width"] <= 390 for box in boxes))
                            self._capture(page, screenshots / f"mobile-{category}.png")

                        album = page.locator('[data-group-id="album-travel"]')
                        album.locator("summary").click()
                        expect(album.locator("details")).not_to_have_attribute("open", "")
                        album.locator("summary").click()
                        album.locator(".media-photo").first.click()
                        expect(page.locator("#photo-lightbox")).to_be_visible()
                        self.assertLessEqual(page.locator(".lightbox-panel").bounding_box()["width"], 390)
                        self._capture(page, screenshots / "mobile-lightbox.png", full_page=False)
                        page.keyboard.press("Escape")
                        self.assertEqual(script_errors, [])
                        self.assertEqual(forbidden_requests, [])
                        context.close()
                    finally:
                        server.shutdown()
                        server.server_close()
                        server_thread.join(timeout=5)
                        self.assertFalse(server_thread.is_alive())
                finally:
                    browser.close()

    @staticmethod
    def _capture(page, path, *, full_page=True):
        if os.getenv("IG_MONITOR_BROWSER_REPRESENTATIVE_ONLY") == "1" and path.name not in {
            "desktop-posts.png", "desktop-highlights.png", "mobile-posts.png",
        }:
            return
        # Wait on the local visible images, not a fixed delay or external network idle.
        page.locator(".gallery-entry:visible img").evaluate_all(
            "images => Promise.all(images.map(image => image.complete ? Promise.resolve() : image.decode().catch(() => {})))"
        )
        page.screenshot(path=str(path), full_page=full_page)

    @staticmethod
    def _make_video_fixture(browser, path):
        fixture_page = browser.new_page()
        try:
            encoded = fixture_page.evaluate("""async () => {
                const canvas = document.createElement('canvas'); canvas.width=240; canvas.height=240;
                const context = canvas.getContext('2d'); context.fillStyle='#713f12';
                context.fillRect(0,0,240,240); context.fillStyle='#fef3c7';
                context.font='24px sans-serif'; context.fillText('DEMO VIDEO',38,128);
                const stream=canvas.captureStream(5), chunks=[];
                const recorder=new MediaRecorder(stream,{mimeType:'video/webm'});
                const complete=new Promise(resolve => recorder.onstop=resolve);
                recorder.ondataavailable=event=>chunks.push(event.data);
                recorder.start(); await new Promise(resolve=>setTimeout(resolve,350)); recorder.stop();
                await complete; stream.getTracks().forEach(track=>track.stop());
                const bytes=new Uint8Array(await new Blob(chunks,{type:'video/webm'}).arrayBuffer());
                return btoa(String.fromCharCode(...bytes));
            }""")
            path.write_bytes(base64.b64decode(encoded))
        finally:
            fixture_page.close()

    @staticmethod
    def _make_database(db_path, root):
        db = Database(db_path)
        try:
            db.sync_accounts([AccountConfig("https://www.instagram.com/demo_gallery/", True, "隔離示範")])
            account_id = db.enabled_accounts()[0]["id"]
            snapshot = ProfileSnapshot(
                "demo_gallery", "隔離示範帳號", 12, 123, 45,
                "此頁完全使用人工示意資料，未連線至 Instagram 或匿名來源。",
                PrivacyState.PUBLIC, "", observed_at="2026-09-05T01:00:00+00:00",
            )

            def item(key, category, group=None, position=0, kind="image", **metadata):
                return MediaCandidate(
                    key, category, kind, f"https://unused.invalid/{key}",
                    parent_id=group, position=position, source="anonyig", source_media_id=key,
                    published_at="2026-09-05T01:00:00+00:00", **metadata,
                )

            db.record_success(account_id, snapshot, [], [
                item("one", "posts", "post-carousel", 0, caption="保留原順序的示意輪播：照片 → 影片 → 照片"),
                item("video", "posts", "post-carousel", 1, kind="video"),
                item("two", "posts", "post-carousel", 2),
                item("three", "posts", "post-single", caption="一則貼文、一張卡片"),
                item("one", "stories", "story-one"),
                item("one", "highlights", album_id="album-travel", album_title="旅行紀錄"),
                item("two", "highlights", position=1, album_id="album-travel", album_title="旅行紀錄"),
                item("three", "highlights", album_id="album-daily", album_title="日常片刻"),
                item("video", "reels", "reel-video", kind="video"),
                MediaCandidate("legacy", "posts", "image", "https://unused.invalid/legacy"),
            ])
            colors = {"one": "#9f1239", "two": "#1d4ed8", "three": "#047857", "legacy": "#6b7280"}
            numbers = {"one": "01", "two": "03", "three": "04", "legacy": "OLD"}
            for media in db.pending_media(account_id, 100):
                if media["kind"] == "video":
                    path = root / "demo.webm"
                else:
                    path = root / f"{media['media_key']}.svg"
                    path.write_text(
                        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600" viewBox="0 0 600 600">'
                        f'<rect width="600" height="600" fill="{colors[media["media_key"]]}"/>'
                        '<circle cx="300" cy="280" r="160" fill="none" stroke="#ffffff55" stroke-width="3"/>'
                        f'<text x="300" y="322" text-anchor="middle" fill="white" font-family="sans-serif" font-size="110">{numbers[media["media_key"]]}</text>'
                        '<text x="300" y="495" text-anchor="middle" fill="#ffffffbb" font-family="sans-serif" font-size="28">LOCAL TEST FIXTURE</text></svg>',
                        encoding="utf-8",
                    )
                db.mark_media_downloaded(media["id"], str(path), f"fake-{media['id']}")
            db.record_collection_observations(account_id, "隔離示範", {
                category: {"state": "media", "complete": True}
                for category in ("posts", "stories", "highlights", "reels")
            }, now="2026-09-05T01:00:00+00:00", persist_progress=False)
            db.record_collection_observations(account_id, "隔離示範", {
                "posts": {"state": "partial", "complete": False, "error": "示意：下一批尚未完成"},
                "stories": {"state": "blocked", "complete": False, "error": "示意：來源限流，自動退避中"},
                "reels": {"state": "failed", "complete": False, "error": "示意：連線逾時"},
            }, now="2026-09-05T02:00:00+00:00", persist_progress=False)
            return account_id
        finally:
            db.close()
