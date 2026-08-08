import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ig_monitor.config import AccountConfig
from ig_monitor.db import Database
from ig_monitor.models import MediaCandidate, PrivacyState, ProfileSnapshot


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.sqlite3")
        self.account = AccountConfig("https://insta-stories-viewer.com/a/", True, "a")
        self.db.sync_accounts([self.account])
        self.row = self.db.enabled_accounts()[0]

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_failure_alert_only_after_three_runs(self):
        self.db.record_failure(self.row["id"], "a", "err", None)
        self.db.record_failure(self.row["id"], "a", "err", None)
        self.assertEqual(self.db.pending_events(10), [])
        self.db.record_failure(self.row["id"], "a", "err", "CAPTCHA")
        events = self.db.pending_events(10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "failure")

    def test_snapshot_and_initial_event_are_atomic(self):
        snapshot = ProfileSnapshot("a", None, 1, 2, 3, "bio", PrivacyState.PRIVATE,
                                   "https://cdn/a", "hash", "/a.jpg", "now")
        self.db.record_success(self.row["id"], snapshot,
                               [("initial:1", "initial", {"label": "a", "snapshot": snapshot.to_dict()})], [])
        row = self.db.get_account("a")
        self.assertEqual(self.db.snapshot_from_row(row).followers, 2)
        self.assertEqual(len(self.db.pending_events(10)), 1)
        self.assertTrue(self.db.reset_account("a"))
        self.assertIsNone(self.db.snapshot_from_row(self.db.get_account("a")))
        self.assertEqual(self.db.pending_events(10), [])

    def test_successful_observations_append_profile_count_history(self):
        first = ProfileSnapshot(
            "a", None, 10, 20, 30, "", PrivacyState.PUBLIC, "",
            observed_at="2026-08-01T00:00:00+00:00",
        )
        second = ProfileSnapshot(
            "a", None, 12, 25, 28, "", PrivacyState.PUBLIC, "",
            observed_at="2026-08-02T00:00:00+00:00",
        )
        self.db.record_success(self.row["id"], first, [], [])
        self.db.record_success(self.row["id"], second, [], [])

        history = self.db.profile_history(self.row["id"])

        self.assertEqual(
            [(row["posts"], row["followers"], row["following"]) for row in history],
            [(10, 20, 30), (12, 25, 28)],
        )

    def test_existing_events_backfill_profile_count_history(self):
        first = ProfileSnapshot(
            "a", None, 10, 20, 30, "", PrivacyState.PUBLIC, "",
            observed_at="2026-07-01T00:00:00+00:00",
        )
        current = ProfileSnapshot(
            "a", None, 12, 25, 28, "", PrivacyState.PUBLIC, "",
            observed_at="2026-07-03T00:00:00+00:00",
        )
        self.db.conn.execute(
            "UPDATE accounts SET snapshot_json=?,last_success_at=? WHERE id=?",
            (json.dumps(current.to_dict()), current.observed_at, self.row["id"]),
        )
        self.db.conn.execute(
            """INSERT INTO events(event_key,account_id,kind,payload_json,created_at)
               VALUES(?,?,?,?,?)""",
            ("legacy-initial", self.row["id"], "initial", json.dumps({"snapshot": first.to_dict()}),
             "2026-07-01T00:00:00+00:00"),
        )
        self.db.conn.execute(
            """INSERT INTO events(event_key,account_id,kind,payload_json,created_at)
               VALUES(?,?,?,?,?)""",
            ("legacy-change", self.row["id"], "change", json.dumps({"changes": {
                "posts": [10, 12], "followers": [20, 25], "following": [30, 28],
            }}), "2026-07-02T00:00:00+00:00"),
        )
        self.db.conn.execute("DELETE FROM profile_history WHERE account_id=?", (self.row["id"],))
        self.db.conn.commit()
        path = self.db.path
        self.db.close()
        self.db = Database(path)

        history = self.db.profile_history(self.row["id"])

        self.assertEqual(
            [(row["observed_at"], row["posts"], row["followers"], row["following"]) for row in history],
            [
                ("2026-07-01T00:00:00+00:00", 10, 20, 30),
                ("2026-07-02T00:00:00+00:00", 12, 25, 28),
                ("2026-07-03T00:00:00+00:00", 12, 25, 28),
            ],
        )


    def test_identity_and_effective_url_survive_config_sync(self):
        self.db.set_identity(self.row["id"], "123", "renamed")
        self.db.sync_accounts([self.account])
        row = self.db.get_account("a")
        self.assertEqual(row["instagram_profile_id"], "123")
        self.assertEqual(row["effective_url"], "https://insta-stories-viewer.com/renamed/")

    def test_apify_budget_notice_is_once_per_cycle(self):
        self.assertFalse(self.db.budget_notice_sent("2026-07-01"))
        self.db.mark_budget_notice_sent("2026-07-01")
        self.assertTrue(self.db.budget_notice_sent("2026-07-01"))
        self.assertFalse(self.db.budget_notice_sent("2026-08-01"))

    def test_summary_pending_only_counts_downloadable_media(self):
        snapshot = ProfileSnapshot("a", None, 1, 2, 3, "bio", PrivacyState.PUBLIC,
                                   None, None, None, "now")
        media = [
            MediaCandidate("pending", "posts", "image", "https://cdn/pending.jpg"),
            MediaCandidate("failed", "posts", "image", "https://cdn/failed.jpg"),
            MediaCandidate("downloaded", "posts", "image", "https://cdn/downloaded.jpg"),
            MediaCandidate("duplicate", "posts", "image", "https://cdn/duplicate.jpg"),
        ]
        self.db.record_success(self.row["id"], snapshot, [], media)
        rows = {item["media_key"]: item for item in self.db.pending_media(self.row["id"], 10)}
        self.db.mark_media_failed(rows["failed"]["id"], "temporary error")
        self.db.mark_media_downloaded(rows["downloaded"]["id"], "/downloaded.jpg", "hash-1")
        self.db.mark_media_downloaded(rows["duplicate"]["id"], "/duplicate.jpg", "hash-2")
        self.db.mark_media_duplicate(rows["duplicate"]["id"], rows["downloaded"]["id"], "hash-1")

        self.assertEqual(self.db.summary()["pending"], 2)

    def test_relationship_schema_and_account_switch_are_migrated(self):
        self.assertEqual(self.row["relationship_tracking"], 1)
        expected_tables = {
            "collector_state", "relationship_jobs", "relationship_runs",
            "relationship_run_members", "relationship_members",
            "account_relationships", "relationship_history",
            "member_enrichment_jobs", "member_enrichment_attempts",
            "authenticated_work_runs", "post_feature_state", "post_jobs",
            "post_runs", "posts", "post_items", "post_change_history",
        }
        actual = {
            row[0] for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(expected_tables <= actual)
        self.assertEqual(self.row["post_tracking"], 1)
        self.assertEqual(self.row["full_post_backfill_on_reopen"], 0)
        self.assertEqual(self.db.conn.execute(
            "SELECT state FROM post_feature_state WHERE id=1"
        ).fetchone()[0], "disabled")

        disabled = AccountConfig(
            "https://insta-stories-viewer.com/a/", True, "a", False
        )
        self.db.sync_accounts([disabled])
        self.assertEqual(self.db.get_account("a")["relationship_tracking"], 0)

    def test_existing_active_members_without_cached_avatar_are_backfilled_on_startup(self):
        now = "2026-08-08T00:00:00+00:00"
        self.db.conn.execute(
            """INSERT INTO relationship_members(
                 instagram_profile_id,username,username_observed_at,created_at,updated_at
               ) VALUES('member-1','alice',?,?,?)""",
            (now, now, now),
        )
        self.db.conn.execute(
            """INSERT INTO account_relationships(
                 account_id,direction,instagram_profile_id,username,active,
                 first_seen_at,last_seen_at
               ) VALUES(?,'following','member-1','alice',1,?,?)""",
            (self.row["id"], now, now),
        )
        self.db.conn.commit()
        path = self.db.path
        self.db.close()

        self.db = Database(path)

        job = self.db.conn.execute(
            """SELECT reason,status FROM member_enrichment_jobs
               WHERE instagram_profile_id='member-1'"""
        ).fetchone()
        self.assertEqual((job["reason"], job["status"]), ("avatar_cache_backfill", "pending"))

    def test_stuck_relationship_queue_alert_is_enqueued_only_once(self):
        now = datetime(2026, 8, 2, tzinfo=UTC)
        job_id = self.db.enqueue_relationship_job(
            self.row["id"], True, False, "count_change",
            (now - timedelta(hours=25)).isoformat(),
        )
        self.db.conn.execute(
            "UPDATE relationship_jobs SET created_at=? WHERE id=?",
            ((now - timedelta(hours=25)).isoformat(), job_id),
        )
        self.db.conn.commit()
        self.db.enqueue_relationship_watchdogs(now)
        self.db.enqueue_relationship_watchdogs(now + timedelta(minutes=5))
        events = [event for event in self.db.pending_events(20) if event["kind"] == "queue_stuck"]
        self.assertEqual(len(events), 1)

    def test_collector_risk_hold_suspends_enabled_post_state_and_jobs(self):
        now = "2026-08-03T00:00:00+00:00"
        self.db.conn.execute(
            "UPDATE post_feature_state SET state='active',updated_at=? WHERE id=1", (now,)
        )
        self.db.conn.execute(
            """INSERT INTO post_jobs(
                 account_id,reason,mode,priority,status,available_at,created_at,updated_at
               ) VALUES(?,'baseline','ordinary',100,'pending',?,?,?)""",
            (self.row["id"], now, now, now),
        )
        self.db.conn.commit()

        self.db.place_collector_risk_hold("PleaseWaitFewMinutes")

        state = self.db.conn.execute(
            "SELECT state,suspension_reason FROM post_feature_state WHERE id=1"
        ).fetchone()
        job = self.db.conn.execute("SELECT status FROM post_jobs").fetchone()
        self.assertEqual((state["state"], state["suspension_reason"]),
                         ("suspended", "PleaseWaitFewMinutes"))
        self.assertEqual(job["status"], "paused")


if __name__ == "__main__":
    unittest.main()
