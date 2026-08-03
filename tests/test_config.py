import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            self.assertFalse(config.instagram_enrichment.enabled)
            self.assertFalse(config.instagram_posts.enabled)
            self.assertEqual(config.instagram_posts.baseline_max, 6)
            self.assertEqual(config.instagram_posts.batch_size, 12)
            self.assertTrue(config.accounts[0].relationship_tracking)
            self.assertTrue(config.accounts[0].post_tracking)
            self.assertFalse(config.accounts[0].full_post_backfill_on_reopen)

    def test_instagram_posts_safe_limits_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/sin_9311/
    post_tracking: false
    full_post_backfill_on_reopen: true
telegram:
  enabled: false
instagram_posts:
  baseline_min: 2
  baseline_max: 5
  batch_size: 10
  jobs_per_day: 1
  reconcile_days: 45
  min_free_gb: 8
  min_free_percent: 15
""", encoding="utf-8")

            config = load_config(path, require_telegram=False)

            self.assertFalse(config.instagram_posts.enabled)
            self.assertEqual(config.instagram_posts.baseline_min, 2)
            self.assertEqual(config.instagram_posts.baseline_max, 5)
            self.assertEqual(config.instagram_posts.jobs_per_day, 1)
            self.assertFalse(config.accounts[0].post_tracking)
            self.assertTrue(config.accounts[0].full_post_backfill_on_reopen)

    def test_instagram_posts_rejects_less_safe_limits(self):
        unsafe_values = {
            "baseline_max": 7,
            "batch_size": 13,
            "jobs_per_day": 3,
            "reconcile_days": 29,
            "min_free_gb": 4.9,
            "min_free_percent": 9,
            "phase_one_stable_days": 29,
            "canary_days": 6,
            "post_delay_min_seconds": 9,
            "carousel_delay_min_seconds": 1,
            "retry_delay_min_seconds": 29,
        }
        for name, value in unsafe_values.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.yaml"
                path.write_text(f"""
accounts:
  - url: https://insta-stories-viewer.com/a/
telegram:
  enabled: false
instagram_posts:
  {name}: {value}
""", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "instagram_posts"):
                    load_config(path, require_telegram=False)

    def test_only_one_enabled_full_post_backfill_account_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/a/
    full_post_backfill_on_reopen: true
  - url: https://insta-stories-viewer.com/b/
    full_post_backfill_on_reopen: true
telegram:
  enabled: false
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "only one"):
                load_config(path, require_telegram=False)

    def test_post_canary_account_is_fixed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/a/
telegram:
  enabled: false
instagram_posts:
  canary_account: another_account
""", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "chaiyi_lili.cos"):
                load_config(path, require_telegram=False)

    def test_instagram_enrichment_safe_limits_are_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/a/
    relationship_tracking: false
telegram:
  enabled: false
instagram_enrichment:
  enabled: true
  daily_relationship_jobs: 4
  minimum_job_interval_minutes: 300
  daily_member_enrichments: 50
  page_delay_min_seconds: 15
  page_delay_max_seconds: 25
""", encoding="utf-8")

            config = load_config(path, require_telegram=False)

            self.assertTrue(config.instagram_enrichment.enabled)
            self.assertFalse(config.accounts[0].relationship_tracking)
            self.assertEqual(config.instagram_enrichment.daily_relationship_jobs, 4)
            self.assertEqual(config.instagram_enrichment.minimum_job_interval_minutes, 300)

    def test_instagram_enrichment_rejects_less_safe_limits(self):
        unsafe_values = {
            "daily_relationship_jobs": 7,
            "minimum_job_interval_minutes": 239,
            "daily_member_enrichments": 67,
            "page_size": 201,
            "page_delay_min_seconds": 9,
            "direction_delay_min_seconds": 119,
            "observation_hours": 71,
            "canary_days": 6,
        }
        for name, value in unsafe_values.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.yaml"
                path.write_text(f"""
accounts:
  - url: https://insta-stories-viewer.com/a/
telegram:
  enabled: false
instagram_enrichment:
  {name}: {value}
""", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, name):
                    load_config(path, require_telegram=False)

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

    def test_non_apify_service_can_load_enabled_apify_without_token(self):
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
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(
                    path, require_telegram=False, require_apify=False
                )
            self.assertTrue(config.apify.enabled)
            self.assertIsNone(config.apify.token)

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
