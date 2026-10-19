"""Durable job queue built on ``scheduled_jobs``.

Why not plain arq/Celery ETA? Because a reminder must survive a redis flush, be
visible to support ("why did this patient get two messages?"), be cancellable
by business rules, and be idempotent across webhook replays.  Redis is used for
the *wake-up*, Postgres is the source of truth.

Every job carries a ``dedupe_key`` that is unique per tenant, so re-running the
same business decision twice is a no-op.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.timeutil import QuietHours, now_utc, shift_out_of_quiet_hours
from arix.db.models_core import ScheduledJob

#: Jobs that talk to a patient and therefore must respect quiet hours.
OUTBOUND_KINDS: frozenset[str] = frozenset(
    {
        "reminder_24h",
        "reminder_2h",
        "followup",
        "recall_invite",
        "dormant_invite",
        "review_request",
        "noshow_rebook",
        "waitlist_offer",
        "treatment_plan_nudge",
        # edu
        "trial_reminder",
        "trial_followup",
        "trial_rebook",
        "pay_reminder",
        "renew_reminder",
        "next_level_offer",
        "parent_report",
        "reactivation",
    }
)

#: Jobs that must fire at the configured instant no matter what (internal work).
EXEMPT_FROM_QUIET = frozenset(
    {
        "hold_expire",
        "handoff_pickup_check",
        "handoff_escalate",
        "owner_report",
        "visit_autocomplete",
        "demo_expire",
        "metrics_rollup",
        "invoice_create",
        "attendance_prompt",
        "absence_notice",
        "night_handoff_digest",
        "wa_quota_check",
    }
)


@dataclass(frozen=True)
class JobRequest:
    kind: str
    run_at: datetime
    dedupe_key: str
    payload: dict


def dedupe(*parts: object) -> str:
    """Build a readable, stable dedupe key: ``reminder_24h:<visit>:v1``."""
    return ":".join(str(p) for p in parts if p is not None)[:190]


async def enqueue(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    run_at: datetime,
    *,
    dedupe_key: str,
    payload: dict | None = None,
    timezone: str = "UTC",
    quiet: QuietHours | None = None,
    respect_quiet_hours: bool | None = None,
    replace: bool = False,
) -> ScheduledJob | None:
    """Insert a job unless an identical one already exists.

    Returns the job row, or ``None`` when a duplicate was suppressed.
    ``replace=True`` reschedules an existing pending job instead (used when a
    visit is moved).
    """
    if respect_quiet_hours is None:
        respect_quiet_hours = kind in OUTBOUND_KINDS and kind not in EXEMPT_FROM_QUIET
    if respect_quiet_hours:
        run_at = shift_out_of_quiet_hours(run_at, timezone, quiet)

    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "kind": kind,
        "run_at": run_at,
        "payload": payload or {},
        "status": "pending",
        "attempts": 0,
        "dedupe_key": dedupe_key,
    }
    stmt = pg_insert(ScheduledJob).values(**values)
    if replace:
        stmt = stmt.on_conflict_do_update(
            index_elements=[ScheduledJob.tenant_id, ScheduledJob.dedupe_key],
            set_={
                "run_at": run_at,
                "payload": values["payload"],
                "status": "pending",
                "attempts": 0,
                "locked_at": None,
                "locked_by": None,
                "finished_at": None,
                "last_error": None,
            },
            where=ScheduledJob.status.in_(["pending", "failed", "cancelled"]),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[ScheduledJob.tenant_id, ScheduledJob.dedupe_key]
        )
    stmt = stmt.returning(ScheduledJob)
    row = (await session.execute(stmt)).scalar_one_or_none()
    return row


async def cancel(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    dedupe_key: str | None = None,
    dedupe_prefix: str | None = None,
    kinds: list[str] | None = None,
) -> int:
    """Cancel pending jobs. Used by stop-conditions (patient replied, booked …)."""
    conds = [ScheduledJob.tenant_id == tenant_id, ScheduledJob.status == "pending"]
    if dedupe_key:
        conds.append(ScheduledJob.dedupe_key == dedupe_key)
    if dedupe_prefix:
        conds.append(ScheduledJob.dedupe_key.like(f"{dedupe_prefix}%"))
    if kinds:
        conds.append(ScheduledJob.kind.in_(kinds))
    result = await session.execute(
        update(ScheduledJob)
        .where(and_(*conds))
        .values(status="cancelled", finished_at=now_utc())
    )
    return result.rowcount or 0


async def claim_due(
    session: AsyncSession,
    worker_id: str,
    *,
    limit: int = 50,
    lease_seconds: int = 300,
) -> list[ScheduledJob]:
    """Atomically lease due jobs.

    ``FOR UPDATE SKIP LOCKED`` lets several workers share the queue without
    double-delivery.  A crashed worker's lease expires after ``lease_seconds``
    and the job is picked up again.
    """
    now = now_utc()
    stale = now - timedelta(seconds=lease_seconds)
    subq = (
        select(ScheduledJob.id)
        .where(
            ScheduledJob.run_at <= now,
            or_(
                ScheduledJob.status == "pending",
                and_(ScheduledJob.status == "running", ScheduledJob.locked_at < stale),
            ),
        )
        .order_by(ScheduledJob.run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = list((await session.execute(subq)).scalars())
    if not ids:
        return []
    rows = (
        await session.execute(
            update(ScheduledJob)
            .where(ScheduledJob.id.in_(ids))
            .values(status="running", locked_at=now, locked_by=worker_id)
            .returning(ScheduledJob)
        )
    ).scalars()
    return list(rows)


async def finish(session: AsyncSession, job_id: uuid.UUID) -> None:
    await session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id == job_id)
        .values(status="done", finished_at=now_utc(), locked_at=None, locked_by=None)
    )


async def fail(
    session: AsyncSession, job: ScheduledJob, error: str, *, max_attempts: int = 5
) -> datetime | None:
    """Record a failure and schedule an exponential retry.

    Returns the next run time, or ``None`` when the job is dead (which the
    worker reports to the ARIX ops channel).
    """
    attempts = (job.attempts or 0) + 1
    if attempts >= max_attempts:
        await session.execute(
            update(ScheduledJob)
            .where(ScheduledJob.id == job.id)
            .values(
                status="dead",
                attempts=attempts,
                last_error=error[:2000],
                finished_at=now_utc(),
                locked_at=None,
                locked_by=None,
            )
        )
        return None
    delay = min(2**attempts, 60) * 60 if attempts > 2 else 2**attempts * 30
    next_run = now_utc() + timedelta(seconds=delay)
    await session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id == job.id)
        .values(
            status="pending",
            attempts=attempts,
            last_error=error[:2000],
            run_at=next_run,
            locked_at=None,
            locked_by=None,
        )
    )
    return next_run


async def queue_health(session: AsyncSession) -> dict:
    """Oldest pending job age + per-status counts, for /health and alerts."""
    now = now_utc()
    rows = (
        await session.execute(
            select(ScheduledJob.status, func.count(), func.min(ScheduledJob.run_at)).group_by(
                ScheduledJob.status
            )
        )
    ).all()
    out: dict = {"counts": {}, "oldest_pending_age_s": 0}
    for status, count, oldest in rows:
        out["counts"][status] = count
        if status == "pending" and oldest is not None and oldest < now:
            out["oldest_pending_age_s"] = max(
                out["oldest_pending_age_s"], int((now - oldest).total_seconds())
            )
    return out


__all__ = [
    "EXEMPT_FROM_QUIET",
    "OUTBOUND_KINDS",
    "JobRequest",
    "cancel",
    "claim_due",
    "dedupe",
    "enqueue",
    "fail",
    "finish",
    "queue_health",
]
