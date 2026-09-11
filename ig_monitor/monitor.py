from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig, account_username
from .apify import ApifyClient, ApifyError, IdentityResult
from .avatar import reconcile_avatar
from .db import Database
from .media import download_account_media, save_avatar
from .models import COLLECTIONS, PrivacyState, ScrapeFailure, ScrapeResult, TerminalState
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
                if any(value.state in {TerminalState.FAILED, TerminalState.BLOCKED, TerminalState.UNKNOWN}
                       or value.error for value in result.collections.values()):
                    raise ScrapeFailure("一個或多個媒體分類未完成驗證", "檢查分類",
                                        blocker="source_blocked" if any(
                                            value.state == TerminalState.BLOCKED
                                            for value in result.collections.values()) else None)
                avatar, _ = await scraper.download(result.snapshot.avatar_url,
                                                    getattr(scraper, "media_referer", None) or account.url)
                result.snapshot.avatar_sha256 = sha256_bytes(avatar)
                LOG.info("CHECK %s: %s, posts=%d followers=%d following=%d media=%d",
                         account.label, result.snapshot.privacy.value, result.snapshot.posts,
                         result.snapshot.followers, result.snapshot.following, len(result.media))
            except Exception as exc:
                failures += 1
                LOG.error("CHECK %s 失敗：%s", account.label, exc)
                if isinstance(exc, ScrapeFailure) and exc.blocker:
                    break
    return 1 if failures else 0


