import unittest

from ig_monitor.apify import ApifyClient, ApifyError


class ApifyClientTests(unittest.TestCase):
    def test_identity_parser_accepts_actor_common_fields(self):
        self.assertEqual(ApifyClient._first_text({"id": 123}, "id"), "123")
        self.assertEqual(ApifyClient._first_text({"username": "  person  "}, "username"), "person")
        self.assertIsNone(ApifyClient._first_text({}, "id"))

    def test_error_type_is_operational(self):
        self.assertIsInstance(ApifyError("x"), RuntimeError)
