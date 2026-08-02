import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from ig_monitor.config import AccountConfig, load_config
from ig_monitor.dashboard import _chart_axis, create_app, system_status
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


class DashboardTests(unittest.TestCase):
    def test_constant_integer_chart_uses_distinct_lower_and_upper_ticks(self):
        axis = _chart_axis([25, 25, 25])

        self.assertEqual(axis, {"min": 23, "max": 27, "ticks": [23, 24, 25, 26, 27]})

    def test_dashboard_shows_latest_deltas_and_profile_history_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite3"
            db = Database(db_path)
            try:
                db.sync_accounts([
                    AccountConfig("https://insta-stories-viewer.com/trend/", True, "trend")
                ])
                account = db.get_account("trend")
                db.record_success(account["id"], ProfileSnapshot(
                    "trend", None, 10, 20, 30, "", PrivacyState.PUBLIC, "",
                    observed_at="2026-08-01T00:00:00+00:00",
                ), [], [])
                db.record_success(account["id"], ProfileSnapshot(
                    "trend", None, 12, 25, 28, "", PrivacyState.PUBLIC, "",
                    observed_at="2026-08-02T00:00:00+00:00",
                ), [], [])
            finally:
                db.close()

            app = create_app(db_path, lambda: {
                "monitor": "active", "timer": "active", "next_run": "soon",
            })
            home = app.test_client().get("/").data.decode("utf-8")
            self.assertIn("(+2)", home)
            self.assertIn("(+5)", home)
            self.assertIn("(-2)", home)
            self.assertIn("delta-up", home)
            self.assertIn("delta-down", home)

            detail = app.test_client().get(f"/account/{account['id']}").data.decode("utf-8")
            self.assertIn('id="posts-history-chart"', detail)
            self.assertIn('id="relationships-history-chart"', detail)
            self.assertIn('id="posts-history-tooltip"', detail)
            self.assertIn('id="relationships-history-tooltip"', detail)
            self.assertNotIn('id="profile-metric-select"', detail)
            self.assertIn('<details class="trend" id="social-trends">', detail)
            self.assertNotIn('<details class="trend" id="social-trends" open>', detail)
            self.assertIn('<summary><span>社群趨勢</span>', detail)
            self.assertIn("socialTrends.addEventListener('toggle'", detail)
            self.assertIn("貼文數量", detail)
            self.assertIn("跟隨者與追蹤中", detail)
            self.assertIn("showChartPoint", detail)
            self.assertIn('"date": "2026-08-01"', detail)
            self.assertIn('"posts": 12', detail)

    def test_docker_runtime_status_does_not_depend_on_systemd(self):
        with patch.dict("os.environ", {"IG_MONITOR_RUNTIME": "docker"}):
            self.assertEqual(system_status(), {
                "monitor": "Docker Compose",
                "timer": "內建排程器",
                "next_run": "依 config.yaml 的 interval_minutes",
            })

    def test_dashboard_shows_saved_profile_id_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "state.sqlite3")
            try:
                account = AccountConfig("https://insta-stories-viewer.com/test/", True, "test")
                db.sync_accounts([account])
                row = db.enabled_accounts()[0]
                avatar = Path(tmp) / "avatar.jpg"
                avatar.write_bytes(b"avatar")
                media_file = Path(tmp) / "photo.jpg"
                media_file.write_bytes(b"photo")
                snapshot = ProfileSnapshot(
                    "test", "Test Account", 1, 2, 3, "", PrivacyState.PRIVATE, "",
                    avatar_path=str(avatar), observed_at="now",
                )
                candidate = MediaCandidate("media-1", "post", "image", "https://example.test/photo.jpg")
                db.record_success(row["id"], snapshot, [], [candidate])
                media_row = db.pending_media(row["id"], 1)[0]
                db.mark_media_downloaded(media_row["id"], str(media_file), "hash")
                db.set_identity(row["id"], "12345", "test")
                app = create_app(Path(tmp) / "state.sqlite3", lambda: {"monitor": "active", "timer": "active", "next_run": "soon"})
                response = app.test_client().get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"12345", response.data)
                self.assertIn(b"test", response.data)
                self.assertIn(b"/account/1", response.data)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                detail = app.test_client().get(f"/account/{row['id']}")
                self.assertEqual(detail.status_code, 200)
                self.assertIn(b"12345", detail.data)
                self.assertIn(f"/media/{media_row['id']}".encode(), detail.data)
                self.assertIn(b'data-source="posts"', detail.data)
                self.assertIn(b'data-source="stories"', detail.data)
                self.assertIn(b'data-source="highlights"', detail.data)
                self.assertIn(b'data-sources="posts"', detail.data)
                self.assertEqual(app.test_client().get(f"/account/{row['id']}/avatar").data, b"avatar")
                self.assertEqual(app.test_client().get(f"/media/{media_row['id']}").data, b"photo")
                health = app.test_client().get("/healthz")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.get_json(), {"status": "ok"})
            finally:
                db.close()

    def test_home_adds_a_validated_account_to_config_and_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/existing/
    enabled: true
    label: existing
telegram:
  enabled: false