class Monitor:
    def __init__(self, config: AppConfig, db: Database, apify_client: ApifyClient | None = None):
        self.config = config
        self.db = db
        self.telegram = TelegramSender(config.telegram)
        self.relationship_trigger = RelationshipTrigger(db, config.instagram_enrichment)
        self.apify = apify_client or (ApifyClient(config.apify) if config.apify.enabled else None)
        for path in (config.paths.data_dir, config.paths.download_root, config.paths.diagnostics_dir):
            path.mkdir(parents=True, exist_ok=True)
        # The HTTP-only IGWatcher adapter must not create a browser cache,
        # especially when its unused default is inside a read-only config mount.
        if config.browser.anonymous_source in {"legacy", "anonyig"}:
            config.browser.browsers_path.mkdir(parents=True, exist_ok=True)

    async def run(self) -> int:
        self.db.sync_accounts(self.config.accounts)
        run_id = self.db.start_run()
        source = self.config.browser.anonymous_source
        cooldown = self.db.source_cooldown(source)
        if cooldown:
            LOG.warning("匿名來源 %s 冷卻中，下次允許：%s", source, cooldown["next_allowed_at"])
            await self.telegram.deliver_pending(self.db)
            self.db.finish_run(run_id, "cooldown", f"source={source}, next_allowed_at={cooldown['next_allowed_at']}")
            return 0
        failures = 0
        source_access_succeeded = False
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
                scraper.source_guard = lambda: bool(self.db.source_cooldown(source))
                for index, account in enumerate(enabled):
                    if self.db.source_cooldown(source):
                        break
                    if index:
                        delay = random.uniform(self.config.schedule.account_delay_min_seconds,
                                               self.config.schedule.account_delay_max_seconds)
                        LOG.info("等待 %.1f 秒後檢查下一個帳號", delay)
                        await asyncio.sleep(delay)
                    if self.db.source_cooldown(source):
                        break
                    blocked = None
                    try:
                        target_url = account.get("effective_url") or account["url"]
                        if getattr(scraper, "supports_progress", False):
                            progress = self.db.collection_observations(account["id"], source)
                            cursors = {key: value["cursor"] for key, value in progress.items()
                                       if value.get("cursor")} if self.config.schedule.media_download_enabled else {}
                            known = {key: self.db.known_source_ids(account["id"], key, source)
                                     for key in COLLECTIONS} if self.config.schedule.media_download_enabled else {}
                            completed = {key for key, value in progress.items()
                                         if value.get("baseline_complete")} if self.config.schedule.media_download_enabled else set()
                            result = await scraper.scrape(target_url, cursors=cursors, known_ids=known,
                                                          completed_categories=completed)
                        else:
                            result = await scraper.scrape(target_url)
                        blocked = next((value.error or "來源要求驗證或限流"
                                        for value in result.collections.values()
                                        if value.state == TerminalState.BLOCKED), None)
                        if blocked:
                            self.db.record_source_block(source, blocked)
                            LOG.error("%s：來源暫停，%s", account["label"], blocked)
                            try:
                                save_diagnostic(self.config.paths.diagnostics_dir, account["account_key"],
                                                None, None, blocked, self.config.retention.diagnostic_runs)
                            except OSError:
                                LOG.warning("%s：診斷檔無法寫入；已保留日誌與來源冷卻紀錄", account["label"])
                        self._validate_source_identity(account, target_url, result)
                        old = self.db.snapshot_from_row(account)
                        if blocked:
                            # No more source requests, including avatar or media downloads.
                            avatar_hash = old.avatar_sha256 if old else None
                            avatar_path = old.avatar_path if old else None
                        else:
                            referer = getattr(scraper, "media_referer", None) or target_url
                            try:
                                avatar_hash, avatar_path = await save_avatar(
                                    scraper, self.config.paths.download_root, account["account_key"],
                                    result.snapshot.avatar_url, referer,
                                )
                            except ScrapeFailure as exc:
                                if not exc.blocker:
                                    raise
                                # Keep already verified collections even if the subsequent
                                # avatar request reveals a source-wide challenge.
                                blocked = str(exc)
                                self.db.record_source_block(source, blocked)
                                avatar_hash = old.avatar_sha256 if old else None
                                avatar_path = old.avatar_path if old else None
                        result.snapshot.avatar_sha256 = avatar_hash
                        result.snapshot.avatar_path = avatar_path
                        same_avatar_content = reconcile_avatar(old, result.snapshot)
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
                            if same_avatar_content:
                                changes.pop("avatar_sha256", None)
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
                        recorded_media = result.media if self.config.schedule.media_download_enabled else []
                        self.db.record_success(account["id"], result.snapshot, events, recorded_media)
                        if result.groups:
                            # Counts describe source observations, not downloaded files;
                            # preserve them even while media recording is disabled.
                            self.db.record_group_observations(account["id"], source, result.groups)
                        self.db.set_meta(f"anonymous_active_source:{account['id']}", source)
                        if result.profile_id:
                            self.db.set_meta(f"anonymous_profile_id:{account['id']}:{source}", result.profile_id)
                        if result.collections:
                            self.db.record_collection_observations(
                                account["id"], account["label"], result.collections, source,
                                persist_progress=self.config.schedule.media_download_enabled,
                            )
                        if blocked:
                            self.db.record_source_block(source, blocked)
                            failures += 1
                        elif any(value.error or value.state in {TerminalState.FAILED, TerminalState.UNKNOWN}
                                 for value in result.collections.values()):
                            failures += 1
                        if not blocked and result.source == source:
                            source_access_succeeded = True
                        observed_at = (
                            datetime.fromisoformat(result.snapshot.observed_at)
                            if result.snapshot.observed_at else datetime.now(UTC)
                        )
                        self.relationship_trigger.observe_profile(
                            account["id"], old, result.snapshot, observed_at
                        )
                        if self.apify and not blocked and not account.get("instagram_profile_id"):
                            await self._enrol_identity(account, result.snapshot.username)
                        if self.config.schedule.media_download_enabled:
                            LOG.info("%s 載入成功：%s，發現媒體 %d", account["label"],
                                     result.snapshot.privacy.value, len(result.media))
                            if source == "igwatcher":
                                LOG.warning("%s：IGWatcher 保守模式，%d 筆候選歸屬待確認；不代表完整性驗收通過",
                                            account["label"], sum(item.ownership_status == "pending" for item in result.media))
                        else:
                            LOG.warning("%s 載入成功：%s；媒體記錄與下載目前已暫停，忽略候選 %d 筆",
                                        account["label"], result.snapshot.privacy.value, len(result.media))
                        if blocked:
                            break
                    except ScrapeFailure as exc:
                        failures += 1
                        save_diagnostic(self.config.paths.diagnostics_dir, account["account_key"], exc.html,
                                        exc.screenshot, str(exc), self.config.retention.diagnostic_runs)
                        if exc.blocker or blocked:
                            self.db.record_source_block(source, str(exc))
                            LOG.error("%s：來源暫停，%s", account["label"], exc)
                            break
                        count = self.db.record_failure(account["id"], account["label"], str(exc), exc.blocker)
                        if self.apify and account.get("instagram_profile_id"):
                            await self._recover_username(account)
                        LOG.error("%s 載入失敗（連續 %d 次）：%s", account["label"], count, exc)
                    except Exception as exc:
                        failures += 1
                        count = self.db.record_failure(account["id"], account["label"], str(exc), None)
                        LOG.exception("%s 處理失敗（連續 %d 次）", account["label"], count)
                        if self.db.source_cooldown(source):
                            break

                if self.config.schedule.media_download_enabled:
                    refreshed = {row["id"]: row for row in self.db.enabled_accounts()}
                    for account_id, account in refreshed.items():
                        if self.db.source_cooldown(source):
                            break
                        current = self.db.snapshot_from_row(account)
                        if account["fail_count"] or current is None or current.privacy != PrivacyState.PUBLIC:
                            continue
                        try:
                            stats = await download_account_media(self.db, scraper, account,
                                                                 self.config.paths.download_root,
                                                                 self.config.schedule.media_limit_per_account,
                                                                 self.config.dedup)
                        except ScrapeFailure as exc:
                            if not exc.blocker:
                                raise
                            self.db.record_source_block(source, str(exc))
                            failures += 1
                            break
                        attachments = stats.pop("attachments", [])
                        if source == "igwatcher" and stats["failed"]:
                            failures += 1
                        if self.config.telegram.send_new_media:
                            stats["attachments"] = attachments[:self.config.telegram.max_new_media_attachments]
                            stats["attachment_total"] = len(attachments)
                        if stats["downloaded"] or stats["failed"] or account_id in opened:
                            payload = {"label": account["label"], **stats}
                            self.db.enqueue_event(f"media:{run_id}:{account_id}", "media_summary", payload, account_id)
                        LOG.info("%s 媒體：新增 %d、重複 %d、失敗 %d、待下載 %d", account["label"],
                                 stats["downloaded"], stats["duplicate"], stats["failed"], stats["pending"])
                else:
                    LOG.warning("媒體記錄與下載已由 schedule.media_download_enabled=false 暫停")

            if source_access_succeeded and not self.db.source_cooldown(source):
                self.db.record_source_recovery(source)
            self._enqueue_heartbeat_if_due()
            self.db.enqueue_relationship_watchdogs(datetime.now(UTC))
            self._backup_if_due()
            sent, send_failed = await self.telegram.deliver_pending(self.db)
            LOG.info("Telegram：成功 %d、失敗 %d", sent, send_failed)
            status = ("partial" if failures or send_failed else
                      "cooldown" if self.db.source_cooldown(source) else "success")
            self.db.finish_run(run_id, status, f"account_failures={failures}, telegram_failures={send_failed}")
            return 1 if failures else 0
        except Exception as exc:
            self.db.finish_run(run_id, "failed", str(exc))
            raise

    @staticmethod
    def _json_value(value):
        return value.value if hasattr(value, "value") else value

    def _validate_source_identity(self, account: dict, target_url: str, result: ScrapeResult) -> None:
        if result.source == "legacy":
            return
        expected_id = account.get("instagram_profile_id") or self.db.get_meta(
            f"anonymous_profile_id:{account['id']}:{result.source}"
        )
        if expected_id and result.profile_id != str(expected_id):
            raise ScrapeFailure("來源帳號 ID 與既有身分不一致，未保存此次資料", "驗證帳號身分")
        same_id = bool(expected_id and result.profile_id == str(expected_id))
        if result.snapshot.username.casefold() != account_username(target_url) and not same_id:
            raise ScrapeFailure("來源使用者名稱與查詢目標不一致，未保存此次資料", "驗證帳號身分")
        if not result.profile_id:
            raise ScrapeFailure("來源缺少可驗證帳號 ID，未保存此次資料", "驗證帳號身分")

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
