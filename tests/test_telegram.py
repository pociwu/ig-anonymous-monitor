from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from ig_monitor.config import TelegramConfig
from ig_monitor.db import Database
from ig_monitor.telegram import TelegramSender, event_steps, format_event


class TelegramTests(unittest.TestCase):
    def test_collection_failure_does_not_claim_no_content_was_updated(self):
        text = format_event("failure", {"label": "alice / reels", "scope": "collection",
                                      "fail_count": 3, "error": "部分項目格式無效"})
        self.assertIn("連續 3 輪擷取或解析未完整成功", text)
        self.assertNotIn("次未更新", text)
        self.assertIn("既有內容保留", text)

    def test_collector_state_uses_chinese_labels_and_keeps_codes(self):
        text = format_event("collector_state", {
            "old_state": "risk_hold", "state": "observing", "reason": "BadPassword",
        })
        self.assertIn("風控暫停（risk_hold） → 觀察中（observing）", text)
        self.assertIn("密碼錯誤（BadPassword）", text)

    def test_identity_and_budget_notifications_are_chinese(self):
        username = format_event("username_change", {
            "label": "a", "old_username": "old", "new_username": "new",
        })
        budget = format_event("apify_budget_exhausted", {"cap_usd": 5.0})
        self.assertIn("IG 使用者名稱已變更", username)
        self.assertIn("已透過 Instagram Profile ID 確認", username)
        self.assertIn("Apify 每月用量已達 5.00 美元上限", budget)
        self.assertIn("使用者名稱反查已暫停", budget)

    def test_open_change_heading_and_before_after(self):
        text = format_event("change", {"label": "a", "changes": {
            "privacy": ["private", "public"], "followers": [2, 3]
        }})
        self.assertIn("帳號 a 開放", text)
        self.assertIn("跟隨者：2 → 3", text)

    def test_media_summary_separates_photos_and_videos(self):
        text = format_event("media_summary", {"label": "a", "photos": 2, "videos": 1,
                                                     "duplicate": 3, "failed": 0, "pending": 4})
        self.assertIn("新增照片：2", text)
        self.assertIn("新增影片：1", text)

    def test_media_summary_attaches_new_photo_and_video(self):
        payload = {"label": "a", "photos": 1, "videos": 1, "duplicate": 0,
                   "failed": 0, "pending": 0, "attachments": [
                       {"kind": "image", "path": "/downloads/new.jpg"},
                       {"kind": "video", "path": "/downloads/new.mp4"},
                   ]}
        steps = event_steps("media_summary", payload)
        self.assertEqual([step[0] for step in steps], ["text", "photo", "video"])
        self.assertIn("新增照片 1/1", steps[1][1])
        self.assertIn("新增影片 1/1", steps[2][1])

    def test_media_summary_marks_pending_photo_and_video_captions(self):
        payload = {"label": "我的監控名稱", "review_downloaded": 2, "attachments": [
            {"kind": "image", "path": "/downloads/pending.jpg", "ownership_pending": True},
            {"kind": "video", "path": "/downloads/pending.mp4", "ownership_pending": True},
        ]}
        steps = event_steps("media_summary", payload)
        self.assertEqual([step[0] for step in steps], ["text", "photo", "video"])
        self.assertIn("本通知附2筆媒體", steps[0][1])
        for step, media_label in zip(steps[1:], ["新增照片 1/1", "新增影片 1/1"]):
            self.assertEqual(step[1], "IGWatcher・歸屬待確認\n"
                             "監控標籤：我的監控名稱（來源查詢結果，不代表已確認作者）\n" + media_label)
        self.assertEqual(steps[1][2], "/downloads/pending.jpg")
        self.assertEqual(steps[2][2], "/downloads/pending.mp4")

    def test_mixed_media_marks_only_pending_attachments_and_keeps_numbering(self):
        payload = {"label": "a", "review_downloaded": 2, "attachments": [
            {"kind": "image", "path": "/downloads/normal.jpg"},
            {"kind": "image", "path": "/downloads/pending.jpg", "ownership_pending": True},
            {"kind": "video", "path": "/downloads/pending.mp4", "ownership_pending": True},
            {"kind": "video", "path": "/downloads/normal.mp4", "ownership_pending": False},
        ]}
        steps = event_steps("media_summary", payload)
        self.assertEqual(steps[1], ("photo", "a：新增照片 1/2", "/downloads/normal.jpg"))
        self.assertIn("IGWatcher・歸屬待確認", steps[2][1])
        self.assertIn("新增照片 2/2", steps[2][1])
        self.assertIn("IGWatcher・歸屬待確認", steps[3][1])
        self.assertIn("新增影片 1/2", steps[3][1])
        self.assertEqual(steps[4], ("video", "a：新增影片 2/2", "/downloads/normal.mp4"))
        self.assertIn("本通知附2筆媒體", steps[0][1])
        self.assertNotIn("本通知附4筆媒體", steps[0][1])

    def test_old_media_event_without_pending_markers_keeps_normal_captions(self):
        payload = {"label": "a", "review_downloaded": 3, "attachments": [
            {"kind": "image", "path": "/downloads/normal.jpg"},
            {"kind": "video", "path": "/downloads/normal.mp4"},
        ]}
        steps = event_steps("media_summary", payload)
        self.assertEqual(steps[1], ("photo", "a：新增照片 1/1", "/downloads/normal.jpg"))
        self.assertEqual(steps[2], ("video", "a：新增影片 1/1", "/downloads/normal.mp4"))
        self.assertIn("其中歸屬待確認：3（來源查詢結果，不代表已確認作者；不附媒體）", steps[0][1])

    def test_media_summary_reports_only_pending_attachments_remaining_after_limit(self):
        text = format_event("media_summary", {"label": "a", "review_downloaded": 5, "attachments": [
            {"kind": "image", "path": "/downloads/pending.jpg", "ownership_pending": True},
        ]})
        self.assertIn("其中歸屬待確認：5", text)
        self.assertIn("來源查詢結果，不代表已確認作者；本通知附1筆媒體", text)
        self.assertNotIn("本通知附5筆媒體", text)
        self.assertNotIn("不附媒體", text)

    def test_media_summary_without_pending_attachments_keeps_non_attachment_notice(self):
        for attachments in (None, [], [
            {"kind": "image", "path": "/downloads/normal.jpg", "ownership_pending": False},
            {"kind": "video", "path": "/downloads/normal.mp4", "ownership_pending": "true"},
        ]):
            with self.subTest(attachments=attachments):
                payload = {"label": "a", "review_downloaded": 3}
                if attachments is not None:
                    payload["attachments"] = attachments
                steps = event_steps("media_summary", payload)
                self.assertIn("其中歸屬待確認：3（來源查詢結果，不代表已確認作者；不附媒體）", steps[0][1])
                self.assertNotIn("本通知附", steps[0][1])
                for _, caption, _ in steps[1:]:
                    self.assertNotIn("歸屬待確認", caption)


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_media_retry_resumes_at_failed_video_after_database_reopen(self):
        config = TelegramConfig(enabled=True, retry_limit_per_run=10, send_new_media=True,
                                max_new_media_attachments=10, bot_token=None, chat_id=None,
                                message_thread_id=None)
        sender = TelegramSender(config)
        payload = {"label": "a", "review_downloaded": 2, "attachments": [
            {"kind": "image", "path": "/downloads/pending.jpg", "ownership_pending": True},
            {"kind": "video", "path": "/downloads/pending.mp4", "ownership_pending": True},
        ]}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(sender, "_send_text", new_callable=AsyncMock) as send_text, \
                patch.object(sender, "_send_photo", new_callable=AsyncMock) as send_photo, \
                patch.object(sender, "_send_video", new_callable=AsyncMock) as send_video:
            db_path = Path(tmp) / "state.sqlite3"
            send_video.side_effect = [RuntimeError("temporary send failure"), None]
            with closing(Database(db_path)) as db:
                db.enqueue_event("pending-media", "media_summary", payload)
                event = db.pending_events(1)[0]
                with self.assertRaisesRegex(RuntimeError, "temporary send failure"):
                    await sender._deliver_event(db, event)
                self.assertEqual(db.pending_events(1)[0]["payload"]["delivery_stage"], 2)
                send_text.assert_awaited_once()
                send_photo.assert_awaited_once()
                send_video.assert_awaited_once()
                self.assertIn("IGWatcher・歸屬待確認", send_photo.await_args.args[1])

            send_text.reset_mock()
            send_photo.reset_mock()
            send_video.reset_mock()
            with closing(Database(db_path)) as db:
                retry_event = db.pending_events(1)[0]
                self.assertEqual(retry_event["payload"]["delivery_stage"], 2)
                self.assertEqual(retry_event["payload"]["attachments"], payload["attachments"])
                await sender._deliver_event(db, retry_event)
                self.assertEqual(db.pending_events(1)[0]["payload"]["delivery_stage"], 3)
                send_text.assert_not_awaited()
                send_photo.assert_not_awaited()
                send_video.assert_awaited_once_with(
                    Path("/downloads/pending.mp4"),
                    "IGWatcher・歸屬待確認\n監控標籤：a（來源查詢結果，不代表已確認作者）\n新增影片 1/1",
                )


if __name__ == "__main__":
    unittest.main()
