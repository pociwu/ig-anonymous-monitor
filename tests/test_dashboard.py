import tempfile
import unittest
from pathlib import Path

from ig_monitor.config import AccountConfig
from ig_monitor.dashboard import create_app
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


class DashboardTests(unittest.TestCase):
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
            finally:
                db.close()
