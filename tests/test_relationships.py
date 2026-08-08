import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ig_monitor.config import AccountConfig, InstagramEnrichmentConfig
from ig_monitor.db import Database
from ig_monitor.dashboard import create_app
from ig_monitor.member_enrichment import MemberEnrichmentWorker, PlaywrightMemberProfileSource
from ig_monitor.models import PrivacyState, ProfileSnapshot
from ig_monitor.relationships import (
    CollectorAdministration,
    CollectorFatalError,
    CollectorIdentity,
    Direction,
    MemberIdentity,
    RelationshipPage,
    RelationshipTarget,
    RelationshipTrigger,
    RelationshipWorker,
)


def enrichment(enabled=True):
    return InstagramEnrichmentConfig(
        enabled=enabled,
        member_limit_per_direction=1000,
        page_size=200,
        page_delay_min_seconds=10,
        page_delay_max_seconds=20,
        direction_delay_min_seconds=120,
        direction_delay_max_seconds=300,
        daily_relationship_jobs=6,
        minimum_job_interval_minutes=240,
        reconciliation_days=30,
        observation_hours=72,
        canary_days=7,
        daily_member_enrichments=66,
        member_delay_min_seconds=30,
        member_delay_max_seconds=90,
        member_retry_min_hours=6,
        member_stale_days=30,
    )


def snapshot(followers, following, privacy=PrivacyState.PUBLIC):
    return ProfileSnapshot(
        "target", None, 10, followers, following, "", privacy, "", observed_at="2026-08-02T00:00:00+00:00"
    )


class RelationshipTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.sqlite3")
        self.db.sync_accounts([
            AccountConfig("https://insta-stories-viewer.com/target/", True, "target")
        ])
        self.account = self.db.get_account("target")
        self.now = datetime(2026, 8, 2, tzinfo=UTC)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_global_disabled_never_queues(self):
        result = RelationshipTrigger(self.db, enrichment(False)).observe_profile(
            self.account["id"], snapshot(10, 20), snapshot(11, 20), self.now
        )
        self.assertEqual(result.action, "disabled")
        self.assertEqual(self.db.relationship_jobs(), [])

    def test_count_changes_coalesce_and_widen_directions(self):
        self.db.conn.execute(
            """UPDATE accounts SET followers_baseline_at=?,following_baseline_at=? WHERE id=?""",
            (self.now.isoformat(), self.now.isoformat(), self.account["id"]),
        )
        self.db.conn.commit()
        trigger = RelationshipTrigger(self.db, enrichment())
        first = trigger.observe_profile(
            self.account["id"], snapshot(10, 20), snapshot(11, 20), self.now
        )
        second = trigger.observe_profile(
            self.account["id"], snapshot(11, 20), snapshot(11, 21), self.now
        )

        self.assertEqual(first.directions, (Direction.FOLLOWERS,))
        self.assertEqual(second.directions, (Direction.FOLLOWERS, Direction.FOLLOWING))
        jobs = self.db.relationship_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["need_followers"], jobs[0]["need_following"]), (1, 1))

    def test_private_transition_freezes_and_cancels_open_job(self):
        trigger = RelationshipTrigger(self.db, enrichment())
        trigger.observe_profile(self.account["id"], snapshot(10, 20), snapshot(11, 20), self.now)
        result = trigger.observe_profile(
            self.account["id"], snapshot(11, 20), snapshot(11, 20, PrivacyState.PRIVATE), self.now
        )
        self.assertEqual(result.action, "frozen")
        self.assertEqual(self.db.get_account("target")["relationship_status"], "frozen")
        self.assertEqual(self.db.relationship_jobs()[0]["status"], "cancelled")

    def test_reopen_queues_both_directions_but_scope_exceeded_is_not_requested(self):
        trigger = RelationshipTrigger(self.db, enrichment())
        result = trigger.observe_profile(
            self.account["id"], snapshot(1200, 20, PrivacyState.PRIVATE), snapshot(1200, 20), self.now
        )
        self.assertEqual(result.action, "queued")
        self.assertEqual(result.directions, (Direction.FOLLOWING,))
        account = self.db.get_account("target")
        self.assertEqual(account["relationship_status"], "scope_exceeded")