""", encoding="utf-8")
            db_path = root / "state.sqlite3"
            db = Database(db_path)
            db.sync_accounts(load_config(config_path, require_telegram=False).accounts)
            db.close()
            validated = []
            app = create_app(
                db_path,
                lambda: {"monitor": "active", "timer": "active", "next_run": "soon"},
                config_path=config_path,
                account_validator=lambda url: validated.append(url),
            )

            response = app.test_client().post("/accounts", data={
                "url": "https://insta-stories-viewer.com/new_account",
                "label": "新帳號",
            })

            self.assertEqual(response.status_code, 303)
            self.assertEqual(validated, ["https://insta-stories-viewer.com/new_account/"])
            saved = load_config(config_path, require_telegram=False)
            self.assertEqual([account.key for account in saved.accounts], ["existing", "new_account"])
            home = app.test_client().get("/").data
            self.assertIn("新帳號".encode(), home)
            self.assertIn("驗證並新增".encode(), home)
            self.assertIn("移除監控".encode(), home)

    def test_home_rejects_an_unavailable_account_without_changing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            original = """
accounts:
  - url: https://insta-stories-viewer.com/existing/
    enabled: true
telegram:
  enabled: false
"""
            config_path.write_text(original, encoding="utf-8")

            def unavailable(_url):
                raise ValueError("網址驗證失敗：頁面無法載入")

            app = create_app(
                root / "state.sqlite3",
                lambda: {"monitor": "active", "timer": "active", "next_run": "soon"},
                config_path=config_path,
                account_validator=unavailable,
            )

            response = app.test_client().post("/accounts", data={
                "url": "https://insta-stories-viewer.com/not_found/",
            })

            self.assertEqual(response.status_code, 400)
            self.assertIn("頁面無法載入".encode(), response.data)
            saved = load_config(config_path, require_telegram=False)
            self.assertEqual([account.key for account in saved.accounts], ["existing"])

    def test_home_can_add_the_sixteenth_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            accounts = "\n".join(
                f"  - url: https://insta-stories-viewer.com/account_{index}/"
                for index in range(15)
            )
            config_path.write_text(
                f"accounts:\n{accounts}\ntelegram:\n  enabled: false\n",
                encoding="utf-8",
            )
            db_path = root / "state.sqlite3"
            db = Database(db_path)
            db.sync_accounts(load_config(config_path, require_telegram=False).accounts)
            db.close()
            app = create_app(
                db_path,
                lambda: {"monitor": "active", "timer": "active", "next_run": "soon"},
                config_path=config_path,
                account_validator=lambda _url: None,
            )

            response = app.test_client().post("/accounts", data={
                "url": "https://insta-stories-viewer.com/account_15/",
            })

            self.assertEqual(response.status_code, 303)
            self.assertEqual(len(load_config(config_path, require_telegram=False).accounts), 16)

    def test_remove_button_stops_monitoring_without_deleting_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/keep/
    enabled: true
  - url: https://insta-stories-viewer.com/remove_me/
    enabled: true
telegram:
  enabled: false
""", encoding="utf-8")
            db_path = root / "state.sqlite3"
            config = load_config(config_path, require_telegram=False)
            db = Database(db_path)
            db.sync_accounts(config.accounts)
            remove_row = db.get_account("remove_me")
            media_file = root / "saved.jpg"
            media_file.write_bytes(b"saved media")
            snapshot = ProfileSnapshot(
                "remove_me", None, 1, 2, 3, "", PrivacyState.PUBLIC, "",
                avatar_path=None, observed_at="now",
            )
            candidate = MediaCandidate("saved", "posts", "image", "https://example.test/saved.jpg")
            db.record_success(remove_row["id"], snapshot, [], [candidate])
            media_row = db.pending_media(remove_row["id"], 1)[0]
            db.mark_media_downloaded(media_row["id"], str(media_file), "hash")
            db.close()
            app = create_app(
                db_path,
                lambda: {"monitor": "active", "timer": "active", "next_run": "soon"},
                config_path=config_path,
                account_validator=lambda _url: None,
            )

            response = app.test_client().post(f"/accounts/{remove_row['id']}/remove")

            self.assertEqual(response.status_code, 303)
            saved = load_config(config_path, require_telegram=False)
            self.assertEqual([account.key for account in saved.accounts], ["keep"])
            self.assertNotIn(b"remove_me", app.test_client().get("/").data)
            self.assertEqual(app.test_client().get(f"/account/{remove_row['id']}").status_code, 404)
            self.assertTrue(media_file.is_file())

    def test_drag_order_is_saved_and_used_by_the_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text("""
accounts:
  - url: https://insta-stories-viewer.com/first/
  - url: https://insta-stories-viewer.com/second/
  - url: https://insta-stories-viewer.com/third/
telegram:
  enabled: false
""", encoding="utf-8")
            db_path = root / "state.sqlite3"
            db = Database(db_path)
            db.sync_accounts(load_config(config_path, require_telegram=False).accounts)
            ids = {row["account_key"]: row["id"] for row in db.enabled_accounts()}
            db.close()
            app = create_app(
                db_path,
                lambda: {"monitor": "active", "timer": "active", "next_run": "soon"},
                config_path=config_path,
                account_validator=lambda _url: None,
            )

            response = app.test_client().post("/accounts/reorder", json={
                "account_ids": [ids["third"], ids["first"], ids["second"]],
            })

            self.assertEqual(response.status_code, 204)
            saved = load_config(config_path, require_telegram=False)
            self.assertEqual([account.key for account in saved.accounts], ["third", "first", "second"])
            home = app.test_client().get("/").data.decode("utf-8")
            self.assertLess(home.index(">third<"), home.index(">first<"))
            self.assertLess(home.index(">first<"), home.index(">second<"))
            self.assertIn('draggable="true"', home)
