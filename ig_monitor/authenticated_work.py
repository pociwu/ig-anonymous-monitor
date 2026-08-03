from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .db import Database


TAIPEI = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class AuthenticatedWorkClaim:
    run_id: int
    kind: str
    work_ref_id: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ClaimOutcome:
    status: str
    claim: AuthenticatedWorkClaim | None = None


class AuthenticatedWorkCoordinator:
    """Atomically owns the shared authenticated start budget, spacing, and lease."""

    def __init__(
        self,
        db: Database,
        daily_start_limit: int,
        minimum_start_interval_minutes: int,
        lease_minutes: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.db = db
        self.daily_start_limit = daily_start_limit
        self.minimum_start_interval = timedelta(minutes=minimum_start_interval_minutes)
        self.lease_duration = timedelta(minutes=lease_minutes)
        self.clock = clock

    def claim_next(
        self,
        now: datetime,
        relationship_canary_account_id: int | None = None,
    ) -> ClaimOutcome:
        now_text = now.isoformat(timespec="seconds")
        lease_until = (now + self.lease_duration).isoformat(timespec="seconds")
        budget_day = now.astimezone(TAIPEI).date().isoformat()

        with self.db.transaction() as con:
            expired = con.execute(
                """SELECT work_ref_id FROM authenticated_work_runs
                   WHERE status='running' AND work_kind='relationship' AND lease_until<?""",
                (now_text,),
            ).fetchall()
            if expired:
                con.executemany(
                    """UPDATE relationship_jobs SET status='pending',lease_until=NULL,updated_at=?
                       WHERE id=? AND status='leased'""",
                    ((now_text, row["work_ref_id"]) for row in expired),
                )
            con.execute(
                """UPDATE authenticated_work_runs
                   SET status='abandoned',finished_at=?,outcome='lease_expired'
                   WHERE status='running' AND lease_until<?""",
                (now_text, now_text),
            )

            active = con.execute(
                """SELECT 1 FROM authenticated_work_runs
                   WHERE status='running' AND lease_until>=? LIMIT 1""",
                (now_text,),
            ).fetchone()
            if active:
                return ClaimOutcome("busy")

            starts = con.execute(
                "SELECT COUNT(*) FROM authenticated_work_runs WHERE budget_day=?",
                (budget_day,),
            ).fetchone()[0]
            if int(starts) >= self.daily_start_limit:
                return ClaimOutcome("daily_budget")

            last = con.execute(
                "SELECT MAX(started_at) FROM authenticated_work_runs"
            ).fetchone()[0]
            if last and now < datetime.fromisoformat(last) + self.minimum_start_interval:
                return ClaimOutcome("spacing")

            query = """SELECT j.* FROM relationship_jobs j
                       JOIN accounts a ON a.id=j.account_id
                       WHERE j.status='pending' AND j.available_at<=?
                         AND a.enabled=1 AND a.relationship_tracking=1"""
            params: list[Any] = [now_text]
            if relationship_canary_account_id is not None:
                query += " AND j.account_id=?"
                params.append(relationship_canary_account_id)
            query += """ ORDER BY CASE j.reason
                         WHEN 'reopened' THEN 0 WHEN 'count_change' THEN 1
                         WHEN 'canary' THEN 2 ELSE 3 END,j.id LIMIT 1"""
            row = con.execute(query, params).fetchone()
            if row is None:
                return ClaimOutcome("idle")

            updated = con.execute(
                """UPDATE relationship_jobs
                   SET status='leased',lease_until=?,started_at=?,attempts=attempts+1,updated_at=?
                   WHERE id=? AND status='pending'""",
                (lease_until, now_text, now_text, row["id"]),
            )
            if updated.rowcount != 1:
                return ClaimOutcome("busy")
            cursor = con.execute(
                """INSERT INTO authenticated_work_runs(
                     work_kind,work_ref_id,budget_day,status,lease_until,started_at
                   ) VALUES('relationship',?,?,'running',?,?)""",
                (row["id"], budget_day, lease_until, now_text),
            )
            con.execute(
                "UPDATE collector_state SET last_job_started_at=?,updated_at=? WHERE id=1",
                (now_text, now_text),
            )
            payload = dict(row)
            payload.update(status="leased", lease_until=lease_until, started_at=now_text)
            claim = AuthenticatedWorkClaim(
                int(cursor.lastrowid), "relationship", int(row["id"]), payload
            )
            return ClaimOutcome("claimed", claim)

    def finish(
        self,
        claim: AuthenticatedWorkClaim,
        outcome: str,
        error: str | None = None,
    ) -> None:
        finished_at = self.clock().isoformat(timespec="seconds")
        with self.db.transaction() as con:
            con.execute(
                """UPDATE authenticated_work_runs
                   SET status='finished',finished_at=?,outcome=?,error=?
                   WHERE id=? AND status='running'""",
                (finished_at, outcome, error, claim.run_id),
            )
