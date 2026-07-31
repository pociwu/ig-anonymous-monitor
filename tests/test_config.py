import tempfile
import unittest
from pathlib import Path

from ig_monitor.config import load_config


class ConfigTests(unittest.TestCase):
    def test_valid_config_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/sin_9311
    enabled: true
heartbeat:
  enabled: false
""", encoding="utf-8")
            config = load_config(path, require_telegram=False)
            self.assertEqual(config.accounts[0].url, "https://insta-stories-viewer.com/sin_9311/")
            self.assertEqual(config.schedule.account_delay_min_seconds, 10)
            self.assertEqual(config.schedule.account_delay_max_seconds, 20)
            self.assertEqual(config.schedule.media_limit_per_account, 50)

    def test_duplicate_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/a/
  - url: https://insta-stories-viewer.com/a/
telegram:
  enabled: false
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重複網址"):
                load_config(path, require_telegram=False)

    def test_wrong_domain_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("accounts:\n  - url: https://example.com/a/\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "格式錯誤"):
                load_config(path, require_telegram=False)

    def test_sixteen_accounts_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            accounts = "\n".join(
                f"  - url: https://insta-stories-viewer.com/account_{index}/"
                for index in range(16)
            )
            path.write_text(f"accounts:\n{accounts}\ntelegram:\n  enabled: false\n", encoding="utf-8")

            config = load_config(path, require_telegram=False)

            self.assertEqual(len(config.accounts), 16)

    def test_seventeen_accounts_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            accounts = "\n".join(
                f"  - url: https://insta-stories-viewer.com/account_{index}/"
                for index in range(17)
            )
            path.write_text(f"accounts:\n{accounts}\ntelegram:\n  enabled: false\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "16"):
                load_config(path, require_telegram=False)


    def test_apify_cap_cannot_exceed_five_usd(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/a/
telegram:
  enabled: false
apify:
  monthly_cap_usd: 5.01
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no more than 5"):
                load_config(path, require_telegram=False)

    def test_apify_enabled_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/a/
telegram:
  enabled: false
apify:
  enabled: true
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "APIFY_API_TOKEN"):
                load_config(path, require_telegram=False)

    def test_schedule_interval_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/a/
telegram:
  enabled: false
schedule:
  interval_minutes: 0
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "interval_minutes"):
                load_config(path, require_telegram=False)


if __name__ == "__main__":
    unittest.main()
