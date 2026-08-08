from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
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
          label TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS profile_history (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          observed_at TEXT NOT NULL,
          posts INTEGER NOT NULL,
          followers INTEGER NOT NULL,
          following INTEGER NOT NULL,
          UNIQUE(account_id,observed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_profile_history_account_time
          ON profile_history(account_id,observed_at DESC,id DESC);
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
        CREATE TABLE IF NOT EXISTS collector_state (
          id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL DEFAULT 'unconfigured',
          observed_since TEXT, approved_at TEXT, canary_account_id INTEGER REFERENCES accounts(id),
          canary_started_at TEXT, last_health_check_at TEXT, last_job_started_at TEXT,
          risk_reason TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relationship_jobs (
          id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
          need_followers INTEGER NOT NULL DEFAULT 0, need_following INTEGER NOT NULL DEFAULT 0,
          reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', available_at TEXT NOT NULL,
          lease_until TEXT, started_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_relationship_jobs_ready
          ON relationship_jobs(status,available_at,id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_jobs_one_open
          ON relationship_jobs(account_id) WHERE status IN ('pending','leased');
        CREATE TABLE IF NOT EXISTS relationship_runs (
          id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES relationship_jobs(id),
          account_id INTEGER NOT NULL REFERENCES accounts(id), direction TEXT NOT NULL,
          status TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0, expected_count INTEGER,
          collected_count INTEGER NOT NULL DEFAULT 0, cursor TEXT, error TEXT,
          started_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS relationship_members (
          instagram_profile_id TEXT PRIMARY KEY, username TEXT NOT NULL,
          display_name TEXT, avatar_url TEXT, avatar_sha256 TEXT, avatar_path TEXT,
          posts INTEGER, followers INTEGER, following INTEGER,
          bio TEXT, privacy TEXT, profile_observed_at TEXT, username_observed_at TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relationship_run_members (
          run_id INTEGER NOT NULL REFERENCES relationship_runs(id) ON DELETE CASCADE,
          instagram_profile_id TEXT NOT NULL REFERENCES relationship_members(instagram_profile_id),
          username TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(run_id,instagram_profile_id)
        );
        CREATE TABLE IF NOT EXISTS account_relationships (
          account_id INTEGER NOT NULL REFERENCES accounts(id), direction TEXT NOT NULL,
          instagram_profile_id TEXT NOT NULL REFERENCES relationship_members(instagram_profile_id),
          username TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1, removed_at TEXT,
          PRIMARY KEY(account_id,direction,instagram_profile_id)
        );
        CREATE INDEX IF NOT EXISTS idx_account_relationships_active
          ON account_relationships(account_id,direction,active,username);
        CREATE TABLE IF NOT EXISTS relationship_history (
          id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
          direction TEXT NOT NULL, change_kind TEXT NOT NULL,
          instagram_profile_id TEXT REFERENCES relationship_members(instagram_profile_id),
          username TEXT, interval_change INTEGER NOT NULL DEFAULT 0,
          observed_at TEXT NOT NULL, run_id INTEGER REFERENCES relationship_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_relationship_history_account
          ON relationship_history(account_id,observed_at DESC,id DESC);
        CREATE TABLE IF NOT EXISTS member_enrichment_jobs (
          id INTEGER PRIMARY KEY, instagram_profile_id TEXT NOT NULL REFERENCES relationship_members(instagram_profile_id),
          reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', available_at TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, lease_until TEXT, last_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_member_enrichment_jobs_ready
          ON member_enrichment_jobs(status,available_at,id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_member_enrichment_jobs_one_open
          ON member_enrichment_jobs(instagram_profile_id) WHERE status IN ('pending','leased');
        CREATE TABLE IF NOT EXISTS member_enrichment_attempts (
          id INTEGER PRIMARY KEY, instagram_profile_id TEXT NOT NULL,
          status TEXT NOT NULL, error TEXT, attempted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS authenticated_work_runs (
          id INTEGER PRIMARY KEY, work_kind TEXT NOT NULL, work_ref_id INTEGER NOT NULL,
          budget_day TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
          lease_until TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
          outcome TEXT, error TEXT,
          UNIQUE(work_kind,work_ref_id,started_at)
        );
        CREATE INDEX IF NOT EXISTS idx_authenticated_work_budget
          ON authenticated_work_runs(budget_day,started_at);
        CREATE INDEX IF NOT EXISTS idx_authenticated_work_active
          ON authenticated_work_runs(status,lease_until);
        CREATE TABLE IF NOT EXISTS post_feature_state (
          id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL DEFAULT 'disabled',
          phase_one_stable_since TEXT, canary_account_id INTEGER REFERENCES accounts(id),
          canary_started_at TEXT, canary_completed_at TEXT, approved_at TEXT,
          suspended_at TEXT, suspension_reason TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS post_jobs (
          id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
          reason TEXT NOT NULL, mode TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
          status TEXT NOT NULL DEFAULT 'pending', available_at TEXT NOT NULL,
          cursor TEXT, cursor_reset_at TEXT, baseline_target INTEGER,
          lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_post_jobs_ready
          ON post_jobs(status,priority,available_at,id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_post_jobs_one_open
          ON post_jobs(account_id) WHERE status IN ('pending','leased','paused');
        CREATE TABLE IF NOT EXISTS post_runs (
          id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES post_jobs(id),
          account_id INTEGER NOT NULL REFERENCES accounts(id),
          authenticated_work_run_id INTEGER REFERENCES authenticated_work_runs(id),
          reason TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
          requested_count INTEGER, observed_count INTEGER NOT NULL DEFAULT 0,
          cursor_in TEXT, cursor_out TEXT, complete INTEGER NOT NULL DEFAULT 0,
          error TEXT, started_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS posts (
          id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
          owner_profile_id TEXT NOT NULL, media_pk TEXT NOT NULL, shortcode TEXT,
          original_url TEXT, taken_at TEXT, caption TEXT, media_type TEXT NOT NULL,
          product_type TEXT, pinned INTEGER NOT NULL DEFAULT 0,
          like_count INTEGER, comment_count INTEGER, source_flags TEXT NOT NULL DEFAULT '',
          availability TEXT NOT NULL DEFAULT 'current', first_observed_at TEXT NOT NULL,
          last_observed_at TEXT NOT NULL, last_complete_scan_at TEXT,
          unavailable_since TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(owner_profile_id,media_pk)
        );
        CREATE INDEX IF NOT EXISTS idx_posts_account_time
          ON posts(account_id,taken_at DESC,id DESC);
        CREATE TABLE IF NOT EXISTS post_items (
          id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
          item_pk TEXT, position INTEGER NOT NULL, media_type TEXT NOT NULL,
          width INTEGER, height INTEGER, duration REAL, candidate_url TEXT,
          candidate_expires_at TEXT, canonical_media_id INTEGER REFERENCES media(id),
          download_status TEXT NOT NULL DEFAULT 'pending', last_error TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(post_id,position)
        );
        CREATE TABLE IF NOT EXISTS post_change_history (
          id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
          run_id INTEGER REFERENCES post_runs(id), change_kind TEXT NOT NULL,
          before_json TEXT, after_json TEXT, observed_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_post_change_history_post
          ON post_change_history(post_id,observed_at DESC,id DESC);
        """)
        self._add_column_if_missing("accounts", "effective_url", "TEXT")
        self._add_column_if_missing("accounts", "instagram_profile_id", "TEXT")
        self._add_column_if_missing("accounts", "sort_order", "INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing("accounts", "relationship_tracking", "INTEGER NOT NULL DEFAULT 1")
        self._add_column_if_missing("accounts", "relationship_status", "TEXT NOT NULL DEFAULT 'unavailable'")
        self._add_column_if_missing("accounts", "relationship_frozen_at", "TEXT")
        self._add_column_if_missing("accounts", "followers_baseline_at", "TEXT")
        self._add_column_if_missing("accounts", "following_baseline_at", "TEXT")
        self._add_column_if_missing("accounts", "relationship_reconciled_at", "TEXT")
        self._add_column_if_missing("accounts", "identity_verified_source", "TEXT")
        self._add_column_if_missing("accounts", "identity_verified_at", "TEXT")
        self._add_column_if_missing("accounts", "identity_conflict_json", "TEXT")
        self._add_column_if_missing("accounts", "post_tracking", "INTEGER NOT NULL DEFAULT 1")
        self._add_column_if_missing(
            "accounts", "full_post_backfill_on_reopen", "INTEGER NOT NULL DEFAULT 0"
        )
        self._add_column_if_missing("accounts", "post_baseline_target", "INTEGER")
        self._add_column_if_missing(
            "accounts", "post_status", "TEXT NOT NULL DEFAULT 'unavailable'"
        )
        self._add_column_if_missing("accounts", "post_last_run_at", "TEXT")
        self._add_column_if_missing("accounts", "post_reconciled_at", "TEXT")
        self._add_column_if_missing("accounts", "post_pause_reason", "TEXT")
        self._add_column_if_missing("relationship_jobs", "started_at", "TEXT")
        self._add_column_if_missing("relationship_members", "avatar_sha256", "TEXT")
        self._add_column_if_missing("relationship_members", "avatar_path", "TEXT")
        self._add_column_if_missing("media", "duplicate_of_id", "INTEGER")
        self._add_column_if_missing("media", "fingerprint_json", "TEXT")
        self._add_column_if_missing("media", "file_size", "INTEGER")
        self._add_column_if_missing("media", "video_duration", "REAL")
        self._add_column_if_missing("media", "video_bitrate", "INTEGER")
        self.conn.execute("UPDATE accounts SET effective_url=url WHERE effective_url IS NULL")
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_one_full_post_backfill
               ON accounts(full_post_backfill_on_reopen)
               WHERE enabled=1 AND full_post_backfill_on_reopen=1"""
        )
        now = utc_now()
        self.conn.execute(
            """INSERT OR IGNORE INTO post_feature_state(id,state,updated_at)
               VALUES(1,'disabled',?)""",
            (now,),
        )
        self.conn.execute("INSERT OR IGNORE INTO media_sources(media_id,category) SELECT id,category FROM media")
        self._backfill_member_avatar_jobs()
        self._backfill_authenticated_work_runs()
        self._backfill_profile_history()
        self.conn.commit()

    def _backfill_member_avatar_jobs(self) -> None:
        now = utc_now()
        self.conn.execute(
            """INSERT OR IGNORE INTO member_enrichment_jobs(
                 instagram_profile_id,reason,status,available_at,created_at,updated_at
               )
               SELECT DISTINCT rm.instagram_profile_id,'avatar_cache_backfill','pending',?,?,?
               FROM relationship_members rm
               JOIN account_relationships ar
                 ON ar.instagram_profile_id=rm.instagram_profile_id AND ar.active=1
               WHERE rm.avatar_path IS NULL""",
            (now, now, now),
        )

    def _backfill_authenticated_work_runs(self) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO authenticated_work_runs(
                 work_kind,work_ref_id,budget_day,status,lease_until,started_at,
                 finished_at,outcome,error
               )
               SELECT 'relationship',id,date(datetime(started_at),'+8 hours'),
                 CASE WHEN status='leased' THEN 'abandoned' ELSE 'finished' END,
                 COALESCE(lease_until,started_at),started_at,
                 CASE WHEN status='leased' THEN started_at ELSE updated_at END,
                 status,last_error
               FROM relationship_jobs WHERE started_at IS NOT NULL"""
        )

    def _backfill_profile_history(self) -> None:
        fields = ("posts", "followers", "following")

        def insert(account_id: int, observed_at: str | None, state: dict[str, int]) -> None:
            if not observed_at:
                return
            self.conn.execute(
                """INSERT OR IGNORE INTO profile_history(
                     account_id,observed_at,posts,followers,following
                   ) VALUES(?,?,?,?,?)""",
                (account_id, observed_at, *(state[field] for field in fields)),
            )

        for row in self.conn.execute(
            """SELECT id,snapshot_json,last_success_at,created_at,updated_at
               FROM accounts WHERE snapshot_json IS NOT NULL"""
        ).fetchall():
            try:
                snapshot = json.loads(row["snapshot_json"])
                observed_at = snapshot.get("observed_at") or row["last_success_at"] or row["updated_at"]
                state = {field: int(snapshot.get(field, 0)) for field in fields}
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            insert(row["id"], observed_at, state)
            found_initial = False
            count_change_found = False
            events = self.conn.execute(
                """SELECT kind,payload_json,created_at FROM events
                   WHERE account_id=? AND kind IN ('initial','change')
                   ORDER BY created_at DESC,id DESC""",
                (row["id"],),
            ).fetchall()
            for event in events:
                try:
                    payload = json.loads(event["payload_json"])
                    if event["kind"] == "initial":
                        initial = payload.get("snapshot", {})
                        initial_state = {field: int(initial.get(field, state[field])) for field in fields}
                        insert(row["id"], initial.get("observed_at") or event["created_at"], initial_state)
                        found_initial = True
                        continue
                    changes = payload.get("changes", {})
                    changed_fields = [field for field in fields if field in changes]
                    if not changed_fields:
                        continue
                    insert(row["id"], event["created_at"], state)
                    count_change_found = True
                    for field in changed_fields:
                        state[field] = int(changes[field][0])
                except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            if count_change_found and not found_initial:
                insert(row["id"], row["created_at"], state)

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def sync_accounts(self, accounts: Iterable[AccountConfig]) -> None:
        now = utc_now()
        with self.transaction() as con:
            con.execute("UPDATE accounts SET enabled=0, updated_at=?", (now,))
            for sort_order, account in enumerate(accounts):
                con.execute("""
                  INSERT INTO accounts(url,account_key,label,enabled,sort_order,effective_url,
                    relationship_tracking,post_tracking,full_post_backfill_on_reopen,
                    created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(url) DO UPDATE SET account_key=excluded.account_key,
                    label=excluded.label,enabled=excluded.enabled,sort_order=excluded.sort_order,
                    relationship_tracking=excluded.relationship_tracking,
                    post_tracking=excluded.post_tracking,
                    full_post_backfill_on_reopen=excluded.full_post_backfill_on_reopen,
                    updated_at=excluded.updated_at
                """, (account.url, account.key, account.label, int(account.enabled), sort_order,
                      account.url, int(account.relationship_tracking), int(account.post_tracking),
                      int(account.full_post_backfill_on_reopen), now, now))
            con.execute(
                """UPDATE relationship_jobs SET status='cancelled',lease_until=NULL,updated_at=?
                   WHERE status IN ('pending','leased') AND account_id IN (
                     SELECT id FROM accounts WHERE enabled=0 OR relationship_tracking=0
                   )""", (now,)
            )
            con.execute(
                """UPDATE post_jobs SET status='cancelled',lease_until=NULL,updated_at=?
                   WHERE status IN ('pending','leased','paused') AND account_id IN (
                     SELECT id FROM accounts WHERE enabled=0 OR post_tracking=0
                   )""", (now,)
            )

    def enabled_accounts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM accounts WHERE enabled=1 ORDER BY sort_order,id"
        )]

    def get_account(self, url_or_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM accounts WHERE url=? OR account_key=?", (url_or_key, url_or_key)).fetchone()
        return dict(row) if row else None

    def get_account_by_id(self, account_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
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
        observed_at = snapshot.observed_at or now
        with self.transaction() as con:
            con.execute(
                """INSERT OR IGNORE INTO profile_history(
                     account_id,observed_at,posts,followers,following
                   ) VALUES(?,?,?,?,?)""",
                (account_id, observed_at, snapshot.posts, snapshot.followers, snapshot.following),
            )
            con.execute(
                "DELETE FROM profile_history WHERE account_id=? AND observed_at<?",
                (account_id, (datetime.now(UTC) - timedelta(days=365)).isoformat(timespec="seconds")),
            )
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

    def profile_history(self, account_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            rows = self.conn.execute(
                """SELECT * FROM profile_history WHERE account_id=?
                   ORDER BY observed_at,id""", (account_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM (
                     SELECT * FROM profile_history WHERE account_id=?
                     ORDER BY observed_at DESC,id DESC LIMIT ?
                   ) ORDER BY observed_at,id""", (account_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

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

    def set_relationship_status(self, account_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE accounts SET relationship_status=?,updated_at=? WHERE id=?",
            (status, utc_now(), account_id),
        )
        self.conn.commit()

    def freeze_relationships(self, account_id: int, observed_at: str) -> None:
        with self.transaction() as con:
            con.execute(
                """UPDATE accounts SET relationship_status='frozen',relationship_frozen_at=?,updated_at=?
                   WHERE id=?""",
                (observed_at, utc_now(), account_id),
            )
            con.execute(
                """UPDATE relationship_jobs SET status='cancelled',lease_until=NULL,updated_at=?
                   WHERE account_id=? AND status IN ('pending','leased')""",
                (utc_now(), account_id),
            )

    def enqueue_relationship_job(
        self,
        account_id: int,
        need_followers: bool,
        need_following: bool,
        reason: str,
        available_at: str,
    ) -> int:
        now = utc_now()
        with self.transaction() as con:
            row = con.execute(
                """SELECT id,need_followers,need_following FROM relationship_jobs
                   WHERE account_id=? AND status IN ('pending','leased') ORDER BY id LIMIT 1""",
                (account_id,),
            ).fetchone()
            if row:
                con.execute(
                    """UPDATE relationship_jobs SET need_followers=?,need_following=?,reason=?,updated_at=?
                       WHERE id=?""",
                    (
                        int(bool(row["need_followers"]) or need_followers),
                        int(bool(row["need_following"]) or need_following),
                        reason,
                        now,
                        row["id"],
                    ),
                )
                return int(row["id"])
            cursor = con.execute(
                """INSERT INTO relationship_jobs(
                     account_id,need_followers,need_following,reason,status,available_at,created_at,updated_at
                   ) VALUES(?,?,?,?, 'pending',?,?,?)""",
                (account_id, int(need_followers), int(need_following), reason, available_at, now, now),
            )
            con.execute(
                """UPDATE accounts SET relationship_status=CASE
                     WHEN relationship_status='scope_exceeded' THEN relationship_status ELSE 'queued' END,
                     updated_at=? WHERE id=?""",
                (now, account_id),
            )
            return int(cursor.lastrowid)

    def relationship_jobs(self, account_id: int | None = None) -> list[dict[str, Any]]:
        if account_id is None:
            rows = self.conn.execute("SELECT * FROM relationship_jobs ORDER BY id")
        else:
            rows = self.conn.execute(
                "SELECT * FROM relationship_jobs WHERE account_id=? ORDER BY id", (account_id,)
            )
        return [dict(row) for row in rows]

    def get_relationship_job(self, job_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM relationship_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def has_open_relationship_job(self, account_id: int) -> bool:
        return self.conn.execute(
            """SELECT 1 FROM relationship_jobs WHERE account_id=?
               AND status IN ('pending','leased') LIMIT 1""", (account_id,)
        ).fetchone() is not None

    def enqueue_due_reconciliations(self, now: datetime, days: int, random_uniform) -> int:
        queued = 0
        threshold = now - timedelta(days=days)
        rows = self.conn.execute(
            """SELECT * FROM accounts WHERE enabled=1 AND relationship_tracking=1
               ORDER BY sort_order,id"""
        ).fetchall()
        for row in rows:
            if self.has_open_relationship_job(row["id"]):
                continue
            if not row["snapshot_json"] or json.loads(row["snapshot_json"]).get("privacy") != "public":
                continue
            reconciled = row["relationship_reconciled_at"]
            if reconciled and datetime.fromisoformat(reconciled) > threshold:
                continue
            available = now + timedelta(seconds=random_uniform(0, 86_400))
            self.enqueue_relationship_job(
                row["id"], True, True, "reconciliation", available.isoformat(timespec="seconds")
            )
            queued += 1
        return queued

    def prune_relationship_data(self, now: datetime) -> None:
        history_cutoff = (now - timedelta(days=365)).isoformat(timespec="seconds")
        run_cutoff = (now - timedelta(days=90)).isoformat(timespec="seconds")
        with self.transaction() as con:
            con.execute("DELETE FROM relationship_history WHERE observed_at<?", (history_cutoff,))
            con.execute(
                """UPDATE relationship_history SET run_id=NULL WHERE run_id IN (
                     SELECT id FROM relationship_runs WHERE started_at<?
                   )""", (run_cutoff,)
            )
            con.execute("DELETE FROM relationship_runs WHERE started_at<?", (run_cutoff,))
            con.execute("DELETE FROM member_enrichment_attempts WHERE attempted_at<?", (run_cutoff,))
            con.execute(
                "DELETE FROM member_enrichment_jobs WHERE status IN ('completed','cancelled') AND updated_at<?",
                (history_cutoff,),
            )
            con.execute(
                "DELETE FROM account_relationships WHERE active=0 AND removed_at<?", (history_cutoff,)
            )
            con.execute(
                """DELETE FROM relationship_members WHERE updated_at<?
                   AND NOT EXISTS(SELECT 1 FROM account_relationships ar
                                  WHERE ar.instagram_profile_id=relationship_members.instagram_profile_id
                                    AND ar.active=1)
                   AND NOT EXISTS(SELECT 1 FROM member_enrichment_jobs ej
                                  WHERE ej.instagram_profile_id=relationship_members.instagram_profile_id
                                    AND ej.status IN ('pending','leased'))""", (history_cutoff,)
            )

    def collector_state(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM collector_state WHERE id=1").fetchone()
        if row is None:
            now = utc_now()
            self.conn.execute(
                "INSERT INTO collector_state(id,state,updated_at) VALUES(1,'unconfigured',?)", (now,)
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM collector_state WHERE id=1").fetchone()
        return dict(row)

    def enqueue_relationship_watchdogs(self, now: datetime) -> None:
        relationship_cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        member_cutoff = (now - timedelta(hours=72)).isoformat(timespec="seconds")
        created_at = now.isoformat(timespec="seconds")
        with self.transaction() as con:
            for row in con.execute(
                """SELECT j.id,j.account_id,a.label FROM relationship_jobs j
                   JOIN accounts a ON a.id=j.account_id
                   WHERE j.status IN ('pending','leased') AND j.created_at<=?""",
                (relationship_cutoff,),
            ).fetchall():
                con.execute(
                    """INSERT OR IGNORE INTO events(event_key,account_id,kind,payload_json,created_at)
                       VALUES(?,?,'queue_stuck',?,?)""",
                    (
                        f"relationship-queue-stuck:{row['id']}", row["account_id"],
                        json.dumps({"queue": "relationship", "label": row["label"], "job_id": row["id"]}),
                        created_at,
                    ),
                )
            for row in con.execute(
                """SELECT id FROM member_enrichment_jobs
                   WHERE status IN ('pending','leased') AND created_at<=?""",
                (member_cutoff,),
            ).fetchall():
                con.execute(
                    """INSERT OR IGNORE INTO events(event_key,kind,payload_json,created_at)
                       VALUES(?,'queue_stuck',?,?)""",
                    (
                        f"member-queue-stuck:{row['id']}",
                        json.dumps({"queue": "member_enrichment", "job_id": row["id"]}), created_at,
                    ),
                )

    def set_collector_state(self, state: str, changed_at: str) -> None:
        previous = self.collector_state()
        with self.transaction() as con:
            con.execute(
                """UPDATE collector_state SET state=?,risk_reason=NULL,updated_at=? WHERE id=1""",
                (state, changed_at),
            )
            if previous["state"] != state:
                con.execute(
                    """INSERT OR IGNORE INTO events(event_key,kind,payload_json,created_at)
                       VALUES(?,?,?,?)""",
                    (
                        f"collector-state:{state}:{changed_at}",
                        "collector_state",
                        json.dumps({"old_state": previous["state"], "state": state}),
                        changed_at,
                    ),
                )

    def begin_collector_observation(self, observed_at: str) -> None:
        previous = self.collector_state()
        with self.transaction() as con:
            con.execute(
                """UPDATE collector_state SET state='observing',observed_since=?,approved_at=NULL,
                   canary_account_id=NULL,canary_started_at=NULL,last_health_check_at=?,
                   last_job_started_at=NULL,risk_reason=NULL,updated_at=? WHERE id=1""",
                (observed_at, observed_at, observed_at),
            )
            if previous["state"] != "observing":
                con.execute(
                    """INSERT OR IGNORE INTO events(event_key,kind,payload_json,created_at)
                       VALUES(?,?,?,?)""",
                    (
                        f"collector-state:observing:{observed_at}", "collector_state",
                        json.dumps({"old_state": previous["state"], "state": "observing"}), observed_at,
                    ),
                )

    def update_collector_health(self, checked_at: str) -> None:
        self.conn.execute(
            "UPDATE collector_state SET last_health_check_at=?,updated_at=? WHERE id=1",
            (checked_at, checked_at),
        )
        self.conn.commit()

    def approve_collector_canary(self, account_id: int, approved_at: str) -> None:
        previous = self.collector_state()
        with self.transaction() as con:
            con.execute(
                """UPDATE collector_state SET state='canary',approved_at=?,canary_account_id=?,
                   canary_started_at=?,updated_at=? WHERE id=1""",
                (approved_at, account_id, approved_at, approved_at),
            )
            con.execute(
                """INSERT OR IGNORE INTO events(event_key,kind,payload_json,created_at)
                   VALUES(?,?,?,?)""",
                (
                    f"collector-state:canary:{approved_at}", "collector_state",
                    json.dumps({"old_state": previous["state"], "state": "canary"}), approved_at,
                ),
            )

    def place_collector_risk_hold(self, reason: str) -> None:
        now = utc_now()
        previous = self.collector_state()
        with self.transaction() as con:
            con.execute(
                "UPDATE collector_state SET state='risk_hold',risk_reason=?,updated_at=? WHERE id=1",
                (reason, now),
            )
            con.execute(
                "UPDATE relationship_jobs SET status='pending',lease_until=NULL,updated_at=? WHERE status='leased'",
                (now,),
            )
            con.execute(
                """UPDATE post_jobs SET status='paused',lease_until=NULL,last_error=?,updated_at=?
                   WHERE status IN ('pending','leased')""",
                (reason, now),
            )
            con.execute(
                """UPDATE post_feature_state
                   SET state=CASE WHEN state='disabled' THEN state ELSE 'suspended' END,
                       suspended_at=CASE WHEN state='disabled' THEN suspended_at ELSE ? END,
                       suspension_reason=CASE WHEN state='disabled' THEN suspension_reason ELSE ? END,
                       updated_at=? WHERE id=1""",
                (now, reason, now),
            )
            if previous["state"] != "risk_hold":
                con.execute(
                    """INSERT OR IGNORE INTO events(event_key,kind,payload_json,created_at)
                       VALUES(?,?,?,?)""",
                    (
                        f"collector-risk:{now}", "collector_state",
                        json.dumps({"old_state": previous["state"], "state": "risk_hold", "reason": reason}), now,
                    ),
                )

    def finish_relationship_job(self, job_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """UPDATE relationship_jobs SET status=?,lease_until=NULL,last_error=?,updated_at=? WHERE id=?""",
            (status, error, utc_now(), job_id),
        )
        self.conn.commit()

    def start_relationship_run(
        self, job_id: int, account_id: int, direction: str, started_at: str
    ) -> int:
        cursor = self.conn.execute(
            """INSERT INTO relationship_runs(job_id,account_id,direction,status,started_at)
               VALUES(?,?,?,'running',?)""",
            (job_id, account_id, direction, started_at),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def stage_relationship_members(self, run_id: int, members: Iterable[Any]) -> None:
        now = utc_now()
        with self.transaction() as con:
            start = con.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM relationship_run_members WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            for offset, member in enumerate(members):
                prior = con.execute(
                    "SELECT username FROM relationship_members WHERE instagram_profile_id=?",
                    (member.profile_id,),
                ).fetchone()
                con.execute(
                    """INSERT INTO relationship_members(
                         instagram_profile_id,username,display_name,avatar_url,username_observed_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(instagram_profile_id) DO UPDATE SET
                         username=excluded.username,
                         display_name=COALESCE(excluded.display_name,relationship_members.display_name),
                         avatar_url=COALESCE(excluded.avatar_url,relationship_members.avatar_url),
                         username_observed_at=excluded.username_observed_at,updated_at=excluded.updated_at""",
                    (member.profile_id, member.username, member.display_name, member.avatar_url, now, now, now),
                )
                if prior and prior["username"].casefold() != member.username.casefold():
                    con.execute(
                        """INSERT OR IGNORE INTO member_enrichment_jobs(
                             instagram_profile_id,reason,status,available_at,created_at,updated_at
                           ) VALUES(?,'renamed','pending',?,?,?)""",
                        (member.profile_id, now, now, now),
                    )
                con.execute(
                    """INSERT OR REPLACE INTO relationship_run_members(
                         run_id,instagram_profile_id,username,position) VALUES(?,?,?,?)""",
                    (run_id, member.profile_id, member.username, start + offset),
                )

    def finish_relationship_run(
        self, run_id: int, status: str, complete: bool, count: int, error: str | None = None
    ) -> None:
        self.conn.execute(
            """UPDATE relationship_runs SET status=?,complete=?,collected_count=?,error=?,finished_at=?
               WHERE id=?""",
            (status, int(complete), count, error, utc_now(), run_id),
        )
        self.conn.commit()

    def apply_complete_relationship_run(
        self, run_id: int, account_id: int, direction: str, observed_at: str,
        member_stale_days: int = 30,
    ) -> None:
        baseline_column = {
            "followers": "followers_baseline_at", "following": "following_baseline_at"
        }[direction]
        with self.transaction() as con:
            account = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            staged_rows = con.execute(
                "SELECT instagram_profile_id,username FROM relationship_run_members WHERE run_id=?",
                (run_id,),
            ).fetchall()
            staged = {row["instagram_profile_id"]: row["username"] for row in staged_rows}
            existing_rows = con.execute(
                """SELECT instagram_profile_id,username FROM account_relationships
                   WHERE account_id=? AND direction=? AND active=1""",
                (account_id, direction),
            ).fetchall()
            existing = {row["instagram_profile_id"]: row["username"] for row in existing_rows}
            has_baseline = account[baseline_column] is not None
            opposite_baseline = (
                account["following_baseline_at"] if direction == "followers"
                else account["followers_baseline_at"]
            )
            existing_mutual_rows = con.execute(
                """SELECT f.instagram_profile_id,f.username FROM account_relationships f
                   JOIN account_relationships g ON g.account_id=f.account_id
                    AND g.instagram_profile_id=f.instagram_profile_id
                    AND g.direction='following' AND g.active=1
                   WHERE f.account_id=? AND f.direction='followers' AND f.active=1""",
                (account_id,),
            ).fetchall()
            existing_mutual = {
                row["instagram_profile_id"]: row["username"] for row in existing_mutual_rows
            }
            joined = sorted(set(staged) - set(existing))
            left = sorted(set(existing) - set(staged))
            interval_change = int(account["relationship_frozen_at"] is not None)

            for profile_id, username in staged.items():
                con.execute(
                    """INSERT INTO account_relationships(
                         account_id,direction,instagram_profile_id,username,first_seen_at,last_seen_at,active
                       ) VALUES(?,?,?,?,?,?,1)
                       ON CONFLICT(account_id,direction,instagram_profile_id) DO UPDATE SET
                         username=excluded.username,last_seen_at=excluded.last_seen_at,active=1,removed_at=NULL""",
                    (account_id, direction, profile_id, username, observed_at, observed_at),
                )
            for profile_id in left:
                con.execute(
                    """UPDATE account_relationships SET active=0,removed_at=?,last_seen_at=?
                       WHERE account_id=? AND direction=? AND instagram_profile_id=?""",
                    (observed_at, observed_at, account_id, direction, profile_id),
                )
            if has_baseline:
                for kind, identifiers, names in (
                    ("joined", joined, staged), ("left", left, existing)
                ):
                    for profile_id in identifiers:
                        con.execute(
                            """INSERT INTO relationship_history(
                                 account_id,direction,change_kind,instagram_profile_id,username,
                                 interval_change,observed_at,run_id) VALUES(?,?,?,?,?,?,?,?)""",
                            (account_id, direction, kind, profile_id, names[profile_id], interval_change, observed_at, run_id),
                        )
            new_mutual_rows = con.execute(
                """SELECT f.instagram_profile_id,f.username FROM account_relationships f
                   JOIN account_relationships g ON g.account_id=f.account_id
                    AND g.instagram_profile_id=f.instagram_profile_id
                    AND g.direction='following' AND g.active=1
                   WHERE f.account_id=? AND f.direction='followers' AND f.active=1""",
                (account_id,),
            ).fetchall()
            new_mutual = {row["instagram_profile_id"]: row["username"] for row in new_mutual_rows}
            mutual_joined = sorted(set(new_mutual) - set(existing_mutual))
            mutual_left = sorted(set(existing_mutual) - set(new_mutual))
            if has_baseline and opposite_baseline:
                for kind, identifiers, names in (
                    ("joined", mutual_joined, new_mutual), ("left", mutual_left, existing_mutual)
                ):
                    for profile_id in identifiers:
                        con.execute(
                            """INSERT INTO relationship_history(
                                 account_id,direction,change_kind,instagram_profile_id,username,
                                 interval_change,observed_at,run_id) VALUES(?,'mutual',?,?,?,?,?,?)""",
                            (account_id, kind, profile_id, names[profile_id], interval_change, observed_at, run_id),
                        )
            for profile_id in joined:
                member_row = con.execute(
                    "SELECT profile_observed_at FROM relationship_members WHERE instagram_profile_id=?",
                    (profile_id,),
                ).fetchone()
                stale_cutoff = (
                    datetime.fromisoformat(observed_at) - timedelta(days=member_stale_days)
                ).isoformat(timespec="seconds")
                if not member_row["profile_observed_at"] or member_row["profile_observed_at"] <= stale_cutoff:
                    con.execute(
                        """INSERT OR IGNORE INTO member_enrichment_jobs(
                             instagram_profile_id,reason,status,available_at,created_at,updated_at
                           ) VALUES(?,'new_member','pending',?,?,?)""",
                        (profile_id, observed_at, observed_at, observed_at),
                    )
            payload = {
                "label": account["label"], "direction": direction,
                "baseline": not has_baseline, "total": len(staged),
                "joined": [staged[item] for item in joined[:20]],
                "left": [existing[item] for item in left[:20]],
                "joined_count": len(joined) if has_baseline else 0,
                "left_count": len(left) if has_baseline else 0,
                "mutual_available": bool(opposite_baseline),
                "mutual_joined": [new_mutual[item] for item in mutual_joined[:20]],
                "mutual_left": [existing_mutual[item] for item in mutual_left[:20]],
                "mutual_joined_count": len(mutual_joined) if has_baseline and opposite_baseline else 0,
                "mutual_left_count": len(mutual_left) if has_baseline and opposite_baseline else 0,
                "private_interval": bool(interval_change),
                "private_interval_started_at": account["relationship_frozen_at"],
            }
            con.execute(
                """INSERT OR IGNORE INTO events(event_key,account_id,kind,payload_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (f"relationship:{run_id}", account_id, "relationship_digest", json.dumps(payload, ensure_ascii=False), observed_at),
            )
            con.execute(
                f"""UPDATE accounts SET {baseline_column}=?,
                    relationship_status=CASE
                      WHEN relationship_status='scope_exceeded' THEN 'scope_exceeded'
                      WHEN relationship_frozen_at IS NULL THEN 'complete' ELSE 'frozen' END,
                    relationship_reconciled_at=?,updated_at=? WHERE id=?""",
                (observed_at, observed_at, observed_at, account_id),
            )
            con.execute("DELETE FROM relationship_run_members WHERE run_id=?", (run_id,))

    def finalize_relationship_account(self, account_id: int, observed_at: str) -> None:
        self.conn.execute(
            """UPDATE accounts SET relationship_status=CASE
                 WHEN relationship_status='scope_exceeded' THEN 'scope_exceeded' ELSE 'complete' END,
               relationship_frozen_at=NULL,
               relationship_reconciled_at=?,updated_at=? WHERE id=?""",
            (observed_at, observed_at, account_id),
        )
        self.conn.commit()

    def relationship_memberships(self, account_id: int, direction: Any) -> list[dict[str, Any]]:
        value = direction.value if hasattr(direction, "value") else str(direction)
        rows = self.conn.execute(
            """SELECT ar.*,rm.display_name,rm.avatar_url,rm.profile_observed_at
               FROM account_relationships ar JOIN relationship_members rm
                 ON rm.instagram_profile_id=ar.instagram_profile_id
               WHERE ar.account_id=? AND ar.direction=? AND ar.active=1 ORDER BY ar.username""",
            (account_id, value),
        ).fetchall()
        return [dict(row) for row in rows]

    def relationship_history(self, account_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM relationship_history WHERE account_id=? ORDER BY id", (account_id,)
        )]

    def set_verified_identity(self, account_id: int, profile_id: str, observed_at: str) -> None:
        self.conn.execute(
            """UPDATE accounts SET instagram_profile_id=?,identity_verified_source='instagrapi',
               identity_verified_at=?,identity_conflict_json=NULL,updated_at=? WHERE id=?""",
            (profile_id, observed_at, observed_at, account_id),
        )
        self.conn.commit()

    def record_identity_conflict(self, account_id: int, observed_profile_id: str, observed_at: str) -> None:
        account = self.get_account_by_id(account_id)
        payload = {
            "stored_profile_id": account.get("instagram_profile_id") if account else None,
            "observed_profile_id": observed_profile_id,
            "observed_at": observed_at,
        }
        with self.transaction() as con:
            con.execute(
                "UPDATE accounts SET identity_conflict_json=?,relationship_status='identity_conflict',updated_at=? WHERE id=?",
                (json.dumps(payload), observed_at, account_id),
            )
            con.execute(
                """INSERT OR IGNORE INTO events(event_key,account_id,kind,payload_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (f"identity-conflict:{account_id}:{observed_at}", account_id, "identity_conflict", json.dumps(payload), observed_at),
            )

    def member_enrichment_count_for_taipei_day(self, now: datetime) -> int:
        local_day = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
        row = self.conn.execute(
            """SELECT COUNT(*) FROM member_enrichment_attempts
               WHERE date(datetime(attempted_at), '+8 hours')=?""", (local_day,)
        ).fetchone()
        return int(row[0])

    def last_member_enrichment_attempt(self) -> str | None:
        row = self.conn.execute(
            "SELECT attempted_at FROM member_enrichment_attempts ORDER BY attempted_at DESC,id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def claim_member_enrichment_job(self, now: str) -> dict[str, Any] | None:
        lease_until = (datetime.fromisoformat(now) + timedelta(minutes=10)).isoformat(timespec="seconds")
        with self.transaction() as con:
            con.execute(
                """UPDATE member_enrichment_jobs SET status='pending',lease_until=NULL,updated_at=?
                   WHERE status='leased' AND lease_until<?""", (now, now)
            )
            row = con.execute(
                """SELECT * FROM member_enrichment_jobs WHERE status='pending' AND available_at<=?
                   ORDER BY CASE reason WHEN 'manual' THEN 0 WHEN 'renamed' THEN 1 ELSE 2 END,id LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            con.execute(
                """UPDATE member_enrichment_jobs SET status='leased',lease_until=?,attempts=attempts+1,
                   updated_at=? WHERE id=?""", (lease_until, now, row["id"])
            )
            con.execute(
                """INSERT INTO member_enrichment_attempts(instagram_profile_id,status,attempted_at)
                   VALUES(?,'started',?)""", (row["instagram_profile_id"], now)
            )
            result = dict(row)
            result["status"] = "leased"
            return result

    def relationship_member(self, profile_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM relationship_members WHERE instagram_profile_id=?", (profile_id,)
        ).fetchone()
        return dict(row) if row else None

    def enqueue_member_enrichment(self, profile_id: str, reason: str, available_at: str) -> bool:
        now = utc_now()
        try:
            cursor = self.conn.execute(
                """INSERT INTO member_enrichment_jobs(
                     instagram_profile_id,reason,status,available_at,created_at,updated_at
                   ) VALUES(?,?,'pending',?,?,?)""",
                (profile_id, reason, available_at, now, now),
            )
            self.conn.commit()
            return cursor.rowcount == 1
        except sqlite3.IntegrityError:
            return False

    def finish_member_enrichment_job(self, job_id: int, status: str, error: str | None = None) -> None:
        with self.transaction() as con:
            job = con.execute("SELECT * FROM member_enrichment_jobs WHERE id=?", (job_id,)).fetchone()
            con.execute(
                """UPDATE member_enrichment_jobs SET status=?,lease_until=NULL,last_error=?,updated_at=?
                   WHERE id=?""", (status, error, utc_now(), job_id)
            )
            if job:
                con.execute(
                    """UPDATE member_enrichment_attempts SET status=?,error=?
                       WHERE id=(SELECT MAX(id) FROM member_enrichment_attempts
                                WHERE instagram_profile_id=?)""",
                    (status, error, job["instagram_profile_id"]),
                )

    def retry_member_enrichment_job(self, job_id: int, available_at: str, error: str) -> None:
        with self.transaction() as con:
            job = con.execute("SELECT * FROM member_enrichment_jobs WHERE id=?", (job_id,)).fetchone()
            con.execute(
                """UPDATE member_enrichment_jobs SET status='pending',available_at=?,lease_until=NULL,
                   last_error=?,updated_at=? WHERE id=?""", (available_at, error, utc_now(), job_id)
            )
            if job:
                con.execute(
                    """UPDATE member_enrichment_attempts SET status='failed',error=?
                       WHERE id=(SELECT MAX(id) FROM member_enrichment_attempts
                                WHERE instagram_profile_id=?)""",
                    (error, job["instagram_profile_id"]),
                )

    def apply_member_profile(self, job: dict[str, Any], snapshot: ProfileSnapshot, observed_at: str) -> None:
        profile_id = job["instagram_profile_id"]
        current = self.relationship_member(profile_id)
        old_username = current["username"] if current else None
        with self.transaction() as con:
            con.execute(
                """UPDATE relationship_members SET username=?,display_name=?,avatar_url=?,
                   avatar_sha256=?,avatar_path=?,posts=?,
                   followers=?,following=?,bio=?,privacy=?,profile_observed_at=?,
                   username_observed_at=?,updated_at=? WHERE instagram_profile_id=?""",
                (
                    snapshot.username, snapshot.display_name, snapshot.avatar_url,
                    snapshot.avatar_sha256, snapshot.avatar_path, snapshot.posts,
                    snapshot.followers, snapshot.following, snapshot.bio, snapshot.privacy.value,
                    observed_at, observed_at, observed_at, profile_id,
                ),
            )
            con.execute(
                "UPDATE account_relationships SET username=? WHERE instagram_profile_id=?",
                (snapshot.username, profile_id),
            )
            con.execute(
                """UPDATE member_enrichment_jobs SET status='completed',lease_until=NULL,last_error=NULL,
                   updated_at=? WHERE id=?""", (observed_at, job["id"])
            )
            con.execute(
                """UPDATE member_enrichment_attempts SET status='completed'
                   WHERE id=(SELECT MAX(id) FROM member_enrichment_attempts
                            WHERE instagram_profile_id=?)""", (profile_id,)
            )
            if old_username and old_username.casefold() != snapshot.username.casefold():
                con.execute(
                    """INSERT INTO relationship_history(
                         account_id,direction,change_kind,instagram_profile_id,username,observed_at)
                       SELECT DISTINCT account_id,'profile','renamed',?,?,? FROM account_relationships
                       WHERE instagram_profile_id=?""",
                    (profile_id, f"{old_username} -> {snapshot.username}", observed_at, profile_id),
                )

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
