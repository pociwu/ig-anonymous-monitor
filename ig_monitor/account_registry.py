from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import yaml

from .config import load_config, normalize_account_url
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
            if len(config.accounts) >= 10:
                raise ValueError("監控帳號已達 10 個上限")
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
