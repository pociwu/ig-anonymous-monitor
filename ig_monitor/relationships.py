from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from .authenticated_work import AuthenticatedWorkCoordinator
from .config import InstagramEnrichmentConfig
from .db import Database
from .models import PrivacyState, ProfileSnapshot


class Direction(StrEnum):
    FOLLOWERS = "followers"
    FOLLOWING = "following"


@dataclass(frozen=True, slots=True)
class TriggerOutcome:
    action: str
    directions: tuple[Direction, ...] = ()
    job_id: int | None = None


@dataclass(frozen=True, slots=True)
class CollectorIdentity:
    profile_id: str
    username: str


@dataclass(frozen=True, slots=True)
class RelationshipTarget:
    profile_id: str
    username: str
    is_public: bool


@dataclass(frozen=True, slots=True)
class MemberIdentity:
    profile_id: str
    username: str
    display_name: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class RelationshipPage:
    members: tuple[MemberIdentity, ...]
    complete: bool
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorStatus:
    state: str
    observed_since: str | None = None
    canary_account_id: int | None = None
    risk_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkOutcome:
    status: str
    job_id: int | None = None
    detail: str | None = None


class InstagramRelationshipSource(Protocol):
    def login_or_validate_saved_session(self) -> CollectorIdentity: ...
    def own_account_health(self) -> None: ...
    def resolve_public_user(self, username: str) -> RelationshipTarget: ...
    def iter_members(
        self, user_id: str, direction: Direction, page_size: int, limit: int
    ) -> Iterator[RelationshipPage]: ...


class CollectorFatalError(RuntimeError):
    pass


class TargetIneligibleError(RuntimeError):
    pass


def _collector_fatal_reason(exc: CollectorFatalError) -> str:
    reason = str(exc).strip()
    return reason if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", reason) else "CollectorFatalError"


class RelationshipTrigger:
    """Turns anonymous profile observations into durable relationship work."""

    def __init__(self, db: Database, config: InstagramEnrichmentConfig):
        self.db = db
        self.config = config

    def observe_profile(
        self,
        account_id: int,
        previous: ProfileSnapshot | None,
        current: ProfileSnapshot,
        observed_at: datetime,
    ) -> TriggerOutcome:
        if not self.config.enabled:
            return TriggerOutcome("disabled")
        account = self.db.get_account_by_id(account_id)
        if not account or not account.get("relationship_tracking"):
            return TriggerOutcome("account_disabled")

        when = observed_at.isoformat(timespec="seconds")
        if current.privacy != PrivacyState.PUBLIC:
            self.db.freeze_relationships(account_id, when)
            return TriggerOutcome("frozen")

        reopened = previous is not None and previous.privacy != PrivacyState.PUBLIC
        first_observation = previous is None
        followers_missing = account.get("followers_baseline_at") is None
        following_missing = account.get("following_baseline_at") is None
        need_followers = followers_missing or first_observation or reopened or previous.followers != current.followers
        need_following = following_missing or first_observation or reopened or previous.following != current.following

        followers_scoped = current.followers <= self.config.member_limit_per_direction
        following_scoped = current.following <= self.config.member_limit_per_direction
        need_followers = need_followers and followers_scoped
        need_following = need_following and following_scoped
        if not followers_scoped or not following_scoped:
            self.db.set_relationship_status(account_id, "scope_exceeded")
        elif reopened:
            self.db.set_relationship_status(account_id, "queued")

        directions = tuple(
            direction
            for direction, needed in (
                (Direction.FOLLOWERS, need_followers),
                (Direction.FOLLOWING, need_following),
            )
            if needed
        )
        if not directions:
            return TriggerOutcome("scope_exceeded" if (not followers_scoped or not following_scoped) else "unchanged")

        reason = "reopened" if reopened else "baseline" if (first_observation or followers_missing or following_missing) else "count_change"
        job_id = self.db.enqueue_relationship_job(
            account_id, need_followers, need_following, reason, when
        )
        job = self.db.get_relationship_job(job_id)
        widened = tuple(
            direction for direction, needed in (
                (Direction.FOLLOWERS, job["need_followers"]),
                (Direction.FOLLOWING, job["need_following"]),
            ) if needed
        )
        return TriggerOutcome("queued", widened, job_id)


