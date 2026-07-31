from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import AccountConfig
from .models import MediaCandidate, ProfileSnapshot


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    def _init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
          id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, account_key TEXT NOT NULL,
          label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          effective_url TEXT, instagram_profile_id TEXT,
          snapshot_json TEXT, fail_count INTEGER NOT NULL DEFAULT 0,
          failure_notified INTEGER NOT NULL DEFAULT 0, failure_since TEXT,
          last_error TEXT, last_success_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
          account_id INTEGER REFERENCES accounts(id), kind TEXT NOT NULL,
          payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
          created_at TEXT NOT NULL, sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_pending ON events(status, created_at);
        CREATE TABLE IF NOT EXISTS media (
          id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
          media_key TEXT NOT NULL, logical_id TEXT, category TEXT NOT NULL, kind TEXT NOT NULL,
          url TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0, published_at TEXT,
          width INTEGER, height INTEGER, source_rank INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          local_path TEXT, sha256 TEXT, last_error TEXT,
          duplicate_of_id INTEGER, fingerprint_json TEXT, file_size INTEGER,
          video_duration REAL, video_bitrate INTEGER,
          discovered_at TEXT NOT NULL, downloaded_at TEXT,
          UNIQUE(account_id, media_key)
        );
        CREATE INDEX IF NOT EXISTS idx_media_pending ON media(account_id, status, discovered_at);
        CREATE INDEX IF NOT EXISTS idx_media_content ON media(account_id,kind,status,sha256);
        CREATE TABLE IF NOT EXISTS media_sources (
          media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
          category TEXT NOT NULL,
          PRIMARY KEY(media_id,category)
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS apify_usage (
          id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), cycle_key TEXT NOT NULL,
          reservation_usd REAL NOT NULL, actual_usd REAL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_apify_usage_cycle ON apify_usage(cycle_key);
        """)
        self._add_column_if_missing("accounts", "effective_url", "TEXT")
        self._add_column_if_missing("accounts", "instagram_profile_id", "TEXT")
        self._add_column_if_missing("media", "duplicate_of_id", "INTEGER")
        self._add_column_if_missing("media", "fingerprint_json", "TEXT")
        self._add_column_if_missing("media", "file_size", "INTEGER")
        self._add_column_if_missing("media", "video_duration", "REAL")
        self._add_column_if_missing("media", "video_bitrate", "INTEGER")
        self.conn.execute("UPDATE accounts SET effective_url=url WHERE effective_url IS NULL")
        self.conn.execute("INSERT OR IGNORE INTO media_sources(media_id,category) SELECT id,category FROM media")
        self.conn.commit()

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def sync_accounts(self, accounts: Iterable[AccountConfig]) -> None:
        now = utc_now()
        with self.transaction() as con:
            con.execute("UPDATE accounts SET enabled=0, updated_at=?", (now,))
            for account in accounts:
                con.execute("""
                  INSERT INTO accounts(url,account_key,label,enabled,effective_url,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(url) DO UPDATE SET account_key=excluded.account_key,
                    label=excluded.label,enabled=excluded.enabled,updated_at=excluded.updated_at
                """, (account.url, account.key, account.label, int(account.enabled), account.url, now, now))

    def enabled_accounts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM accounts WHERE enabled=1 ORDER BY id")]

    def get_account(self, url_or_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM accounts WHERE url=? OR account_key=?", (url_or_key, url_or_key)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def snapshot_from_row(row: dict[str, Any]) -> ProfileSnapshot | None:
        return ProfileSnapshot.from_dict(json.loads(row["snapshot_json"])) if row.get("snapshot_json") else None

    def record_success(
        self,
        account_id: int,
        snapshot: ProfileSnapshot,
        events: Iterable[tuple[str, str, dict[str, Any]]],
        media: Iterable[MediaCandidate],
    ) -> None:
        now = utc_now()
        with self.transaction() as con:
            for event_key, kind, payload in events:
                con.execute("""
                  INSERT OR IGNORE INTO events(event_key,account_id,kind,payload_json,created_at)
                  VALUES(?,?,?,?,?)
                """, (event_key, account_id, kind, json.dumps(payload, ensure_ascii=False), now))
            for item in media:
                con.execute("""
                  INSERT INTO media(account_id,media_key,logical_id,category,kind,url,position,published_at,
                    width,height,source_rank,discovered_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(account_id,media_key) DO UPDATE SET
                    url=CASE WHEN excluded.source_rank>=media.source_rank THEN excluded.url ELSE media.url END,
                    width=COALESCE(excluded.width,media.width),height=COALESCE(excluded.height,media.height),
                    source_rank=MAX(excluded.source_rank,media.source_rank)
                """, (account_id, item.media_key, item.logical_id, item.category, item.kind, item.url,
                      item.position, item.published_at, item.width, item.height, item.source_rank, now))
                media_row = con.execute(
                    "SELECT id FROM media WHERE account_id=? AND media_key=?", (account_id, item.media_key)
                ).fetchone()
                con.execute(
                    "INSERT OR IGNORE INTO media_sources(media_id,category) VALUES(?,?)",
                    (media_row["id"], item.category),
                )
            con.execute("""
              UPDATE accounts SET snapshot_json=?,fail_count=0,failure_notified=0,failure_since=NULL,
                last_error=NULL,last_success_at=?,updated_at=? WHERE id=?
            """, (json.dumps(snapshot.to_dict(), ensure_ascii=False), now, now, account_id))

    def set_identity(self, account_id: int, profile_id: str, username: str | None = None) -> None:
        now = utc_now()
        with self.transaction() as con:
            if username:
                effective_url = f"https://insta-stories-viewer.com/{username}/"
                con.execute("""UPDATE accounts SET instagram_profile_id=?,effective_url=?,updated_at=? WHERE id=?""",
                            (profile_id, effective_url, now, account_id))
            else:
                con.execute("UPDATE accounts SET instagram_profile_id=?,updated_at=? WHERE id=?",
                            (profile_id, now, account_id))

    def set_effective_url(self, account_id: int, username: str) -> None:
        now = utc_now()
        url = f"https://insta-stories-viewer.com/{username}/"
        self.conn.execute("UPDATE accounts SET effective_url=?,updated_at=? WHERE id=?", (url, now, account_id))
        self.conn.commit()

    def apify_reserved_total(self, cycle_key: str) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(reservation_usd),0) FROM apify_usage WHERE cycle_key=?", (cycle_key,)).fetchone()
        return float(row[0])

    def reserve_apify_usage(self, account_id: int, cycle_key: str, reservation_usd: float) -> None:
        self.conn.execute("""INSERT INTO apify_usage(account_id,cycle_key,reservation_usd,created_at)
                             VALUES(?,?,?,?)""", (account_id, cycle_key, reservation_usd, utc_now()))
        self.conn.commit()

    def budget_notice_sent(self, cycle_key: str) -> bool:
        return self.get_meta(f"apify_budget_notice:{cycle_key}") == "1"

    def mark_budget_notice_sent(self, cycle_key: str) -> None:
        self.set_meta(f"apify_budget_notice:{cycle_key}", "1")

    def record_failure(self, account_id: int, label: str, error: str, blocker: str | None) -> int:
        now = utc_now()
        with self.transaction() as con:
            row = con.execute("SELECT fail_count,failure_notified,failure_since FROM accounts WHERE id=?", (account_id,)).fetchone()
            count = int(row["fail_count"]) + 1
            since = row["failure_since"] or now
            con.execute("UPDATE accounts SET fail_count=?,failure_since=?,last_error=?,updated_at=? WHERE id=?",
                        (count, since, error, now, account_id))
            if count >= 3 and not row["failure_notified"]:
                payload = {"label": label, "error": error, "blocker": blocker, "fail_count": count, "since": since}
                con.execute("""
                  INSERT OR IGNORE INTO events(event_key,account_id,kind,payload_json,created_at)
                  VALUES(?,?,?,?,?)
                """, (f"failure:{account_id}:{since}", account_id, "failure",
                      json.dumps(payload, ensure_ascii=False), now))
                con.execute("UPDATE accounts SET failure_notified=1 WHERE id=?", (account_id,))
            return count

    def enqueue_event(self, event_key: str, kind: str, payload: dict[str, Any], account_id: int | None = None) -> None:
        with self.transaction() as con:
            con.execute("""INSERT OR IGNORE INTO events(event_key,account_id,kind,payload_json,created_at)
                           VALUES(?,?,?,?,?)""",
                        (event_key, account_id, kind, json.dumps(payload, ensure_ascii=False), utc_now()))

    def pending_events(self, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM events WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def update_event_payload(self, event_id: int, payload: dict[str, Any]) -> None:
        self.conn.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), event_id))
        self.conn.commit()

    def mark_event_sent(self, event_id: int) -> None:
        self.conn.execute("UPDATE events SET status='sent',sent_at=?,last_error=NULL WHERE id=?", (utc_now(), event_id))
        self.conn.commit()

    def mark_event_failed(self, event_id: int, error: str) -> None:
        self.conn.execute("UPDATE events SET attempts=attempts+1,last_error=? WHERE id=?", (error, event_id))
        self.conn.commit()

    def pending_media(self, account_id: int, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
          SELECT * FROM media WHERE account_id=? AND status IN ('pending','failed')
          ORDER BY CASE WHEN published_at IS NULL THEN 1 ELSE 0 END,published_at DESC,id DESC LIMIT ?
        """, (account_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_media_downloaded(
        self, media_id: int, local_path: str, sha256: str, fingerprint_json: str | None = None,
        width: int | None = None, height: int | None = None, file_size: int | None = None,
        video_duration: float | None = None, video_bitrate: int | None = None,
    ) -> None:
        self.conn.execute("""UPDATE media SET status='downloaded',local_path=?,sha256=?,fingerprint_json=?,
                             width=COALESCE(?,width),height=COALESCE(?,height),file_size=?,
                             video_duration=?,video_bitrate=?,duplicate_of_id=NULL,
                             downloaded_at=?,last_error=NULL WHERE id=?""",
                          (local_path, sha256, fingerprint_json, width, height, file_size,
                           video_duration, video_bitrate, utc_now(), media_id))
        self.conn.commit()

    def mark_media_failed(self, media_id: int, error: str) -> None:
        self.conn.execute("UPDATE media SET status='failed',attempts=attempts+1,last_error=? WHERE id=?", (error, media_id))
        self.conn.commit()

    def downloaded_by_hash(self, account_id: int, sha256: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM media WHERE account_id=? AND sha256=? AND status='downloaded'
               AND duplicate_of_id IS NULL ORDER BY id LIMIT 1""",
            (account_id, sha256),
        ).fetchone()
        return dict(row) if row else None

    def canonical_media(self, account_id: int, kind: str, exclude_id: int | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
          SELECT * FROM media WHERE account_id=? AND kind=? AND status='downloaded'
          AND duplicate_of_id IS NULL AND id!=COALESCE(?, -1) ORDER BY id
        """, (account_id, kind, exclude_id)).fetchall()
        return [dict(row) for row in rows]

    def downloaded_media(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
          SELECT * FROM media WHERE status='downloaded' AND local_path IS NOT NULL ORDER BY account_id,id
        """).fetchall()
        return [dict(row) for row in rows]

    def store_media_fingerprint(
        self, media_id: int, sha256: str, fingerprint_json: str, width: int, height: int,
        file_size: int, video_duration: float | None, video_bitrate: int | None,
    ) -> None:
        self.conn.execute("""UPDATE media SET sha256=?,fingerprint_json=?,width=?,height=?,file_size=?,
                             video_duration=?,video_bitrate=? WHERE id=?""",
                          (sha256, fingerprint_json, width, height, file_size,
                           video_duration, video_bitrate, media_id))
        self.conn.commit()

    def mark_media_duplicate(self, media_id: int, canonical_id: int, sha256: str,
                             fingerprint_json: str | None = None) -> list[str]:
        with self.transaction() as con:
            paths = [row[0] for row in con.execute(
                "SELECT DISTINCT local_path FROM media WHERE id=? AND local_path IS NOT NULL", (media_id,)
            )]
            con.execute("""INSERT OR IGNORE INTO media_sources(media_id,category)
                           SELECT ?,category FROM media_sources WHERE media_id=?""", (canonical_id, media_id))
            con.execute("DELETE FROM media_sources WHERE media_id=?", (media_id,))
            con.execute("""UPDATE media SET status='duplicate',duplicate_of_id=?,sha256=?,
                           fingerprint_json=COALESCE(?,fingerprint_json),local_path=NULL,last_error=NULL
                           WHERE id=?""", (canonical_id, sha256, fingerprint_json, media_id))
        return paths

    def promote_canonical(self, new_id: int, old_id: int) -> list[str]:
        with self.transaction() as con:
            paths = [row[0] for row in con.execute("""
                SELECT DISTINCT local_path FROM media
                WHERE (id=? OR duplicate_of_id=?) AND local_path IS NOT NULL
            """, (old_id, old_id))]
            con.execute("""INSERT OR IGNORE INTO media_sources(media_id,category)
                           SELECT ?,category FROM media_sources WHERE media_id=?""", (new_id, old_id))
            con.execute("DELETE FROM media_sources WHERE media_id=?", (old_id,))
            con.execute("UPDATE media SET duplicate_of_id=? WHERE duplicate_of_id=?", (new_id, old_id))
            con.execute("""UPDATE media SET status='duplicate',duplicate_of_id=?,local_path=NULL,last_error=NULL
                           WHERE id=?""", (new_id, old_id))
        return paths

    def media_path_referenced(self, local_path: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM media WHERE local_path=? AND status='downloaded' LIMIT 1", (local_path,)
        ).fetchone()
        return row is not None

    def media_counts(self, account_id: int) -> dict[str, int]:
        rows = self.conn.execute("SELECT status,COUNT(*) n FROM media WHERE account_id=? GROUP BY status", (account_id,))
        return {row["status"]: row["n"] for row in rows}

    def summary(self) -> dict[str, int]:
        result = {"accounts": 0, "normal": 0, "private": 0, "public": 0, "error": 0, "pending": 0}
        rows = self.conn.execute("SELECT snapshot_json,fail_count FROM accounts WHERE enabled=1").fetchall()
        result["accounts"] = len(rows)
        for row in rows:
            if row["fail_count"] >= 3:
                result["error"] += 1
            else:
                result["normal"] += 1
            if row["snapshot_json"]:
                privacy = json.loads(row["snapshot_json"]).get("privacy")
                if privacy in ("private", "public"):
                    result[privacy] += 1
        result["pending"] = self.conn.execute(
            "SELECT COUNT(*) FROM media WHERE status IN ('pending','failed')"
        ).fetchone()[0]
        return result

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))
        self.conn.commit()

    def reset_account(self, value: str) -> bool:
        row = self.get_account(value)
        if not row:
            return False
        with self.transaction() as con:
            con.execute("UPDATE accounts SET snapshot_json=NULL,fail_count=0,failure_notified=0,failure_since=NULL,last_error=NULL WHERE id=?",
                        (row["id"],))
            con.execute("DELETE FROM events WHERE account_id=?", (row["id"],))
        return True

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at,status) VALUES(?,'running')", (utc_now(),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        self.conn.execute("UPDATE runs SET finished_at=?,status=?,detail=? WHERE id=?", (utc_now(), status, detail, run_id))
        self.conn.commit()

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()
