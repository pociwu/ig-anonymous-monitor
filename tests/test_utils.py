import unittest

from ig_monitor.models import PrivacyState, ProfileSnapshot
from ig_monitor.utils import normalize_text, snapshot_changes


def snap(**changes):
    base = dict(username="a", display_name=None, posts=1, followers=2, following=3,
                bio="hello", privacy=PrivacyState.PRIVATE, avatar_url="https://cdn/a",
                avatar_sha256="one", avatar_path="/one.jpg", observed_at="now")
    base.update(changes)
    return ProfileSnapshot(**base)


class UtilsTests(unittest.TestCase):
    def test_bio_trailing_space_does_not_trigger_change(self):
        old = snap(bio="line one  \r\nline two")
        new = snap(bio="line one\nline two")
        self.assertNotIn("bio", snapshot_changes(old, new))

    def test_real_changes_are_reported(self):
        changes = snapshot_changes(snap(), snap(followers=9, privacy=PrivacyState.PUBLIC, avatar_sha256="two"))
        self.assertEqual(set(changes), {"followers", "privacy", "avatar_sha256"})

    def test_normalize_text_preserves_internal_lines(self):
        self.assertEqual(normalize_text(" a  \r\n b \n"), "a\n b")


if __name__ == "__main__":
    unittest.main()