class CollectorAdministration:
    def __init__(
        self, db: Database, source: InstagramRelationshipSource,
        config: InstagramEnrichmentConfig,
    ):
        self.db = db
        self.source = source
        self.config = config

    def status(self, now: datetime) -> CollectorStatus:
        row = self.db.collector_state()
        if row["state"] == "observing" and row.get("observed_since"):
            observed = datetime.fromisoformat(row["observed_since"])
            last_health = datetime.fromisoformat(row["last_health_check_at"]) if row.get("last_health_check_at") else None
            if last_health is None or now - last_health >= timedelta(hours=24):
                try:
                    self.source.own_account_health()
                    self.db.update_collector_health(now.isoformat(timespec="seconds"))
                except CollectorFatalError as exc:
                    self.db.place_collector_risk_hold(_collector_fatal_reason(exc))
            if now - observed >= timedelta(hours=self.config.observation_hours):
                latest = self.db.collector_state()
                if latest["state"] == "observing":
                    self.db.set_collector_state("awaiting_approval", now.isoformat(timespec="seconds"))
        return self._status()

    def login(self, now: datetime) -> CollectorStatus:
        try:
            identity = self.source.login_or_validate_saved_session()
        except CollectorFatalError as exc:
            self.db.place_collector_risk_hold(_collector_fatal_reason(exc))
            return self._status()
        if not identity.profile_id:
            raise ValueError("collector identity is missing")
        self.db.begin_collector_observation(now.isoformat(timespec="seconds"))
        return self._status()

    def approve(self, canary_account_id: int, now: datetime) -> CollectorStatus:
        row = self.db.collector_state()
        if row["state"] != "awaiting_approval":
            raise ValueError("collector must complete observation before approval")
        account = self.db.get_account_by_id(canary_account_id)
        if not account or not account["enabled"] or not account["relationship_tracking"]:
            raise ValueError("canary account is not eligible")
        snapshot = self.db.snapshot_from_row(account)
        if (
            snapshot is None or snapshot.privacy != PrivacyState.PUBLIC
            or snapshot.followers > self.config.member_limit_per_direction
            or snapshot.following > self.config.member_limit_per_direction
        ):
            raise ValueError("canary account must be public and within the relationship limits")
        self.db.approve_collector_canary(canary_account_id, now.isoformat(timespec="seconds"))
        self.db.enqueue_relationship_job(
            canary_account_id, True, True, "canary", now.isoformat(timespec="seconds")
        )
        return self._status()

    def begin_recovery(self, now: datetime) -> CollectorStatus:
        if self.db.collector_state()["state"] != "risk_hold":
            raise ValueError("collector is not in risk_hold")
        try:
            self.source.own_account_health()
        except CollectorFatalError as exc:
            self.db.place_collector_risk_hold(_collector_fatal_reason(exc))
            return self._status()
        self.db.begin_collector_observation(now.isoformat(timespec="seconds"))
        return self._status()

    def _status(self) -> CollectorStatus:
        row = self.db.collector_state()
        return CollectorStatus(
            row["state"], row.get("observed_since"), row.get("canary_account_id"), row.get("risk_reason")
        )


