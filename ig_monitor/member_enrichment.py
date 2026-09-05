from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .config import BrowserConfig, InstagramEnrichmentConfig, canonical_account_url
from .db import Database
from .models import ProfileSnapshot, ScrapeFailure
from .relationships import WorkOutcome


class AnonymousMemberProfileSource(Protocol):
    async def fetch_profile(
        self, profile_id: str, username: str, avatar_url: str | None
    ) -> ProfileSnapshot: ...


def _is_instagram_avatar_url(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").casefold()
    return host.endswith((".cdninstagram.com", ".fbcdn.net"))


@dataclass(slots=True)
class PlaywrightMemberProfileSource:
    browser: BrowserConfig
    avatar_root: Path
    db: Database | None = None

    async def fetch_profile(
        self, profile_id: str, username: str, avatar_url: str | None
    ) -> ProfileSnapshot:
        from .media import save_avatar
        from .scraper import ProfileScraper
        modern = getattr(self.browser, "anonymous_source", "legacy") == "anonyig"
        profile_url = canonical_account_url(username) if modern else f"https://insta-stories-viewer.com/{username}/"
        async with ProfileScraper(self.browser) as scraper:
            if self.db:
                scraper.source_guard = lambda: bool(self.db.source_cooldown(self.browser.anonymous_source))
            snapshot = await scraper.scrape_profile_only(profile_url)
            if modern and str(getattr(scraper, "last_profile_id", "")) != str(profile_id):
                raise ScrapeFailure("成員來源 ID 與關係名單身分不一致", "驗證成員身分")
            if _is_instagram_avatar_url(avatar_url):
                try:
                    digest, path = await save_avatar(
                        scraper, self.avatar_root, profile_id, avatar_url,
                        "https://www.instagram.com/",
                    )
                    snapshot.avatar_sha256 = digest
                    snapshot.avatar_path = path
                except (OSError, RuntimeError, ValueError):
                    pass
            if snapshot.avatar_path is None and snapshot.privacy.value == "public" and snapshot.avatar_url:
                digest, path = await save_avatar(
                    scraper, self.avatar_root, profile_id, snapshot.avatar_url,
                    getattr(scraper, "media_referer", None) or profile_url,
                )
                snapshot.avatar_sha256 = digest
                snapshot.avatar_path = path
            return snapshot


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
        source_name = getattr(getattr(self.source, "browser", None), "anonymous_source", None)
        if source_name and self.db.source_cooldown(source_name, now):
            return WorkOutcome("source_cooldown")
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
            snapshot = asyncio.run(self.source.fetch_profile(
                member["instagram_profile_id"], member["username"], member.get("avatar_url")
            ))
            self.db.apply_member_profile(job, snapshot, now.isoformat(timespec="seconds"))
            self._set_next_at(now)
            return WorkOutcome("completed", job["id"])
        except Exception as exc:
            if isinstance(exc, ScrapeFailure) and exc.blocker and source_name:
                self.db.record_source_block(source_name, str(exc), now)
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
