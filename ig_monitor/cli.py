from __future__ import annotations

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .db import Database
from .dedup import deduplicate_existing_media
from .monitor import Monitor, check_accounts
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
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument("--dry-run", action="store_true", help="Preview media deduplication")
    apply_group.add_argument("--apply", action="store_true", help="Apply media deduplication")
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
    if (args.dry_run or args.apply) and not args.dedupe_media:
        raise ValueError("--dry-run/--apply can only be used with --dedupe-media")
    config = load_config(args.config, require_telegram=not (args.check or args.dedupe_media))
    setup_logging(config.paths.data_dir, write_file=not (args.check or args.dedupe_media))
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
