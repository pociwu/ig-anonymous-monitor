from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .apify import ApifyClient, ApifyError, IdentityResult
from .db import Database
from .media import download_account_media, save_avatar
from .models import PrivacyState, ScrapeFailure
from .relationships import RelationshipTrigger
from .scraper import ProfileScraper
from .telegram import TelegramSender
from .utils import save_diagnostic, sha256_bytes, snapshot_changes, stable_key


LOG = logging.getLogger("ig_monitor")


async def check_accounts(config: AppConfig) -> int:
    failures = 0
    enabled = [a for a in config.accounts if a.enabled]
    async with ProfileScraper(config.browser) as scraper:
        for index, account in enumerate(enabled):
            if index:
                await asyncio.sleep(random.uniform(config.schedule.account_delay_min_seconds,
                                                   config.schedule.account_delay_max_seconds))
            try:
                result = await scraper.scrape(account.url)
                avatar, _ = await scraper.download(result.snapshot.avatar_url, account.url)
                result.snapshot.avatar_sha256 = sha256_bytes(avatar)
                LOG.info("CHECK %s: %s, posts=%d followers=%d following=%d media=%d",
                         account.label, result.snapshot.privacy.value, result.snapshot.posts,
                         result.snapshot.followers, result.snapshot.following, len(result.media))
            except Exception as exc:
                failures += 1
                LOG.error("CHECK %s 失敗：%s", account.label, exc)
    return 1 if failures else 0