class RelationshipWorker:
    def __init__(
        self,
        db: Database,
        config: InstagramEnrichmentConfig,
        source: InstagramRelationshipSource,
        sleeper: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        coordinator: AuthenticatedWorkCoordinator | None = None,
    ):
        self.db = db
        self.config = config
        self.source = source
        self.sleeper = sleeper
        self.random_uniform = random_uniform
        self.coordinator = coordinator or AuthenticatedWorkCoordinator(
            db,
            daily_start_limit=config.daily_relationship_jobs,
            minimum_start_interval_minutes=config.minimum_job_interval_minutes,
        )

    def run_once(self, now: datetime) -> WorkOutcome:
        self.db.prune_relationship_data(now)
        if not self.config.enabled:
            return WorkOutcome("disabled")
        collector = self.db.collector_state()
        if collector["state"] not in ("canary", "active"):
            return WorkOutcome("collector_unavailable", detail=collector["state"])
        if collector["state"] == "active":
            self.db.enqueue_due_reconciliations(
                now, self.config.reconciliation_days, self.random_uniform
            )
        canary_final = False
        if collector["state"] == "canary" and collector.get("canary_started_at"):
            canary_final = now >= datetime.fromisoformat(collector["canary_started_at"]) + timedelta(
                days=self.config.canary_days
            )
            if canary_final and not self.db.has_open_relationship_job(collector["canary_account_id"]):
                self.db.enqueue_relationship_job(
                    collector["canary_account_id"], True, True, "canary_final",
                    now.isoformat(timespec="seconds"),
                )
        decision = self.coordinator.claim_next(
            now,
            relationship_canary_account_id=(
                collector.get("canary_account_id") if collector["state"] == "canary" else None
            ),
        )
        if decision.claim is None:
            return WorkOutcome(decision.status)
        claim = decision.claim
        job = claim.payload

        def completed(outcome: WorkOutcome) -> WorkOutcome:
            self.coordinator.finish(claim, outcome.status, outcome.detail)
            return outcome

        account = self.db.get_account_by_id(job["account_id"])
        if not account:
            self.db.finish_relationship_job(job["id"], "cancelled", "account missing")
            return completed(WorkOutcome("cancelled", job["id"]))
        username = (account.get("effective_url") or account["url"]).rstrip("/").rsplit("/", 1)[-1]
        try:
            target = self.source.resolve_public_user(username)
            if not target.is_public:
                self.db.freeze_relationships(account["id"], now.isoformat(timespec="seconds"))
                self.db.finish_relationship_job(job["id"], "cancelled", "target ineligible")
                return completed(WorkOutcome("target_ineligible", job["id"]))
            if account.get("instagram_profile_id") and account["instagram_profile_id"] != target.profile_id:
                self.db.record_identity_conflict(account["id"], target.profile_id, now.isoformat(timespec="seconds"))
                self.db.finish_relationship_job(job["id"], "failed", "identity conflict")
                return completed(WorkOutcome("identity_conflict", job["id"]))
            if not account.get("instagram_profile_id"):
                self.db.set_verified_identity(account["id"], target.profile_id, now.isoformat(timespec="seconds"))
            elif account.get("identity_verified_source") != "instagrapi":
                self.db.set_verified_identity(account["id"], target.profile_id, now.isoformat(timespec="seconds"))

            requested = [
                direction for direction, flag in (
                    (Direction.FOLLOWERS, job["need_followers"]),
                    (Direction.FOLLOWING, job["need_following"]),
                ) if flag
            ]
            snapshot = self.db.snapshot_from_row(account)
            if snapshot:
                requested = [
                    direction for direction in requested
                    if (
                        snapshot.followers if direction == Direction.FOLLOWERS else snapshot.following
                    ) <= self.config.member_limit_per_direction
                ]
            if not requested:
                self.db.set_relationship_status(account["id"], "scope_exceeded")
                self.db.finish_relationship_job(job["id"], "completed")
                return completed(WorkOutcome("scope_exceeded", job["id"]))
            overall = "completed"
            for index, direction in enumerate(requested):
                if index:
                    self.sleeper(self.random_uniform(
                        self.config.direction_delay_min_seconds,
                        self.config.direction_delay_max_seconds,
                    ))
                result = self._collect_direction(job, account, target, direction, now)
                if result != "completed":
                    overall = result
            self.db.finish_relationship_job(job["id"], "completed" if overall == "completed" else "failed")
            if overall == "completed":
                self.db.finalize_relationship_account(account["id"], now.isoformat(timespec="seconds"))
            if overall == "completed" and canary_final and job["account_id"] == collector.get("canary_account_id"):
                self.db.set_collector_state("active", now.isoformat(timespec="seconds"))
            return completed(WorkOutcome(overall, job["id"]))
        except CollectorFatalError as exc:
            reason = _collector_fatal_reason(exc)
            self.db.place_collector_risk_hold(reason)
            self.db.finish_relationship_job(job["id"], "failed", reason)
            return completed(WorkOutcome("risk_hold", job["id"], reason))
        except TargetIneligibleError:
            self.db.freeze_relationships(account["id"], now.isoformat(timespec="seconds"))
            self.db.finish_relationship_job(job["id"], "cancelled", "target ineligible")
            return completed(WorkOutcome("target_ineligible", job["id"]))
        except Exception as exc:
            self.db.finish_relationship_job(job["id"], "failed", type(exc).__name__)
            return completed(WorkOutcome("incomplete", job["id"], type(exc).__name__))

    def _collect_direction(self, job, account, target, direction, now) -> str:
        run_id = self.db.start_relationship_run(job["id"], account["id"], direction.value, now.isoformat(timespec="seconds"))
        complete = False
        count = 0
        try:
            pages = self.source.iter_members(
                target.profile_id, direction, self.config.page_size,
                self.config.member_limit_per_direction,
            )
            for page_index, page in enumerate(pages):
                if page_index:
                    self.sleeper(self.random_uniform(
                        self.config.page_delay_min_seconds, self.config.page_delay_max_seconds
                    ))
                count += len(page.members)
                if count > self.config.member_limit_per_direction:
                    self.db.finish_relationship_run(run_id, "scope_exceeded", False, count, "limit exceeded")
                    self.db.set_relationship_status(account["id"], "scope_exceeded")
                    return "scope_exceeded"
                self.db.stage_relationship_members(run_id, page.members)
                complete = page.complete
            if not complete:
                self.db.finish_relationship_run(run_id, "incomplete", False, count)
                return "incomplete"
            self.db.apply_complete_relationship_run(
                run_id, account["id"], direction.value, now.isoformat(timespec="seconds"),
                self.config.member_stale_days,
            )
            self.db.finish_relationship_run(run_id, "complete", True, count)
            return "completed"
        except Exception as exc:
            self.db.finish_relationship_run(run_id, "incomplete", False, count, type(exc).__name__)
            raise
