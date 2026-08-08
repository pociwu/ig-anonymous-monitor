import json
import tempfile
import unittest
from pathlib import Path

from ig_monitor.instagram_source import InstagrapiRelationshipSource
from ig_monitor.relationships import CollectorFatalError


class InstagrapiSessionTests(unittest.TestCase):
    def test_health_check_restores_read_only_user_id_from_saved_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            session_path.write_text(
                json.dumps({"authorization_data": {"ds_user_id": "123"}}),
                encoding="utf-8",
            )
            source = InstagrapiRelationshipSource(session_path)
            requested_user_ids = []
            source.client.user_info = lambda user_id: requested_user_ids.append(user_id)

            source.own_account_health()

            self.assertEqual(source.client.user_id, 123)
            self.assertEqual(requested_user_ids, ["123"])

    def test_saved_session_without_user_id_is_rejected_before_health_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            session_path.write_text("{}", encoding="utf-8")
            source = InstagrapiRelationshipSource(session_path)
            source.client.user_info = lambda _user_id: self.fail(
                "invalid session must not issue a health request"
            )

            with self.assertRaisesRegex(CollectorFatalError, "SessionInvalid"):
                source.own_account_health()

    def test_malformed_saved_session_is_reported_as_session_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_path = Path(tmp) / "session.json"
            session_path.write_text("{not-json", encoding="utf-8")
            source = InstagrapiRelationshipSource(session_path)

            with self.assertRaisesRegex(CollectorFatalError, "SessionInvalid"):
                source.own_account_health()


if __name__ == "__main__":
    unittest.main()
