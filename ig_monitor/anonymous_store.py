"""Persistence for anonymous collections, independent of authenticated jobs."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any


CATEGORIES = ("posts", "stories", "highlights", "reels")


def latest_collection_source(connection, account_id: int) -> str:
    """Prefer the successfully selected source without requiring a writable migration."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" in tables:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (f"anonymous_active_source:{account_id}",)).fetchone()
        if row and row[0] in {"legacy", "anonyig", "igwatcher"}:
            return row[0]
    if "anonymous_collection_state" in tables:
        row = connection.execute("""
          SELECT source FROM anonymous_collection_state WHERE account_id=? AND last_attempt_at IS NOT NULL
          ORDER BY last_attempt_at DESC,rowid DESC LIMIT 1
        """, (account_id,)).fetchone()
        if row:
            return row[0]
    return "anonyig"


def read_group_observations(connection, account_id: int, source: str | None = None) -> list[dict[str, Any]]:
    """All preserved group observations, with current revision and sticky uncertainty."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anonymous_group_observations'").fetchone():
        return []
    rows = connection.execute("""
      SELECT * FROM anonymous_group_observations WHERE account_id=? AND (? IS NULL OR source=?)
      ORDER BY observed_at DESC,id DESC
    """, (account_id, source, source))
    result = [dict(row) for row in rows]
    incomplete = {
        (row["source"], row["category"], row["group_id"])
        for row in result if not row["complete"]
    }
    seen = set()
    for item in result:
        key = (item["source"], item["category"], item["group_id"])
        item["is_current"] = key not in seen
        item["ever_incomplete"] = key in incomplete
        item["complete"] = bool(item["complete"]) and key not in incomplete
        seen.add(key)
    return result


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
    result = []
    for row in rows:
        item = dict(row)
        for key, default in (("identity_kind", "source"), ("ownership_status", "unspecified"),
                             ("queried_username", None), ("revision_id", "")):
            item.setdefault(key, default)
        if item["source"] == "igwatcher" and item["ownership_status"] == "unspecified":
            item["ownership_status"] = "pending"
        item["is_current"] = True
        result.append(item)
    current = {
        (row["source"], row["category"], row["group_id"]): row["revision_id"]
        for row in read_group_observations(connection, account_id) if row["is_current"]
    }
    # A scrape and its group observations are persisted separately. Preserve a
    # useful projection for older writers without inventing a source identity.
    for item in sorted(result, key=lambda row: (row["updated_at"], row["id"]), reverse=True):
        if item["revision_id"]:
            current.setdefault((item["source"], item["category"], item["group_id"]), item["revision_id"])
    for item in result:
        if item["revision_id"]:
            item["is_current"] = item["revision_id"] == current.get(
                (item["source"], item["category"], item["group_id"])
            )
    return result


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
        CREATE TABLE IF NOT EXISTS anonymous_group_observations (
          id INTEGER PRIMARY KEY,
          account_id INTEGER NOT NULL REFERENCES accounts(id), source TEXT NOT NULL,
          category TEXT NOT NULL, group_id TEXT NOT NULL, revision_id TEXT NOT NULL,
          declared_count INTEGER, received_count INTEGER NOT NULL,
          observed_at TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 0, album_title TEXT,
          UNIQUE(account_id,source,category,group_id,revision_id,observed_at)
        );
        CREATE INDEX IF NOT EXISTS idx_anonymous_group_history
          ON anonymous_group_observations(account_id,source,category,group_id,observed_at);
        CREATE TABLE IF NOT EXISTS anonymous_source_state (
          source TEXT PRIMARY KEY, block_count INTEGER NOT NULL DEFAULT 0,
          next_allowed_at TEXT, failure_since TEXT,
          failure_notified INTEGER NOT NULL DEFAULT 0, error TEXT,
          last_attempt_at TEXT, last_success_at TEXT
        );
        """)
        self._add_column_if_missing("media_memberships", "owner_id", "TEXT")
        self._add_column_if_missing("media_memberships", "owner_username", "TEXT")
        self._add_column_if_missing("media_memberships", "identity_kind", "TEXT NOT NULL DEFAULT 'source'")
        self._add_column_if_missing("media_memberships", "ownership_status", "TEXT NOT NULL DEFAULT 'unspecified'")
        self._add_column_if_missing("media_memberships", "queried_username", "TEXT")
        self._add_column_if_missing("media_memberships", "revision_id", "TEXT NOT NULL DEFAULT ''")
        self.conn.commit()
        self._backfill_ungrouped_legacy_memberships()

    def _backfill_ungrouped_legacy_memberships(self) -> None:
        # Only pre-upgrade unrepresented media is historical legacy evidence.
        # Running this discovery again would mislabel new duplicate rows whose
        # memberships were deliberately moved to a canonical file.
        marker = "anonymous_legacy_memberships_backfilled_v1"
        with self.transaction() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone():
                return
            rows = con.execute("""
              SELECT m.* FROM media m WHERE NOT EXISTS (
                SELECT 1 FROM media_memberships mm WHERE mm.media_id=m.id
              ) ORDER BY m.id
            """).fetchall()
            timestamp = _time().isoformat(timespec="microseconds")
            for row in rows:
                canonical = self._canonical_membership_target(row["id"])
                target = con.execute("SELECT account_id FROM media WHERE id=?", (canonical,)).fetchone()
                if not target or target[0] != row["account_id"]:
                    continue
                categories = con.execute(
                    "SELECT category FROM media_sources WHERE media_id=?", (row["id"],),
                ).fetchall()
                for category, in categories:
                    # An orphaned old duplicate adds no new ownership evidence
                    # for a category that its canonical already represents.
                    if con.execute("SELECT 1 FROM media_memberships WHERE media_id=? AND category=?",
                                   (canonical, category)).fetchone():
                        continue
                    item = SimpleNamespace(
                        media_key=row["media_key"], category=category, position=row["position"],
                        published_at=row["published_at"], source="legacy",
                    )
                    self._record_media_membership(con, row["account_id"], item, canonical, timestamp)
            con.execute("INSERT INTO meta(key,value) VALUES(?,?)", (marker, timestamp))

    def _record_media_membership(self, con, account_id: int, item, media_id: int, now: str) -> None:
        # Re-observing a prior order within one second must outrank a newer row
        # ID from an intervening order; account snapshot timestamps stay intact.
        now = _time().isoformat(timespec="microseconds")
        source = getattr(item, "source", "legacy") or "legacy"
        # Old logical_id/position values do not prove a parent post or album.
        group_id = (getattr(item, "album_id", None) if item.category == "highlights"
                    else getattr(item, "parent_id", None))
        source_media_id = getattr(item, "source_media_id", None)
        revision_id = getattr(item, "revision_id", "") or ""
        ownership_status = getattr(item, "ownership_status", "unspecified")
        if source == "igwatcher" and ownership_status == "unspecified":
            ownership_status = "pending"
        parts = [group_id, source_media_id or item.media_key, item.position]
        if revision_id:
            parts.append(revision_id)
        membership_key = json.dumps(
            parts, separators=(",", ":")
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
            media_id,published_at,created_at,updated_at,
            identity_kind,ownership_status,queried_username,revision_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(account_id,source,category,membership_key) DO UPDATE SET
            media_id=excluded.media_id,album_title=COALESCE(excluded.album_title,album_title),
            caption=COALESCE(excluded.caption,caption),
            owner_id=COALESCE(excluded.owner_id,owner_id),
            owner_username=COALESCE(excluded.owner_username,owner_username),
            identity_kind=excluded.identity_kind,
            ownership_status=CASE WHEN excluded.ownership_status='unspecified'
              THEN ownership_status ELSE excluded.ownership_status END,
            queried_username=COALESCE(excluded.queried_username,queried_username),
            published_at=COALESCE(excluded.published_at,published_at),updated_at=excluded.updated_at
        """, (account_id, source, item.category, membership_key, group_id,
              getattr(item, "album_title", None), getattr(item, "caption", None),
              source_media_id, getattr(item, "owner_id", None) or None,
              getattr(item, "owner_username", None) or None,
              item.position, media_id, item.published_at, now, now,
              getattr(item, "identity_kind", "source"), ownership_status,
              getattr(item, "queried_username", None), revision_id))

    def gallery_memberships(self, account_id: int) -> list[dict[str, Any]]:
        return read_gallery_memberships(self.conn, account_id)

    def record_group_observations(self, account_id: int, source: str, groups) -> None:
        """Append observations; a missing item or later equal count never deletes history."""
        with self.transaction() as con:
            for group in groups:
                value = group if isinstance(group, dict) else {
                    key: getattr(group, key) for key in (
                        "category", "group_id", "revision_id", "declared_count", "received_count",
                        "observed_at", "complete", "album_title",
                    )
                }
                if value["category"] not in CATEGORIES:
                    raise ValueError("Unknown anonymous group category")
                declared, received = value["declared_count"], value["received_count"]
                if type(received) is not int or received < 0 or (
                    declared is not None and (type(declared) is not int or declared < 0)
                ):
                    raise ValueError("Invalid anonymous group counts")
                complete = bool(value.get("complete")) and (declared is None or declared == received)
                con.execute("""
                  INSERT OR IGNORE INTO anonymous_group_observations(
                    account_id,source,category,group_id,revision_id,declared_count,received_count,
                    observed_at,complete,album_title) VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (account_id, source, value["category"], value["group_id"], value["revision_id"],
                      declared, received, _time(value["observed_at"]).isoformat(timespec="microseconds"),
                      int(complete), value.get("album_title")))

    def group_observations(self, account_id: int, source: str | None = None) -> list[dict[str, Any]]:
        return read_group_observations(self.conn, account_id, source)

    def _canonical_membership_target(self, media_id: int) -> int:
        seen = set()
        while media_id not in seen:
            seen.add(media_id)
            row = self.conn.execute("SELECT duplicate_of_id FROM media WHERE id=?", (media_id,)).fetchone()
            if not row or row[0] is None:
                return media_id
            media_id = row[0]
        return media_id

    def media_requires_review(self, media_id: int) -> bool:
        """Exact-file sharing must not turn pending source membership into author proof."""
        canonical_id = self._canonical_membership_target(media_id)
        return self.conn.execute("""
          SELECT 1 FROM media_memberships WHERE media_id=? AND
            (ownership_status='pending' OR (source='igwatcher' AND ownership_status!='verified')) LIMIT 1
        """, (canonical_id,)).fetchone() is not None

    def media_notification_allowed(self, media_id: int) -> bool:
        """Pending-only files are not attachments; old pre-membership media stays compatible."""
        canonical_id = self._canonical_membership_target(media_id)
        memberships = self.conn.execute(
            "SELECT source,ownership_status FROM media_memberships WHERE media_id=?", (canonical_id,),
        ).fetchall()
        if memberships:
            return any(row["ownership_status"] == "verified" or (
                row["source"] != "igwatcher" and row["ownership_status"] == "unspecified"
            ) for row in memberships)
        return self.conn.execute("SELECT 1 FROM media WHERE id=?", (canonical_id,)).fetchone() is not None

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
                # Conservative IGWatcher success cannot claim completeness, but
                # a clean observation still resolves an operational incident.
                conservative_success = source == "igwatcher" and state == "partial" and not error
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
                elif success or conservative_success:
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
