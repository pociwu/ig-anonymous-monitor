import tempfile
import unittest
from pathlib import Path

from ig_monitor.config import AccountConfig
from ig_monitor.dashboard import create_app
from ig_monitor.db import Database
from ig_monitor.models import PrivacyState, ProfileSnapshot


class DashboardTests(unittest.TestCase):
    def test_dashboard_shows_saved_profile_id_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "state.sqlite3")
            try:
                account = AccountConfig("https://insta-stories-viewer.com/test/", True, "test")
                db.sync_accounts([account])
                row = db.enabled_accounts()[0]
                snapshot = ProfileSnapshot("test", None, 1, 2, 3, "", PrivacyState.PRIVATE, "", observed_at="now")
                db.record_success(row["id"], snapshot, [], [])
                db.set_identity(row["id"], "12345", "test")
                app = create_app(Path(tmp) / "state.sqlite3", lambda: {"monitor": "active", "timer": "active", "next_run": "soon"})
                response = app.test_client().get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"12345", response.data)
                self.assertIn(b"test", response.data)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            finally:
                db.close()
