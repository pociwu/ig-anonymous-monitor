import unittest

from ig_monitor.telegram import event_steps, format_event


class TelegramTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
