import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ig_monitor.config import load_config
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot, ScrapeResult, TerminalState
from ig_monitor.monitor import Monitor


class FakeProfileScraper:
    def __init__(self, _config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def scrape(self, _url):
        snapshot = ProfileSnapshot(
            "iii_u716", "iii_u716", 25, 87, 183, "bio", PrivacyState.PUBLIC,
            "https://cdn.example.test/avatar.jpg", observed_at="2026-09-02T00:00:00+00:00",
        )
        media = [
            MediaCandidate(
                "contaminated-media", "posts", "video",
                "https://cdn.example.test/unrelated-global-video.mp4",
            )
        ]
        return ScrapeResult(snapshot, media, TerminalState.MEDIA, TerminalState.EMPTY)

    async def download(self, _url, _referer):
        return b"\xff\xd8\xffavatar", "image/jpeg"


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_pause_does_not_record_or_download_discovered_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                """
accounts:
  - url: https://insta-stories-viewer.com/iii_u716/
    label: iii_u716
schedule:
  media_download_enabled: false
heartbeat:
  enabled: false
telegram:
  enabled: false
""",
                encoding="utf-8",
            )
            config = load_config(config_path, require_telegram=False)
            db = Database(config.paths.data_dir / "state.sqlite3")
            download = AsyncMock(return_value={
                "downloaded": 0, "photos": 0, "videos": 0, "duplicate": 0,
                "upgraded": 0, "failed": 0, "pending": 0, "attachments": [],
            })
            try:
                with patch("ig_monitor.monitor.ProfileScraper", FakeProfileScraper), patch(
                    "ig_monitor.monitor.download_account_media", download
                ):
                    self.assertEqual(await Monitor(config, db).run(), 0)

                self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0], 0)
                download.assert_not_awaited()
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
