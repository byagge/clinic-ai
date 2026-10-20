"""The scheduler worker.

A single loop: lease due jobs, run each in its own tenant-scoped transaction,
retry with backoff, report dead jobs to ARIX ops.  One job's failure can never
abort another job's transaction, which is why each gets its own session.

Run with ``python -m arix.scheduler.worker`` (or the ``worker`` compose
service).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid

from sqlalchemy import select

from arix.config import get_settings
from arix.core.context import load_context
from arix.core.timeutil import local_date, now_utc
from arix.db.models_core import ScheduledJob, Tenant
from arix.db.session import admin_session, dispose_engine, tenant_session
from arix.logging_setup import bind_context, clear_context, get_logger, setup_logging
from arix.ops import alert_ops

from . import jobs as jobq
from .registry import daily_arms, handler_for, known_kinds

log = get_logger("arix.worker")

ACTIVE_STATUSES = ("demo", "pilot", "active")


class Worker:
    def __init__(self, worker_id: str | None = None) -> None:
        s = get_settings()
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.poll_interval = s.job_poll_interval_s
        self.batch_size = s.job_batch_size
        self.max_attempts = s.job_max_attempts
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("worker_start", worker=self.id, kinds=len(known_kinds()))
        housekeeping = asyncio.create_task(self._housekeeping_loop())
        try:
            while not self._stop.is_set():
                try:
                    processed = await self.tick()
                except Exception as exc:  # the loop must never die
                    log.exception("worker_tick_failed", error=str(exc))
                    processed = 0
                if processed == 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
        finally:
            housekeeping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await housekeeping
            await dispose_engine()
            log.info("worker_stop", worker=self.id)

    async def tick(self) -> int:
        async with admin_session() as session:
            due = await jobq.claim_due(
                session, self.id, limit=self.batch_size
            )
            claimed = [(job.id, job.tenant_id, job.kind, job.payload, job.attempts) for job in due]
        for job_id, tenant_id, kind, payload, attempts in claimed:
            await self._run_one(job_id, tenant_id, kind, payload, attempts)
        return len(claimed)

    async def _run_one(
        self,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        kind: str,
        payload: dict,
        attempts: int,
    ) -> None:
        bind_context(job_id=str(job_id), tenant_id=str(tenant_id), job_kind=kind)
        handler = handler_for(kind)
        try:
            if handler is None:
                log.error("unknown_job_kind", kind=kind)
                async with admin_session() as session:
                    job = await _reload(session, job_id)
                    if job is not None:
                        await jobq.fail(session, job, f"unknown kind {kind}", max_attempts=1)
                await alert_ops(f"❌ Неизвестный тип задания: {kind} (tenant {tenant_id})")
                return

            async with tenant_session(tenant_id) as session:
                ctx = await load_context(session, tenant_id)
                if ctx.tenant.status not in ACTIVE_STATUSES:
                    # Paused/archived tenant: drop the job instead of retrying.
                    job = await _reload(session, job_id)
                    if job is not None:
                        job.status = "cancelled"
                        job.finished_at = now_utc()
                    log.info("job_skipped_inactive_tenant", status=ctx.tenant.status)
                    return
                job = await _reload(session, job_id)
                if job is None:
                    return
                result = await handler(session, ctx, job)
                await jobq.finish(session, job_id)
                log.info("job_done", result=result)
        except Exception as exc:
            log.exception("job_failed", error=str(exc))
            async with admin_session() as session:
                job = await _reload(session, job_id)
                if job is None:
                    return
                next_run = await jobq.fail(
                    session, job, repr(exc), max_attempts=self.max_attempts
                )
            if next_run is None:
                await alert_ops(
                    f"💀 Задание {kind} (tenant {tenant_id}) умерло после "
                    f"{attempts + 1} попыток: {exc!r}"[:1500]
                )
        finally:
            clear_context()

    async def _housekeeping_loop(self) -> None:
        """Arm per-tenant daily jobs and watch queue health."""
        while not self._stop.is_set():
            try:
                await self.housekeeping()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("housekeeping_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=300)

    async def housekeeping(self) -> None:
        async with admin_session() as session:
            tenants = list(
                (
                    await session.execute(
                        select(Tenant).where(Tenant.status.in_(ACTIVE_STATUSES))
                    )
                ).scalars()
            )
            health = await jobq.queue_health(session)
        if health["oldest_pending_age_s"] > 300:
            await alert_ops(
                f"⏳ Очередь заданий отстаёт на {health['oldest_pending_age_s'] // 60} мин "
                f"({health['counts']})"
            )
        for tenant in tenants:
            async with tenant_session(tenant.id) as session:
                ctx = await load_context(session, tenant.id)
                await self._arm_daily(session, ctx)

    async def _arm_daily(self, session, ctx) -> None:
        """Jobs every vertical shares. Vertical-specific ones register themselves."""
        tz = ctx.tz_name
        quiet = ctx.settings.quiet

        await jobq.arm_local(session, ctx, "owner_report", ctx.settings.owner_report_at)
        if quiet:
            await jobq.arm_local(
                session, ctx, "night_handoff_digest",
                f"{quiet.end.hour:02d}:{quiet.end.minute:02d}",
            )
        await jobq.arm_local(session, ctx, "resolve_stale_handoffs", "03:30")
        if local_date(now_utc(), tz).day == 1:
            await jobq.arm_local(session, ctx, "wa_quota_check", "09:30")
        if ctx.tenant.status == "demo" and ctx.tenant.demo_expires_at:
            await jobq.enqueue(
                session, ctx.id, "demo_expire", ctx.tenant.demo_expires_at,
                dedupe_key=jobq.dedupe("demo_expire", ctx.id),
                payload={}, timezone=tz, respect_quiet_hours=False,
            )
        for arm in daily_arms():
            await arm(session, ctx)


async def _reload(session, job_id: uuid.UUID) -> ScheduledJob | None:
    return (
        await session.execute(select(ScheduledJob).where(ScheduledJob.id == job_id))
    ).scalar_one_or_none()


def _register_vertical() -> None:
    """Import the configured vertical so its handlers land in the registry.

    Resolved by name rather than imported directly: that is what keeps this
    module byte-identical between the clinic and edu deployments
    (see docs/CORE_SYNC.md).
    """
    from importlib import import_module

    module = import_module(f"arix.verticals.{get_settings().vertical}.handlers")
    module.register()


async def main() -> None:
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    _register_vertical()
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