class Monitor:
    def __init__(self, config: AppConfig, db: Database, apify_client: ApifyClient | None = None):
        self.config = config
        self.db = db
        self.telegram = TelegramSender(config.telegram)
        self.relationship_trigger = RelationshipTrigger(db, config.instagram_enrichment)
        self.apify = apify_client or (ApifyClient(config.apify) if config.apify.enabled else None)
        for path in (config.paths.data_dir, config.paths.download_root, config.paths.diagnostics_dir,
                     config.browser.browsers_path):
            path.mkdir(parents=True, exist_ok=True)

    async def run(self) -> int:
        self.db.sync_accounts(self.config.accounts)
        run_id = self.db.start_run()
        failures = 0
        opened: set[int] = set()
        enabled = self.db.enabled_accounts()
        LOG.info("開始監控 %d 個帳號", len(enabled))
        try:
            if self.apify:
                try:
                    await self.apify.enforce_monthly_limit()
                except ApifyError as exc:
                    # A remote account cap is the hard guard. Do not spend if it cannot be verified.
                    LOG.warning("Apify identity resolution disabled for this run: %s", exc)
                    self.apify = None
            async with ProfileScraper(self.config.browser) as scraper:
                for index, account in enumerate(enabled):
                    if index:
                        delay = random.uniform(self.config.schedule.account_delay_min_seconds,
                                               self.config.schedule.account_delay_max_seconds)
                        LOG.info("等待 %.1f 秒後檢查下一個帳號", delay)
                        await asyncio.sleep(delay)
                    try:
                        target_url = account.get("effective_url") or account["url"]
                        result = await scraper.scrape(target_url)
                        avatar_hash, avatar_path = await save_avatar(
                            scraper, self.config.paths.download_root, account["account_key"],
                            result.snapshot.avatar_url, account["url"],
                        )
                        result.snapshot.avatar_sha256 = avatar_hash
                        result.snapshot.avatar_path = avatar_path
                        old = self.db.snapshot_from_row(account)
                        events: list[tuple[str, str, dict]] = []
                        if account["failure_notified"]:
                            events.append((f"recovery:{account['id']}:{result.snapshot.observed_at}", "recovery",
                                           {"label": account["label"]}))
                        if old is None:
                            payload = {"label": account["label"], "snapshot": result.snapshot.to_dict(),
                                       "avatar_path": avatar_path}
                            events.append((f"initial:{account['id']}", "initial", payload))
                        else:
                            changes = snapshot_changes(old, result.snapshot)
                            if changes:
                                serial = {key: [self._json_value(pair[0]), self._json_value(pair[1])]
                                          for key, pair in changes.items()}
                                payload = {"label": account["label"], "changes": serial,
                                           "old_avatar_path": old.avatar_path,
                                           "new_avatar_path": result.snapshot.avatar_path}
                                event_key = f"change:{account['id']}:{stable_key(json.dumps(serial, sort_keys=True, ensure_ascii=False))}"
                                events.append((event_key, "change", payload))
                                privacy = changes.get("privacy")
                                if privacy and privacy[0].value == "private" and privacy[1].value == "public":
                                    opened.add(account["id"])
                        self.db.record_success(account["id"], result.snapshot, events, result.media)
                        observed_at = (
                            datetime.fromisoformat(result.snapshot.observed_at)
                            if result.snapshot.observed_at else datetime.now(UTC)
                        )
                        self.relationship_trigger.observe_profile(
                            account["id"], old, result.snapshot, observed_at
                        )
                        if self.apify and not account.get("instagram_profile_id"):
                            await self._enrol_identity(account, result.snapshot.username)
                        LOG.info("%s 載入成功：%s，發現媒體 %d", account["label"],
                                 result.snapshot.privacy.value, len(result.media))
                    except ScrapeFailure as exc:
                        failures += 1
                        save_diagnostic(self.config.paths.diagnostics_dir, account["account_key"], exc.html,
                                        exc.screenshot, str(exc), self.config.retention.diagnostic_runs)
                        count = self.db.record_failure(account["id"], account["label"], str(exc), exc.blocker)
                        if self.apify and account.get("instagram_profile_id"):
                            await self._recover_username(account)
                        LOG.error("%s 載入失敗（連續 %d 次）：%s", account["label"], count, exc)
                    except Exception as exc:
                        failures += 1
                        count = self.db.record_failure(account["id"], account["label"], str(exc), None)
                        LOG.exception("%s 處理失敗（連續 %d 次）", account["label"], count)

                refreshed = {row["id"]: row for row in self.db.enabled_accounts()}
                for account_id, account in refreshed.items():
                    current = self.db.snapshot_from_row(account)
                    if account["fail_count"] or current is None or current.privacy != PrivacyState.PUBLIC:
                        continue
                    stats = await download_account_media(self.db, scraper, account,
                                                         self.config.paths.download_root,
                                                         self.config.schedule.media_limit_per_account,
                                                         self.config.dedup)
                    attachments = stats.pop("attachments", [])
                    if self.config.telegram.send_new_media:
                        stats["attachments"] = attachments[:self.config.telegram.max_new_media_attachments]
                        stats["attachment_total"] = len(attachments)
                    if stats["downloaded"] or stats["failed"] or account_id in opened:
                        payload = {"label": account["label"], **stats}
                        self.db.enqueue_event(f"media:{run_id}:{account_id}", "media_summary", payload, account_id)
                    LOG.info("%s 媒體：新增 %d、重複 %d、失敗 %d、待下載 %d", account["label"],
                             stats["downloaded"], stats["duplicate"], stats["failed"], stats["pending"])

            self._enqueue_heartbeat_if_due()
            self.db.enqueue_relationship_watchdogs(datetime.now(UTC))
            self._backup_if_due()
            sent, send_failed = await self.telegram.deliver_pending(self.db)
            LOG.info("Telegram：成功 %d、失敗 %d", sent, send_failed)
            status = "partial" if failures or send_failed else "success"
            self.db.finish_run(run_id, status, f"account_failures={failures}, telegram_failures={send_failed}")
            return 1 if failures else 0
        except Exception as exc:
            self.db.finish_run(run_id, "failed", str(exc))
            raise

    @staticmethod
    def _json_value(value):
        return value.value if hasattr(value, "value") else value

    async def _enrol_identity(self, account: dict, username: str) -> None:
        identity = await self._resolve_identity(account, username)
        if identity:
            self.db.set_identity(account["id"], identity.profile_id, identity.username)
            LOG.info("%s saved Instagram Profile ID", account["label"])

    async def _recover_username(self, account: dict) -> None:
        old_username = (account.get("effective_url") or account["url"]).rstrip("/").rsplit("/", 1)[-1]
        identity = await self._resolve_identity(account, account["instagram_profile_id"])
        if not identity:
            return
        self.db.set_identity(account["id"], identity.profile_id, identity.username)
        if identity.username.casefold() != old_username.casefold():
            self.db.enqueue_event(
                f"username-change:{account['id']}:{old_username}:{identity.username}",
                "username_change",
                {"label": account["label"], "old_username": old_username, "new_username": identity.username},
                account["id"],
            )
            LOG.info("%s username changed: %s -> %s", account["label"], old_username, identity.username)

    async def _resolve_identity(self, account: dict, identifier: str) -> IdentityResult | None:
        if not self.apify:
            return None
        try:
            usage = await self.apify.usage_state()
            reservation = self.config.apify.request_reservation_usd
            local = self.db.apify_reserved_total(usage.cycle_key)
            exhausted = (usage.current_usd + reservation > self.config.apify.monthly_cap_usd
                         or local + reservation > self.config.apify.monthly_cap_usd)
            if exhausted:
                if not self.db.budget_notice_sent(usage.cycle_key):
                    self.db.enqueue_event(
                        f"apify-budget:{usage.cycle_key}", "apify_budget_exhausted",
                        {"cycle_key": usage.cycle_key, "cap_usd": self.config.apify.monthly_cap_usd},
                    )
                    self.db.mark_budget_notice_sent(usage.cycle_key)
                return None
            self.db.reserve_apify_usage(account["id"], usage.cycle_key, reservation)
            return await self.apify.resolve(identifier)
        except ApifyError as exc:
            LOG.warning("Apify identity resolution for %s failed: %s", account["label"], exc)
            return None

    def _enqueue_heartbeat_if_due(self) -> None:
        cfg = self.config.heartbeat
        if not cfg.enabled:
            return
        now = datetime.now(ZoneInfo(cfg.timezone))
        hour, minute = (int(x) for x in cfg.time.split(":"))
        today = now.date().isoformat()
        if (now.hour, now.minute) < (hour, minute) or self.db.get_meta("heartbeat_date") == today:
            return
        self.db.enqueue_event(f"heartbeat:{today}", "heartbeat", self.db.summary())
        self.db.set_meta("heartbeat_date", today)

    def _backup_if_due(self) -> None:
        today = datetime.now().date().isoformat()
        if self.db.get_meta("backup_date") == today:
            return
        directory = self.config.paths.data_dir / "backups"
        destination = directory / f"state-{today}.sqlite3"
        self.db.backup(destination)
        self.db.set_meta("backup_date", today)
        backups = sorted(directory.glob("state-*.sqlite3"), reverse=True)
        for old in backups[self.config.retention.database_backups:]:
            old.unlink(missing_ok=True)
