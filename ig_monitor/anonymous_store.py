"""Persistence for anonymous collections, independent of authenticated jobs."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any


CATEGORIES = ("posts", "stories", "highlights", "reels")


def read_gallery_memberships(connection, account_id: int) -> list[dict[str, Any]]:
    """Read-only gallery projection, also safe before the first schema migration."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_memberships'").fetchone():
        return []
    rows = connection.execute("""
      SELECT mm.*,m.kind,m.local_path FROM media_memberships mm
      JOIN media m ON m.id=mm.media_id AND m.account_id=mm.account_id
      WHERE mm.account_id=? AND m.status='downloaded' AND m.local_path IS NOT NULL
        AND m.duplicate_of_id IS NULL
        AND NOT EXISTS(SELECT 1 FROM media_quarantine q
                       WHERE q.media_id=m.id AND q.decision='pending')
      ORDER BY mm.published_at DESC,mm.group_id,mm.position,mm.id
    """, (account_id,))
    return [dict(row) for row in rows]


def read_collection_observations(connection, account_id: int, source: str = "anonyig") -> dict[str, dict]:
    result = {category: {
        "state": "unknown", "error": None, "last_success_at": None,
        "last_attempt_at": None, "cursor": None, "complete": False,
        "baseline_complete": False, "fail_count": 0,
    } for category in CATEGORIES}
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anonymous_collection_state'").fetchone():
        return result
    for row in connection.execute(
        "SELECT * FROM anonymous_collection_state WHERE account_id=? AND source=?", (account_id, source),
    ):
        item = dict(row)
        item["complete"] = bool(item["complete"])
        item["baseline_complete"] = bool(item["baseline_complete"])
        result[item["category"]] = item
    return result


