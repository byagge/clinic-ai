"""Durable job queue: dedupe, quiet hours, leasing, retries."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from arix.core.timeutil import QuietHours, to_tenant
from arix.db.models_core import ScheduledJob
from arix.db.session import tenant_session
from arix.scheduler import jobs

from .conftest import requires_db

pytestmark = requires_db

TZ = "Asia/Almaty"


async def test_dedupe_key_suppresses_a_duplicate(tenant):
    run_at = datetime.now(UTC) + timedelta(hours=1)
    async with tenant_session(tenant) as session:
        first = await jobs.enqueue(
            session, tenant, "reminder_24h", run_at,
            dedupe_key="reminder_24h:v1", payload={"visit_id": "x"}, timezone=TZ,
        )
        second = await jobs.enqueue(
            session, tenant, "reminder_24h", run_at,
            dedupe_key="reminder_24h:v1", payload={"visit_id": "x"}, timezone=TZ,
        )
    assert first is not None
    assert second is None


async def test_replace_reschedules_an_existing_job(tenant):
    first_at = datetime.now(UTC) + timedelta(hours=1)
    later = datetime.now(UTC) + timedelta(hours=5)
    async with tenant_session(tenant) as session:
        await jobs.enqueue(
            session, tenant, "reminder_24h", first_at,
            dedupe_key="reminder_24h:v2", timezone=TZ,
        )
        replaced = await jobs.enqueue(
            session, tenant, "reminder_24h", later,
            dedupe_key="reminder_24h:v2", timezone=TZ, replace=True,
        )
    assert replaced is not None
    assert abs((replaced.run_at - later).total_seconds()) < 2


async def test_outbound_job_is_pushed_out_of_quiet_hours(tenant):
    quiet = QuietHours.parse("21:00-09:00")
    # 23:00 local == 18:00 UTC
    run_at = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
    async with tenant_session(tenant) as session:
        job = await jobs.enqueue(
            session, tenant, "followup", run_at,
            dedupe_key="followup:quiet", timezone=TZ, quiet=quiet,
        )
    assert to_tenant(job.run_at, TZ).hour == 9


async def test_internal_job_ignores_quiet_hours(tenant):
    quiet = QuietHours.parse("21:00-09:00")
    run_at = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
    async with tenant_session(tenant) as session:
        job = await jobs.enqueue(
            session, tenant, "hold_expire", run_at,
            dedupe_key="hold_expire:quiet", timezone=TZ, quiet=quiet,
        )
    assert job.run_at == run_at


async def test_claim_due_leases_only_due_jobs(tenant):
    now = datetime.now(UTC)
    async with tenant_session(tenant) as session:
        await jobs.enqueue(
            session, tenant, "hold_expire", now - timedelta(minutes=1),
            dedupe_key="due:1", timezone=TZ,
        )
        await jobs.enqueue(
            session, tenant, "hold_expire", now + timedelta(hours=2),
            dedupe_key="future:1", timezone=TZ,
        )
    async with tenant_session(tenant, bypass_rls=True) as session:
        claimed = await jobs.claim_due(session, "worker-a", limit=10)
    keys = {job.dedupe_key for job in claimed}
    assert "due:1" in keys
    assert "future:1" not in keys


async def test_a_claimed_job_is_not_handed_out_twice(tenant):
    now = datetime.now(UTC)
    async with tenant_session(tenant) as session:
        await jobs.enqueue(
            session, tenant, "hold_expire", now - timedelta(minutes=1),
            dedupe_key="once:1", timezone=TZ,
        )
    async with tenant_session(tenant, bypass_rls=True) as session:
        first = await jobs.claim_due(session, "worker-a", limit=10)
    async with tenant_session(tenant, bypass_rls=True) as session:
        second = await jobs.claim_due(session, "worker-b", limit=10)
    assert any(job.dedupe_key == "once:1" for job in first)
    assert not any(job.dedupe_key == "once:1" for job in second)


async def test_cancel_by_prefix(tenant):
    now = datetime.now(UTC) + timedelta(hours=1)
    async with tenant_session(tenant) as session:
        for step in (1, 2, 3):
            await jobs.enqueue(
                session, tenant, "followup", now,
                dedupe_key=f"followup:conv-1:{step}", timezone=TZ,
            )
        await jobs.enqueue(
            session, tenant, "followup", now,
            dedupe_key="followup:conv-2:1", timezone=TZ,
        )
        cancelled = await jobs.cancel(session, tenant, dedupe_prefix="followup:conv-1")
    assert cancelled == 3
    async with tenant_session(tenant) as session:
        remaining = list(
            (
                await session.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.tenant_id == tenant, ScheduledJob.status == "pending"
                    )
                )
            ).scalars()
        )
    assert {job.dedupe_key for job in remaining} == {"followup:conv-2:1"}


async def test_failure_retries_then_dies(tenant):
    now = datetime.now(UTC)
    async with tenant_session(tenant) as session:
        job = await jobs.enqueue(
            session, tenant, "hold_expire", now,
            dedupe_key="flaky:1", timezone=TZ,
        )
        next_run = await jobs.fail(session, job, "boom", max_attempts=3)
    assert next_run is not None and next_run > now

    async with tenant_session(tenant) as session:
        job = (
            await session.execute(
                select(ScheduledJob).where(ScheduledJob.dedupe_key == "flaky:1")
            )
        ).scalar_one()
        assert job.attempts == 1
        job.attempts = 2
        await session.flush()
        dead = await jobs.fail(session, job, "boom again", max_attempts=3)
    assert dead is None
    async with tenant_session(tenant) as session:
        job = (
            await session.execute(
                select(ScheduledJob).where(ScheduledJob.dedupe_key == "flaky:1")
            )
        ).scalar_one()
    assert job.status == "dead"


async def test_queue_health_reports_lag(tenant):
    async with tenant_session(tenant) as session:
        await jobs.enqueue(
            session, tenant, "hold_expire", datetime.now(UTC) - timedelta(minutes=10),
            dedupe_key="lag:1", timezone=TZ,
        )
    async with tenant_session(tenant, bypass_rls=True) as session:
        health = await jobs.queue_health(session)
    assert health["oldest_pending_age_s"] >= 600


def test_dedupe_builder_skips_none_and_truncates():
    assert jobs.dedupe("reminder", None, "x") == "reminder:x"
    assert len(jobs.dedupe("a" * 300)) == 190
