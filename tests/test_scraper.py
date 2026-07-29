import unittest
from pathlib import Path

from ig_monitor.config import BrowserConfig
from ig_monitor.models import MediaCandidate
from ig_monitor.scraper import ProfileScraper


class ScraperTests(unittest.TestCase):
    def test_count_parser(self):
        self.assertEqual(ProfileScraper._parse_count("1,234"), 1234)
        self.assertEqual(ProfileScraper._parse_count("1.5K"), 1500)
        with self.assertRaises(ValueError):
            ProfileScraper._parse_count("loading")

    def test_best_source_wins(self):
        low = MediaCandidate("low", "posts", "image", "https://cdn/low", "post1", 1, source_rank=10)
        high = MediaCandidate("high", "posts", "image", "https://cdn/high", "post1", 1, source_rank=100)
        result = ProfileScraper._best_candidates([low, high])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "https://cdn/high")

    def test_thumbnail_is_dropped_when_original_exists(self):
        thumbnail = MediaCandidate("thumb", "posts", "image", "https://cdn/thumb", source_rank=30)
        original = MediaCandidate("original", "posts", "image", "https://cdn/original", "post1", 0, source_rank=100)
        result = ProfileScraper._best_candidates([thumbnail, original])
        self.assertEqual([item.url for item in result], ["https://cdn/original"])

    def test_html_xhr_media_and_highlight_are_extracted(self):
        scraper = ProfileScraper(BrowserConfig(True, 45, 1, Path("browsers")))
        payloads = [{"category": "stories", "data": {"highlights": {
            "html": '<video src="https://cdn.example.test/original.mp4"></video>'
        }}}]
        result = scraper._json_media(payloads, "https://insta-stories-viewer.com/a/")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].category, "highlights")
        self.assertEqual(result[0].kind, "video")


if __name__ == "__main__":
    unittest.main()
