"""Clinic timers (ТЗ §8).

All offsets come from tenant config; quiet hours are applied by
``jobs.enqueue``.  Rescheduling a visit replaces its jobs rather than adding
new ones, so a patient never receives two −24h reminders.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from arix.core.context import TenantContext
from arix.core.timeutil import combine_local, local_date, now_utc, parse_hhmm, to_tenant
from arix.scheduler import jobs

from . import booking as bk


async def schedule_visit_jobs(
    session: AsyncSession, ctx: TenantContext, visit_id: uuid.UUID
) -> list[str]:
    """(Re)arm every timer attached to one visit. Returns the job kinds armed."""
    refs = await bk.visit_with_refs(session, ctx.id, visit_id)
    if refs is None:
        return []
    visit, _doctor, service, _contact = refs
    if visit.status in ("cancelled", "no_show", "done"):
        await cancel_visit_jobs(session, ctx, visit_id)
        return []

    settings = ctx.settings
    quiet = settings.quiet
    armed: list[str] = []

    for offset in settings.reminders.visit_offsets_h:
        run_at = visit.starts_at + timedelta(hours=float(offset))
        kind = "reminder_24h" if abs(float(offset)) >= 12 else "reminder_2h"
        if run_at <= now_utc():
            continue
        # A −2h reminder for an 08:00 visit would land inside quiet hours;
        # ТЗ §7 С5 wants it sent at 08:00 local instead of being pushed to 09:00.
        if kind == "reminder_2h" and quiet and quiet.contains_local(to_tenant(run_at, ctx.tz_name)):
            send_at_local = parse_hhmm(settings.reminders.early_morning_send_at)
            candidate = combine_local(
                local_date(visit.starts_at, ctx.tz_name), send_at_local, ctx.tz_name
            )
            run_at = candidate if candidate > now_utc() else run_at
            respect_quiet = False
        else:
            respect_quiet = True
        await jobs.enqueue(
            session, ctx.id, kind, run_at,
            dedupe_key=jobs.dedupe(kind, visit_id),
            payload={"visit_id": str(visit_id)},
            timezone=ctx.tz_name, quiet=quiet,
            respect_quiet_hours=respect_quiet, replace=True,
        )
        armed.append(kind)

    # "Call the patient" task when the −24h reminder stays unanswered.
    call_at = visit.starts_at - timedelta(hours=settings.reminders.call_task_before_h)
    if call_at > now_utc():
        await jobs.enqueue(
            session, ctx.id, "call_task_admin", call_at,
            dedupe_key=jobs.dedupe("call_task", visit_id),
            payload={"visit_id": str(visit_id)},
            timezone=ctx.tz_name, quiet=quiet, replace=True,
        )
        armed.append("call_task_admin")

    # Auto-complete 2h after the visit ends unless marked otherwise.
    await jobs.enqueue(
        session, ctx.id, "visit_autocomplete", visit.ends_at + timedelta(hours=2),
        dedupe_key=jobs.dedupe("autocomplete", visit_id),
        payload={"visit_id": str(visit_id)},
        timezone=ctx.tz_name, replace=True,
    )
    armed.append("visit_autocomplete")

    if visit.status == "hold" and visit.hold_expires_at:
        await jobs.enqueue(
            session, ctx.id, "hold_expire", visit.hold_expires_at,
            dedupe_key=jobs.dedupe("hold_expire", visit_id),
            payload={"visit_id": str(visit_id)},
            timezone=ctx.tz_name, replace=True,
        )
        armed.append("hold_expire")
    return armed


async def cancel_visit_jobs(
    session: AsyncSession, ctx: TenantContext, visit_id: uuid.UUID
) -> int:
    cancelled = 0
    # These must match the prefixes used by ``schedule_visit_jobs`` exactly.
    for prefix in ("reminder_24h", "reminder_2h", "call_task", "hold_expire", "autocomplete"):
        cancelled += await jobs.cancel(
            session, ctx.id, dedupe_key=jobs.dedupe(prefix, visit_id)
        )
    return cancelled


async def schedule_after_visit(
    session: AsyncSession, ctx: TenantContext, visit_id: uuid.UUID
) -> None:
    """Review request and recall invitation once a visit is done."""
    refs = await bk.visit_with_refs(session, ctx.id, visit_id)
    if refs is None:
        return
    visit, _doctor, service, _contact = refs
    settings = ctx.settings

    if settings.features.reviews:
        await jobs.enqueue(
            session, ctx.id, "review_request",
            visit.ends_at + timedelta(hours=settings.reviews.delay_h),
            dedupe_key=jobs.dedupe("review", visit_id),
            payload={"visit_id": str(visit_id)},
            timezone=ctx.tz_name, quiet=settings.quiet,
        )

    if settings.features.recall and service.recall_days:
        run_at = visit.starts_at + timedelta(
            days=service.recall_days - settings.recall.lead_days
        )
        if run_at > now_utc():
            await jobs.enqueue(
                session, ctx.id, "recall_invite", run_at,
                dedupe_key=jobs.dedupe("recall", visit_id),
                payload={"visit_id": str(visit_id), "service_id": str(service.id)},
                timezone=ctx.tz_name, quiet=settings.quiet,
            )


async def schedule_noshow_rebook(
    session: AsyncSession, ctx: TenantContext, visit_id: uuid.UUID
) -> None:
    await jobs.enqueue(
        session, ctx.id, "noshow_rebook", now_utc() + timedelta(hours=1),
        dedupe_key=jobs.dedupe("noshow", visit_id),
        payload={"visit_id": str(visit_id)},
        timezone=ctx.tz_name, quiet=ctx.settings.quiet,
    )


async def schedule_owner_report(session: AsyncSession, ctx: TenantContext) -> None:
    """Arm tomorrow's 08:00 report. Idempotent per local day."""
    local_now = to_tenant(now_utc(), ctx.tz_name)
    target_time = parse_hhmm(ctx.settings.owner_report_at)
    day = local_now.date()
    run_at = combine_local(day, target_time, ctx.tz_name)
    if run_at <= now_utc():
        run_at = combine_local(day + timedelta(days=1), target_time, ctx.tz_name)
    await jobs.enqueue(
        session, ctx.id, "owner_report", run_at,
        dedupe_key=jobs.dedupe("owner_report", run_at.date().isoformat()),
        payload={},
        timezone=ctx.tz_name, respect_quiet_hours=False,
    )


__all__ = [
    "cancel_visit_jobs",
    "schedule_after_visit",
    "schedule_noshow_rebook",
    "schedule_owner_report",
    "schedule_visit_jobs",
]
