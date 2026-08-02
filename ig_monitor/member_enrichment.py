from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .config import BrowserConfig, InstagramEnrichmentConfig
from .db import Database
from .models import ProfileSnapshot
from .relationships import WorkOutcome


class AnonymousMemberProfileSource(Protocol):
    async def fetch_profile(self, username: str) -> ProfileSnapshot: ...


@dataclass(slots=True)
class PlaywrightMemberProfileSource:
    browser: BrowserConfig

    async def fetch_profile(self, username: str) -> ProfileSnapshot:
        from .scraper import ProfileScraper
        async with ProfileScraper(self.browser) as scraper:
            return await scraper.scrape_profile_only(
                f"https://insta-stories-viewer.com/{username}/"
            )


class MemberEnrichmentWorker:
    def __init__(
        self, db: Database, config: InstagramEnrichmentConfig,
        source: AnonymousMemberProfileSource,
        random_uniform=random.uniform,
    ):
        self.db = db
        self.config = config
        self.source = source
        self.random_uniform = random_uniform

    def run_once(self, now: datetime) -> WorkOutcome:
        if not self.config.enabled:
            return WorkOutcome("disabled")
        if self.db.member_enrichment_count_for_taipei_day(now) >= self.config.daily_member_enrichments:
            return WorkOutcome("daily_budget")
        next_at = self.db.get_meta("member_enrichment_next_at")
        if next_at:
            if now < datetime.fromisoformat(next_at):
                return WorkOutcome("spacing")
        job = self.db.claim_member_enrichment_job(now.isoformat(timespec="seconds"))
        if not job:
            return WorkOutcome("idle")
        member = self.db.relationship_member(job["instagram_profile_id"])
        if not member:
            self.db.finish_member_enrichment_job(job["id"], "cancelled", "member missing")
            return WorkOutcome("cancelled", job["id"])
        try:
            snapshot = asyncio.run(self.source.fetch_profile(member["username"]))
            self.db.apply_member_profile(job, snapshot, now.isoformat(timespec="seconds"))
            self._set_next_at(now)
            return WorkOutcome("completed", job["id"])
        except Exception as exc:
            retry_at = now + timedelta(hours=self.config.member_retry_min_hours)
            self.db.retry_member_enrichment_job(
                job["id"], retry_at.isoformat(timespec="seconds"), type(exc).__name__
            )
            self._set_next_at(now)
            return WorkOutcome("incomplete", job["id"], type(exc).__name__)

    def _set_next_at(self, now: datetime) -> None:
        delay = self.random_uniform(
            self.config.member_delay_min_seconds, self.config.member_delay_max_seconds
        )
        self.db.set_meta(
            "member_enrichment_next_at",
            (now + timedelta(seconds=delay)).isoformat(timespec="seconds"),
        )