class FakeRelationshipSource:
    def __init__(self, pages=None):
        self.pages = pages or {}

    def login_or_validate_saved_session(self):
        return CollectorIdentity("collector-id", "collector")

    def own_account_health(self):
        return None

    def resolve_public_user(self, username):
        return RelationshipTarget("target-id", username, True)

    def iter_members(self, user_id, direction, page_size, limit):
        yield from self.pages.get(direction, [])


class FatalLoginSource(FakeRelationshipSource):
    def login_or_validate_saved_session(self):
        raise CollectorFatalError("TwoFactorRequired")


class RelationshipWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "state.sqlite3")
        self.db.sync_accounts([
            AccountConfig("https://insta-stories-viewer.com/target/", True, "target")
        ])
        self.account = self.db.get_account("target")
        self.now = datetime(2026, 8, 2, tzinfo=UTC)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_login_requires_72_hours_then_manual_canary_approval(self):
        self.db.record_success(self.account["id"], snapshot(10, 20), [], [])
        admin = CollectorAdministration(self.db, FakeRelationshipSource(), enrichment())
        status = admin.login(self.now)
        self.assertEqual(status.state, "observing")
        self.assertEqual(admin.status(self.now + timedelta(hours=71)).state, "observing")
        self.assertEqual(admin.status(self.now + timedelta(hours=72)).state, "awaiting_approval")
        approved = admin.approve(self.account["id"], self.now + timedelta(hours=72))
        self.assertEqual(approved.state, "canary")
        self.assertEqual(approved.canary_account_id, self.account["id"])

    def test_login_preserves_sanitized_collector_fatal_reason(self):
        status = CollectorAdministration(
            self.db, FatalLoginSource(), enrichment()
        ).login(self.now)
        self.assertEqual(status.state, "risk_hold")
        self.assertEqual(status.risk_reason, "TwoFactorRequired")

    def test_complete_baseline_then_change_creates_only_real_delta(self):
        source = FakeRelationshipSource({
            Direction.FOLLOWERS: [RelationshipPage((MemberIdentity("1", "alice"), MemberIdentity("2", "bob")), True)],
        })
        self.db.set_collector_state("active", self.now.isoformat())
        self.db.enqueue_relationship_job(self.account["id"], True, False, "baseline", self.now.isoformat())
        worker = RelationshipWorker(self.db, enrichment(), source)
        first = worker.run_once(self.now)
        self.assertEqual(first.status, "completed")
        self.assertEqual(self.db.relationship_history(self.account["id"]), [])

        source.pages[Direction.FOLLOWERS] = [
            RelationshipPage((MemberIdentity("2", "bob"), MemberIdentity("3", "cara")), True)
        ]
        later = self.now + timedelta(hours=4)
        self.db.enqueue_relationship_job(self.account["id"], True, False, "count_change", later.isoformat())
        second = worker.run_once(later)
        self.assertEqual(second.status, "completed")
        history = self.db.relationship_history(self.account["id"])
        self.assertEqual(
            {(item["change_kind"], item["username"]) for item in history},
            {("joined", "cara"), ("left", "alice")},
        )

    def test_incomplete_page_never_replaces_complete_baseline(self):
        source = FakeRelationshipSource({
            Direction.FOLLOWERS: [RelationshipPage((MemberIdentity("1", "alice"),), True)],
        })
        self.db.set_collector_state("active", self.now.isoformat())
        self.db.enqueue_relationship_job(self.account["id"], True, False, "baseline", self.now.isoformat())
        worker = RelationshipWorker(self.db, enrichment(), source)
        worker.run_once(self.now)

        source.pages[Direction.FOLLOWERS] = [RelationshipPage((), False)]
        later = self.now + timedelta(hours=4)
        self.db.enqueue_relationship_job(self.account["id"], True, False, "count_change", later.isoformat())
        result = worker.run_once(later)
        self.assertEqual(result.status, "incomplete")
        current = self.db.relationship_memberships(self.account["id"], Direction.FOLLOWERS)
        self.assertEqual([item["username"] for item in current], ["alice"])
        self.assertEqual(self.db.relationship_history(self.account["id"]), [])

    def test_dashboard_lists_relationships_with_server_pagination_route(self):
        avatar = Path(self.tmp.name) / "member-avatar.jpg"
        avatar.write_bytes(b"member-avatar")
        source = FakeRelationshipSource({
            Direction.FOLLOWERS: [RelationshipPage((MemberIdentity(
                "1", "alice", "Alice", "https://instagram-cdn.example/alice.jpg"
            ),), True)],
        })
        self.db.set_collector_state("active", self.now.isoformat())
        self.db.enqueue_relationship_job(self.account["id"], True, False, "baseline", self.now.isoformat())
        RelationshipWorker(self.db, enrichment(), source).run_once(self.now)
        self.db.conn.execute(
            "UPDATE relationship_members SET avatar_path=? WHERE instagram_profile_id='1'",
            (str(avatar),),
        )
        self.db.conn.commit()
        app = create_app(Path(self.tmp.name) / "state.sqlite3")
        response = app.test_client().get(
            f"/account/{self.account['id']}/relationships?tab=followers&q=ali"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"@alice", response.data)
        self.assertIn(b"Followers", response.data)
        self.assertIn(b'/relationship-member/1/avatar', response.data)
        self.assertNotIn(b'instagram-cdn.example', response.data)
        self.assertEqual(
            app.test_client().get("/relationship-member/1/avatar").data,
            b"member-avatar",
        )


