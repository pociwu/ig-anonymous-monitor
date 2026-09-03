from __future__ import annotations

import argparse
import asyncio
import logging
import json
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .db import Database
from .dedup import deduplicate_existing_media, quarantine_cross_account_media
from .monitor import Monitor, check_accounts
from .instagram_source import InstagrapiRelationshipSource
from .relationships import CollectorAdministration
from .telegram import TelegramSender
from .utils import process_lock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IG anonymous profile monitor")
    parser.add_argument("--config", default="config.yaml", help="config.yaml 路徑")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只載入與解析，不寫入、不通知、不下載")
    group.add_argument("--send-test", action="store_true", help="傳送 Telegram 測試訊息")
    group.add_argument("--reset-account", metavar="URL_OR_USERNAME", help="清除單一帳號監控基準")
    group.add_argument("--dedupe-media", action="store_true", help="Analyze and deduplicate downloaded media")
    group.add_argument(
        "--quarantine-cross-account-media", action="store_true",
        help="Find exact or perceptually matching media shared by multiple monitored accounts",
    )
    group.add_argument("--collector-status", action="store_true", help="Show non-secret collector state")
    group.add_argument("--collector-login", action="store_true", help="Login and begin the 72-hour observation")
    group.add_argument("--collector-approve", metavar="ACCOUNT", help="Approve one account as the seven-day canary")
    group.add_argument("--collector-recovery", action="store_true", help="Begin a new observation after risk_hold")
    parser.add_argument("--collector-session", default="collector-secrets/session.json")
    parser.add_argument("--media-since", help="Only quarantine media downloaded at or after this ISO-8601 time")
    parser.add_argument(
        "--media-kind", choices=("video", "image", "all"), default="video",
        help="Cross-account quarantine media kind (default: video)",
    )
    parser.add_argument("--min-accounts", type=int, default=3,
                        help="Minimum distinct accounts sharing matching media (default: 3)")
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument("--dry-run", action="store_true", help="Preview media maintenance changes")
    apply_group.add_argument("--apply", action="store_true", help="Apply media maintenance changes")
    return parser


def setup_logging(data_dir: Path, verbose: bool = False, write_file: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    handlers = [stream]
    if write_file:
        data_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(data_dir / "monitor.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    root.handlers[:] = handlers


async def _async_main(args: argparse.Namespace) -> int:
    media_utility = args.dedupe_media or args.quarantine_cross_account_media
    if (args.dry_run or args.apply) and not media_utility:
        raise ValueError("--dry-run/--apply can only be used with a media maintenance command")
    if (args.media_since or args.min_accounts != 3 or args.media_kind != "video") \
            and not args.quarantine_cross_account_media:
        raise ValueError(
            "--media-since/--min-accounts/--media-kind require --quarantine-cross-account-media"
        )
    collector_command = any((args.collector_status, args.collector_login, args.collector_approve, args.collector_recovery))
    utility_command = any((
        args.check, args.send_test, args.reset_account, media_utility, collector_command,
    ))
    config = load_config(
        args.config,
        require_telegram=not (args.check or media_utility or collector_command),
        require_apify=not utility_command,
    )
    setup_logging(config.paths.data_dir, write_file=not (args.check or media_utility))
    if args.check:
        return await check_accounts(config)
    if args.send_test:
        if not config.telegram.bot_token or not config.telegram.chat_id:
            raise ValueError("--send-test 需要 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID")
        await TelegramSender(config.telegram).send_test()
        logging.info("Telegram 測試訊息已送出")
        return 0

    db = Database(config.paths.data_dir / "state.sqlite3")
    try:
        db.sync_accounts(config.accounts)
        if collector_command:
            source = InstagrapiRelationshipSource(Path(args.collector_session).expanduser().resolve())
            admin = CollectorAdministration(db, source, config.instagram_enrichment)
            now = datetime.now(UTC)
            if args.collector_login:
                status = admin.login(now)
            elif args.collector_approve:
                account = db.get_account(args.collector_approve)
                if account is None and str(args.collector_approve).isdigit():
                    account = db.get_account_by_id(int(args.collector_approve))
                if account is None:
                    raise ValueError(f"collector canary account not found: {args.collector_approve}")
                status = admin.approve(account["id"], now)
            elif args.collector_recovery:
                status = admin.begin_recovery(now)
            else:
                status = admin.status(now)
            print(json.dumps({
                "state": status.state,
                "observed_since": status.observed_since,
                "canary_account_id": status.canary_account_id,
                "risk_reason": status.risk_reason,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.dedupe_media:
            if not args.dry_run and not args.apply:
                raise ValueError("--dedupe-media requires --dry-run or --apply")
            report = deduplicate_existing_media(db, config.dedup, apply=args.apply)
            mode = "APPLY" if args.apply else "DRY-RUN"
            logging.info(
                "%s media dedup: scanned=%d analyzed=%d groups=%d duplicates=%d upgrades=%d files=%d errors=%d",
                mode, report["scanned"], report["analyzed"], report["duplicate_groups"],
                report["duplicate_rows"], report["quality_upgrades"], report["removable_files"],
                len(report["errors"]),
            )
            if report["ffmpeg_missing"]:
                logging.warning("ffmpeg/ffprobe unavailable; video deduplication used SHA-256 only")
            for error in report["errors"]:
                logging.warning("%s", error)
            return 1 if report["errors"] else 0
        if args.quarantine_cross_account_media:
            if not args.dry_run and not args.apply:
                raise ValueError("--quarantine-cross-account-media requires --dry-run or --apply")
            if args.min_accounts < 2:
                raise ValueError("--min-accounts must be at least 2")
            if args.media_since:
                try:
                    datetime.fromisoformat(args.media_since)
                except ValueError as exc:
                    raise ValueError("--media-since must be an ISO-8601 date/time") from exc
            report = quarantine_cross_account_media(
                db, apply=args.apply, min_accounts=args.min_accounts, since=args.media_since,
                kind=args.media_kind, dedup=config.dedup,
            )
            mode = "APPLY" if args.apply else "DRY-RUN"
            logging.info(
                "%s cross-account quarantine: kind=%s scanned=%d groups=%d rows=%d files-preserved=%d",
                mode, report["kind"], report["scanned"], report["groups"],
                report["media_rows"], report["files_preserved"],
            )
            for sample in report["samples"]:
                logging.info(
                    "shared %s=%s kinds=%s accounts=%s rows=%d media-ids=%s",
                    sample["signal"], sample["value"][:80],
                    ",".join(sample["kinds"]), ",".join(sample["accounts"]),
                    sample["rows"], ",".join(str(value) for value in sample["media_ids"]),
                )
            if report["fingerprint_missing"]:
                logging.warning(
                    "%d downloaded videos had no reusable ffmpeg fingerprint; exact SHA/URL checks still ran",
                    report["fingerprint_missing"],
                )
            for error in report["errors"]:
                logging.warning("%s", error)
            return 1 if report["errors"] else 0
        if args.reset_account:
            if not db.reset_account(args.reset_account):
                logging.error("找不到帳號：%s", args.reset_account)
                return 2
            logging.info("已重設帳號：%s", args.reset_account)
            return 0
        lock_path = config.paths.data_dir / "monitor.lock"
        with process_lock(lock_path) as acquired:
            if not acquired:
                logging.info("上一輪仍在執行，本輪略過")
                return 0
            return await Monitor(config, db).run()
    finally:
        db.close()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_async_main(args)))
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
