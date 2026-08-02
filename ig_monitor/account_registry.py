from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import yaml

from .config import MAX_ACCOUNTS, load_config, normalize_account_url
from .db import Database


AccountValidator = Callable[[str], None]


class AccountRegistry:
    """Persist dashboard account changes to config.yaml and refresh SQLite."""

    def __init__(self, config_path: Path, db_path: Path, validator: AccountValidator):
        self.config_path = config_path
        self.db_path = db_path
        self.validator = validator
        self._lock = threading.Lock()

    def add(self, url: str, label: str | None = None) -> None:
        normalized = normalize_account_url(url)
        with self._lock:
            config = load_config(self.config_path, require_telegram=False)
            if any(account.url == normalized for account in config.accounts):
                raise ValueError("此帳號已在監控清單中")
            if len(config.accounts) >= MAX_ACCOUNTS:
                raise ValueError(f"監控帳號已達 {MAX_ACCOUNTS} 個上限")
            self.validator(normalized)
            raw = self._read_raw()
            key = normalized.rstrip("/").rsplit("/", 1)[-1]
            clean_label = (label or "").strip()[:100] or key
            raw.setdefault("accounts", []).append({
                "url": normalized,
                "enabled": True,
                "label": clean_label,
            })
            self._write_raw(raw)
            self._sync_database()

    def remove(self, account_id: int) -> None:
        with self._lock:
            config = load_config(self.config_path, require_telegram=False)
            if len(config.accounts) <= 1:
                raise ValueError("至少需要保留一個監控帳號")
            db = Database(self.db_path)
            try:
                row = db.get_account_by_id(account_id)
            finally:
                db.close()
            if row is None:
                raise ValueError("找不到要移除的監控帳號")
            raw = self._read_raw()
            accounts = raw.get("accounts", [])
            raw["accounts"] = [
                item for item in accounts
                if normalize_account_url(item.get("url", "")) != row["url"]
            ]
            if len(raw["accounts"]) == len(accounts):
                raise ValueError("找不到要移除的監控帳號")
            self._write_raw(raw)
            self._sync_database()

    def reorder(self, account_ids: list[int]) -> None:
        with self._lock:
            db = Database(self.db_path)
            try:
                enabled_rows = db.enabled_accounts()
            finally:
                db.close()
            expected_ids = {row["id"] for row in enabled_rows}
            if len(account_ids) != len(expected_ids) or set(account_ids) != expected_ids:
                raise ValueError("排序內容與目前監控帳號不一致，請重新整理後再試")
            urls_by_id = {row["id"]: row["url"] for row in enabled_rows}
            raw = self._read_raw()
            accounts = raw.get("accounts", [])
            items_by_url = {
                normalize_account_url(item.get("url", "")): item
                for item in accounts
            }
            ordered_urls = [urls_by_id[account_id] for account_id in account_ids]
            disabled = [
                item for item in accounts
                if normalize_account_url(item.get("url", "")) not in set(ordered_urls)
            ]
            raw["accounts"] = [items_by_url[url] for url in ordered_urls] + disabled
            self._write_raw(raw)
            self._sync_database()

    def set_relationship_tracking(self, account_id: int, enabled: bool) -> None:
        with self._lock:
            db = Database(self.db_path)
            try:
                row = db.get_account_by_id(account_id)
            finally:
                db.close()
            if row is None:
                raise ValueError("找不到監控帳號")
            raw = self._read_raw()
            for item in raw.get("accounts", []):
                if normalize_account_url(item.get("url", "")) == row["url"]:
                    item["relationship_tracking"] = bool(enabled)
                    self._write_raw(raw)
                    self._sync_database()
                    return
            raise ValueError("找不到監控帳號")

    def _read_raw(self) -> dict:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("config.yaml 必須是 YAML 物件")
        return raw

    def _write_raw(self, raw: dict) -> None:
        with self.config_path.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(raw, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())

    def _sync_database(self) -> None:
        config = load_config(self.config_path, require_telegram=False)
        db = Database(self.db_path)
        try:
            db.sync_accounts(config.accounts)
        finally:
            db.close()