class FakeMemberProfileSource:
    async def fetch_profile(self, profile_id, username):
        return ProfileSnapshot(
            username, "Alice", 12, 34, 56, "bio", PrivacyState.PUBLIC,
            "https://cdn/avatar.jpg", observed_at="2026-08-02T00:00:00+00:00",
            avatar_sha256="avatar-hash", avatar_path=f"/avatars/{profile_id}.jpg",
        )


class MemberEnrichmentWorkerTests(unittest.TestCase):
    def test_playwright_source_downloads_avatar_while_profile_session_is_open(self):
        snapshot = ProfileSnapshot(
            "alice", "Alice", 12, 34, 56, "bio", PrivacyState.PUBLIC,
            "https://cdn/avatar.jpg", observed_at="2026-08-02T00:00:00+00:00",
        )

        class FakeScraper:
            def __init__(self, _browser):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def scrape_profile_only(self, _url):
                return snapshot

        save = AsyncMock(return_value=("avatar-hash", "/avatars/1.jpg"))
        with patch("ig_monitor.scraper.ProfileScraper", FakeScraper), patch(
            "ig_monitor.media.save_avatar", save
        ):
            result = asyncio.run(
                PlaywrightMemberProfileSource(object(), Path("/avatars")).fetch_profile(
                    "1", "alice"
                )
            )

        self.assertEqual(result.avatar_sha256, "avatar-hash")
        self.assertEqual(result.avatar_path, "/avatars/1.jpg")
        save.assert_awaited_once()

    def test_profile_only_enrichment_updates_member_and_obeys_durable_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "state.sqlite3")
            now = datetime(2026, 8, 2, tzinfo=UTC)
            try:
                db.conn.execute(
                    """INSERT INTO relationship_members(
                         instagram_profile_id,username,username_observed_at,created_at,updated_at
                       ) VALUES('1','alice',?,?,?)""", (now.isoformat(), now.isoformat(), now.isoformat())
                )
                db.conn.commit()
                db.enqueue_member_enrichment("1", "manual", now.isoformat())
                worker = MemberEnrichmentWorker(
                    db, enrichment(), FakeMemberProfileSource(), random_uniform=lambda _a, _b: 30
                )
                self.assertEqual(worker.run_once(now).status, "completed")
                member = db.relationship_member("1")
                self.assertEqual(member["followers"], 34)
                self.assertEqual(member["avatar_sha256"], "avatar-hash")
                self.assertEqual(member["avatar_path"], "/avatars/1.jpg")
                self.assertEqual(worker.run_once(now + timedelta(seconds=29)).status, "spacing")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
