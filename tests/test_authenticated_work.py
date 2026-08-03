import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ig_monitor.authenticated_work import AuthenticatedWorkCoordinator
from ig_monitor.config import AccountConfig
from ig_monitor.db import Database


class AuthenticatedWorkCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.sqlite3")
        self.db.sync_accounts([
            AccountConfig("https://insta-stories-viewer.com/a/", True, "a"),
            AccountConfig("https://insta-stories-viewer.com/b/", True, "b"),
            AccountConfig("https://insta-stories-viewer.com/c/", True, "c"),
        ])
        self.accounts = {row["account_key"]: row for row in self.db.enabled_accounts()}
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def enqueue(self, account_key: str, when: datetime | None = None) -> int:
        return self.db.enqueue_relationship_job(
            self.accounts[account_key]["id"], True, False, "count_change",
            (when or self.now).isoformat(timespec="seconds"),
        )

    def coordinator(self, daily_limit: int = 6) -> AuthenticatedWorkCoordinator:
        return AuthenticatedWorkCoordinator(
            self.db, daily_limit, 240, clock=lambda: self.now
        )

    def test_claim_creates_shared_ledger_and_blocks_concurrent_work(self):
        job_id = self.enqueue("a")
        coordinator = self.coordinator()

        first = coordinator.claim_next(self.now)
        second = coordinator.claim_next(self.now)

        self.assertEqual(first.status, "claimed")
        self.assertEqual(first.claim.work_ref_id, job_id)
        self.assertEqual(second.status, "busy")
        row = self.db.conn.execute(
            "SELECT * FROM authenticated_work_runs WHERE id=?", (first.claim.run_id,)
        ).fetchone()
        self.assertEqual((row["work_kind"], row["budget_day"], row["status"]),
                         ("relationship", "2026-08-03", "running"))

    def test_spacing_and_daily_budget_are_shared_durable_rules(self):
        coordinator = self.coordinator(daily_limit=2)
        self.enqueue("a")
        self.enqueue("b")
        self.enqueue("c")

        first = coordinator.claim_next(self.now)
        coordinator.finish(first.claim, "completed")
        self.assertEqual(
            coordinator.claim_next(self.now + timedelta(hours=3, minutes=59)).status,
            "spacing",
        )
        second = coordinator.claim_next(self.now + timedelta(hours=4))
        coordinator.finish(second.claim, "completed")
        self.assertEqual(
            coordinator.claim_next(self.now + timedelta(hours=8)).status,
            "daily_budget",
        )

    def test_expired_lease_is_recoverable_but_still_consumes_budget_and_spacing(self):
        coordinator = self.coordinator()
        self.enqueue("a")
        first = coordinator.claim_next(self.now)

        blocked = coordinator.claim_next(self.now + timedelta(minutes=31))
        retried = coordinator.claim_next(self.now + timedelta(hours=4))

        self.assertEqual(blocked.status, "spacing")
        self.assertEqual(retried.status, "claimed")
        self.assertEqual(retried.claim.work_ref_id, first.claim.work_ref_id)
        states = [row[0] for row in self.db.conn.execute(
            "SELECT status FROM authenticated_work_runs ORDER BY id"
        )]
        self.assertEqual(states, ["abandoned", "running"])

    def test_post_jobs_are_not_claimed_before_phase_two_worker_exists(self):
        now_text = self.now.isoformat(timespec="seconds")
        self.db.conn.execute(
            """INSERT INTO post_jobs(
                 account_id,reason,mode,priority,status,available_at,created_at,updated_at
               ) VALUES(?, 'baseline', 'ordinary', 100, 'pending', ?, ?, ?)""",
            (self.accounts["a"]["id"], now_text, now_text, now_text),
        )
        self.db.conn.commit()

        self.assertEqual(self.coordinator().claim_next(self.now).status, "idle")
        self.assertEqual(
            self.db.conn.execute("SELECT status FROM post_jobs").fetchone()[0], "pending"
        )

    def test_existing_relationship_starts_are_backfilled_into_shared_ledger(self):
        job_id = self.enqueue("a")
        started_at = self.now.isoformat(timespec="seconds")
        self.db.conn.execute(
            """UPDATE relationship_jobs
               SET status='completed',started_at=?,updated_at=? WHERE id=?""",
            (started_at, started_at, job_id),
        )
        self.db.conn.execute("DELETE FROM authenticated_work_runs")
        self.db.conn.commit()
        path = self.db.path
        self.db.close()
        self.db = Database(path)

        row = self.db.conn.execute(
            "SELECT * FROM authenticated_work_runs WHERE work_ref_id=?", (job_id,)
        ).fetchone()

        self.assertEqual(row["work_kind"], "relationship")
        self.assertEqual(row["budget_day"], "2026-08-03")
        self.assertEqual(row["status"], "finished")


if __name__ == "__main__":
    unittest.main()