def _time(value: datetime | str | None = None) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AnonymousStore:
    def _init_anonymous_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS media_memberships (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id),
          source TEXT NOT NULL, category TEXT NOT NULL, membership_key TEXT NOT NULL,
          group_id TEXT, album_title TEXT, caption TEXT, source_media_id TEXT,
          owner_id TEXT, owner_username TEXT,
          position INTEGER NOT NULL DEFAULT 0,
          media_id INTEGER NOT NULL REFERENCES media(id), published_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(account_id,source,category,membership_key)
        );
        CREATE INDEX IF NOT EXISTS idx_media_memberships_gallery
          ON media_memberships(account_id,category,group_id,position);
        CREATE INDEX IF NOT EXISTS idx_media_memberships_canonical
          ON media_memberships(media_id);
        CREATE TABLE IF NOT EXISTS anonymous_collection_state (
          account_id INTEGER NOT NULL REFERENCES accounts(id),
          source TEXT NOT NULL, category TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'unknown', error TEXT,
          last_success_at TEXT, last_attempt_at TEXT,
          cursor TEXT, complete INTEGER NOT NULL DEFAULT 0,
          baseline_complete INTEGER NOT NULL DEFAULT 0,
          fail_count INTEGER NOT NULL DEFAULT 0,
          failure_notified INTEGER NOT NULL DEFAULT 0, failure_since TEXT,
          PRIMARY KEY(account_id,source,category)
        );
        CREATE TABLE IF NOT EXISTS anonymous_source_state (
          source TEXT PRIMARY KEY, block_count INTEGER NOT NULL DEFAULT 0,
          next_allowed_at TEXT, failure_since TEXT,
          failure_notified INTEGER NOT NULL DEFAULT 0, error TEXT,
          last_attempt_at TEXT, last_success_at TEXT
        );
        """)
        self._add_column_if_missing("media_memberships", "owner_id", "TEXT")
        self._add_column_if_missing("media_memberships", "owner_username", "TEXT")
        self.conn.commit()

    def _record_media_membership(self, con, account_id: int, item, media_id: int, now: str) -> None:
        source = getattr(item, "source", "legacy") or "legacy"
        # Old logical_id/position values do not prove a parent post or album.
        group_id = (getattr(item, "album_id", None) if item.category == "highlights"
                    else getattr(item, "parent_id", None))
        source_media_id = getattr(item, "source_media_id", None)
        membership_key = json.dumps(
            [group_id, source_media_id or item.media_key, item.position], separators=(",", ":")
        )
        # Re-observing a low-quality duplicate must attach to its current canonical.
        seen = set()
        while media_id not in seen:
            seen.add(media_id)
            row = con.execute("SELECT duplicate_of_id FROM media WHERE id=?", (media_id,)).fetchone()
            if not row or row["duplicate_of_id"] is None:
                break
            media_id = row["duplicate_of_id"]
        con.execute(
            "INSERT OR IGNORE INTO media_sources(media_id,category) VALUES(?,?)",
            (media_id, item.category),
        )
        con.execute("""
          INSERT INTO media_memberships(account_id,source,category,membership_key,group_id,
            album_title,caption,source_media_id,owner_id,owner_username,position,
            media_id,published_at,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(account_id,source,category,membership_key) DO UPDATE SET
            media_id=excluded.media_id,album_title=COALESCE(excluded.album_title,album_title),
            caption=COALESCE(excluded.caption,caption),
            owner_id=COALESCE(excluded.owner_id,owner_id),
            owner_username=COALESCE(excluded.owner_username,owner_username),
            published_at=COALESCE(excluded.published_at,published_at),updated_at=excluded.updated_at
        """, (account_id, source, item.category, membership_key, group_id,
              getattr(item, "album_title", None), getattr(item, "caption", None),
              source_media_id, getattr(item, "owner_id", None) or None,
              getattr(item, "owner_username", None) or None,
              item.position, media_id, item.published_at, now, now))

    def gallery_memberships(self, account_id: int) -> list[dict[str, Any]]:
        return read_gallery_memberships(self.conn, account_id)

    def collection_observations(self, account_id: int, source: str = "anonyig") -> dict[str, dict]:
        return read_collection_observations(self.conn, account_id, source)

    def known_source_ids(self, account_id: int, category: str, source: str = "anonyig") -> set[str]:
        if not self.collection_observations(account_id, source).get(category, {}).get("baseline_complete"):
            return set()
        return {row[0] for row in self.conn.execute("""
          SELECT DISTINCT COALESCE(group_id,source_media_id) FROM media_memberships
          WHERE account_id=? AND source=? AND category=?
            AND COALESCE(group_id,source_media_id) IS NOT NULL
        """, (account_id, source, category))}

    def record_collection_observations(
        self, account_id: int, label: str, observations: dict,
        source: str = "anonyig", now: datetime | str | None = None,
        persist_progress: bool = True,
    ) -> None:
        timestamp = _time(now).isoformat(timespec="seconds")
        with self.transaction() as con:
            con.execute("BEGIN IMMEDIATE")
            for category, observation in observations.items():
                if category not in CATEGORIES:
                    raise ValueError(f"Unknown anonymous collection: {category}")
                value = observation if isinstance(observation, dict) else vars_from_observation(observation)
                state = str(value.get("state", "unknown"))
                if state not in {"unknown", "media", "empty", "private", "partial", "failed", "blocked"}:
                    raise ValueError(f"Unknown collection state: {state}")
                con.execute("""INSERT OR IGNORE INTO anonymous_collection_state(account_id,source,category)
                               VALUES(?,?,?)""", (account_id, source, category))
                row = con.execute("""SELECT * FROM anonymous_collection_state
                                     WHERE account_id=? AND source=? AND category=?""",
                                  (account_id, source, category)).fetchone()
                error = value.get("error")
                success = state in {"media", "empty", "private"} and not error
                failure = state in {"failed", "unknown"} or (state == "partial" and bool(error))
                count, since, notified = row["fail_count"], row["failure_since"], row["failure_notified"]
                scope = f"anonymous:{source}:{account_id}:{category}"
                if failure:
                    count += 1
                    since = since or timestamp
                    if count >= 3 and not notified:
                        self._anonymous_event(con, f"failure:{scope}:{since}", account_id, "failure", {
                            "label": f"{label} / {category}", "source": source, "category": category,
                            "scope": "collection", "scope_key": scope, "error": error or "Collection result unknown",
                            "blocker": None, "fail_count": count, "since": since,
                        }, timestamp)
                        notified = 1
                elif success:
                    if notified:
                        self._anonymous_event(con, f"recovery:{scope}:{since}", account_id, "recovery", {
                            "label": f"{label} / {category}", "source": source,
                            "category": category, "scope": "collection", "scope_key": scope, "since": since,
                        }, timestamp)
                    count, since, notified = 0, None, 0
                # Blocked and still-running batches never erase an incident or advance it.
                # A partial result can include a durable checkpoint plus a failed
                # album/page; the source keeps that failed scope in its retry cursor.
                progress = persist_progress and (success or state == "partial")
                # Observation completeness is independent of recording being enabled.
                # Only durable progress can establish the incremental baseline.
                complete = bool(value.get("complete")) and success
                established_baseline = complete and persist_progress and state != "private"
                cursor = value.get("cursor") if progress else row["cursor"]
                con.execute("""UPDATE anonymous_collection_state SET state=?,error=?,last_attempt_at=?,
                  last_success_at=?,cursor=?,complete=?,baseline_complete=?,fail_count=?,
                  failure_notified=?,failure_since=? WHERE account_id=? AND source=? AND category=?""",
                  (state, error, timestamp, timestamp if success else row["last_success_at"],
                   cursor, int(complete), int(bool(row["baseline_complete"]) or established_baseline), count,
                   notified, since, account_id, source, category))

    @staticmethod
    def _anonymous_event(con, key: str, account_id: int | None, kind: str, payload: dict, now: str) -> None:
        con.execute("""INSERT OR IGNORE INTO events(event_key,account_id,kind,payload_json,created_at)
                       VALUES(?,?,?,?,?)""",
                    (key, account_id, kind, json.dumps(payload, ensure_ascii=False), now))

    def source_cooldown(self, source: str, now: datetime | str | None = None) -> dict | None:
        row = self.conn.execute("SELECT * FROM anonymous_source_state WHERE source=?", (source,)).fetchone()
        if row and row["next_allowed_at"] and _time(row["next_allowed_at"]) > _time(now):
            return dict(row)
        return None

    def record_source_block(self, source: str, error: str, now: datetime | str | None = None) -> dict:
        moment = _time(now)
        timestamp = moment.isoformat(timespec="seconds")
        with self.transaction() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR IGNORE INTO anonymous_source_state(source) VALUES(?)", (source,))
            row = con.execute("SELECT * FROM anonymous_source_state WHERE source=?", (source,)).fetchone()
            if row["next_allowed_at"] and _time(row["next_allowed_at"]) > moment:
                return dict(row)
            count = int(row["block_count"]) + 1
            next_allowed = (moment + timedelta(minutes=(30, 60, 120, 240)[min(count - 1, 3)])).isoformat(timespec="seconds")
            since = row["failure_since"] or timestamp
            if not row["failure_notified"]:
                self._anonymous_event(con, f"failure:anonymous-source:{source}:{since}", None, "failure", {
                    "label": source, "source": source, "scope": "source", "scope_key": f"anonymous-source:{source}",
                    "error": error, "blocker": "source_blocked", "fail_count": count,
                    "since": since, "next_allowed_at": next_allowed,
                }, timestamp)
            con.execute("""UPDATE anonymous_source_state SET block_count=?,next_allowed_at=?,
                           failure_since=?,failure_notified=1,error=?,last_attempt_at=? WHERE source=?""",
                        (count, next_allowed, since, error, timestamp, source))
            return dict(con.execute("SELECT * FROM anonymous_source_state WHERE source=?", (source,)).fetchone())

    def record_source_recovery(self, source: str, now: datetime | str | None = None) -> None:
        moment = _time(now)
        timestamp = moment.isoformat(timespec="seconds")
        with self.transaction() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM anonymous_source_state WHERE source=?", (source,)).fetchone()
            if row is None:
                return
            # Another worker may have blocked the source after the caller's
            # cooldown check. Recheck while holding the SQLite write lock.
            if row["next_allowed_at"] and _time(row["next_allowed_at"]) > moment:
                return
            if row["failure_notified"]:
                self._anonymous_event(con, f"recovery:anonymous-source:{source}:{row['failure_since']}", None, "recovery", {
                    "label": source, "source": source, "scope": "source", "scope_key": f"anonymous-source:{source}",
                    "since": row["failure_since"],
                }, timestamp)
            con.execute("""UPDATE anonymous_source_state SET block_count=0,next_allowed_at=NULL,
                           failure_since=NULL,failure_notified=0,error=NULL,last_success_at=? WHERE source=?""",
                        (timestamp, source))


def vars_from_observation(observation) -> dict:
    """Also accepts the slots-based CollectionObservation value object."""
    return {key: getattr(observation, key, None) for key in ("state", "error", "cursor", "complete")}
