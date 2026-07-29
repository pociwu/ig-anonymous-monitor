import tempfile
import unittest
from pathlib import Path

from ig_monitor.media import save_avatar


class FakeScraper:
    async def download(self, url, referer):
        return b"\xff\xd8\xffsame-image", "image/jpeg"


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_avatar_reuses_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_hash, first_path = await save_avatar(FakeScraper(), Path(tmp), "a", "https://cdn/a", "https://site/a")
            second_hash, second_path = await save_avatar(FakeScraper(), Path(tmp), "a", "https://cdn/a2", "https://site/a")
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_path, second_path)
            self.assertEqual(len(list((Path(tmp) / "a" / "avatar").iterdir())), 1)
