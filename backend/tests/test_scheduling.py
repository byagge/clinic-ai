"""Visit timers and the job handlers that act on them (ТЗ §8)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from arix.channels.dryrun import DryRunAdapter
from arix.core.context import load_context
from arix.core.timeutil import combine_local, local_date, to_tenant
from arix.db.models_core import Contact, Conversation, Event, ScheduledJob
from arix.db.session import tenant_session
from arix.verticals.clinic import booking as bk
from arix.verticals.clinic.models import Review, Service, Visit
from arix.verticals.clinic.scheduling import (
    cancel_visit_jobs,
    schedule_visit_jobs,
)

from .conftest import requires_db

pytestmark = requires_db

TZ = "Asia/Almaty"


async def _booked_visit(session, tenant, *, days_ahead=5, hour=14, code="hygiene"):
    """Create a confirmed visit at a controlled local time."""
    ctx = await load_context(session, tenant)
    contact = Contact(
        tenant_id=tenant, phone="+77010000011", name="Пациент", lang="ru",
        source="admin", consent_pd_at=datetime.now(UTC),
    )
    session.add(contact)
    await session.flush()
    session.add(
        Conversation(
            tenant_id=tenant, contact_id=contact.id, channel_type="tg",
            peer_id=f"peer-{contact.id}", state="active",
        )
    )
    service = (
        await session.execute(
            select(Service).where(Service.tenant_id == tenant, Service.code == code)
        )
    ).scalar_one()
    doctors = await bk.list_doctors(session, tenant, service_id=service.id)
    day = local_date(datetime.now(UTC), TZ) + timedelta(days=days_ahead)
    start = combine_local(day, datetime.min.time().replace(hour=hour), TZ)
    visit = await bk.hold_slot(
        session, ctx, contact_id=contact.id, doctor_id=doctors[0].id,
        service_id=service.id, start=start,
    )
    await bk.confirm_visit(session, ctx, visit.id)
    return ctx, visit, contact, service


async def test_all_timers_are_armed_for_a_future_visit(tenant):
    async with tenant_session(tenant) as session:
        ctx, visit, _contact, _service = await _booked_visit(session, tenant)
        armed = await schedule_visit_jobs(session, ctx, visit.id)
    assert {"reminder_24h", "reminder_2h", "call_task_admin", "visit_autocomplete"} <= set(armed)

    async with tenant_session(tenant) as session:
        jobs = {
            row.kind: row
            for row in (
                await session.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.tenant_id == tenant, ScheduledJob.status == "pending"
                    )
                )
            ).scalars()
        }
    assert jobs["reminder_24h"].run_at == visit.starts_at - timedelta(hours=24)
    assert jobs["reminder_2h"].run_at == visit.starts_at - timedelta(hours=2)


async def test_reminders_never_fire_inside_quiet_hours(tenant):
    """An 08:00 visit would get its -2h reminder at 06:00 — ТЗ §7 С5."""
    async with tenant_session(tenant) as session:
        ctx, visit, _contact, _service = await _booked_visit(session, tenant, hour=8)
        await schedule_visit_jobs(session, ctx, visit.id)
        jobs = {
            row.kind: row
            for row in (
                await session.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.tenant_id == tenant, ScheduledJob.status == "pending"
                    )
                )
            ).scalars()
        }
    quiet = (await _settings(tenant)).quiet
    local_24h = to_tenant(jobs["reminder_24h"].run_at, TZ)
    assert not quiet.contains_local(local_24h), f"-24h fired at {local_24h:%H:%M}"
    # The -2h reminder for an early visit is the one documented exception: it
    # is pulled forward to 08:00 local rather than pushed past 09:00, which
    # would be after the appointment itself.
    assert to_tenant(jobs["reminder_2h"].run_at, TZ).hour == 8


async def _settings(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        return ctx.settings


async def test_rescheduling_replaces_timers_instead_of_adding_them(tenant):
    async with tenant_session(tenant) as session:
        ctx, visit, _contact, service = await _booked_visit(session, tenant)
        await schedule_visit_jobs(session, ctx, visit.id)
        moved_start = visit.starts_at + timedelta(days=1)
        moved = await bk.reschedule(session, ctx, visit.id, moved_start)
        await cancel_visit_jobs(session, ctx, visit.id)
        await schedule_visit_jobs(session, ctx, moved.id)
        pending = list(
            (
                await session.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.tenant_id == tenant,
                        ScheduledJob.kind == "reminder_24h",
                        ScheduledJob.status == "pending",
                    )
                )
            ).scalars()
        )
    assert len(pending) == 1
    assert pending[0].run_at == moved_start - timedelta(hours=24)


async def test_cancelling_a_visit_drops_its_timers(tenant):
    async with tenant_session(tenant) as session:
        ctx, visit, _contact, _service = await _booked_visit(session, tenant)
        await schedule_visit_jobs(session, ctx, visit.id)
        await bk.cancel_visit(session, ctx, visit.id, "test")
        await cancel_visit_jobs(session, ctx, visit.id)
        pending = list(
            (
                await session.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.tenant_id == tenant, ScheduledJob.status == "pending"
                    )
                )
            ).scalars()
        )
    assert pending == []


async def test_reminder_24h_sends_with_action_buttons(tenant):
    from arix.verticals.clinic.handlers import reminder_24h

    DryRunAdapter.clear()
    async with tenant_session(tenant) as session:
        ctx, visit, _contact, _service = await _booked_visit(session, tenant)
        job = ScheduledJob(
            tenant_id=tenant, kind="reminder_24h",
            run_at=datetime.now(UTC), payload={"visit_id": str(visit.id)},
            dedupe_key="t:reminder", status="running",
        )
        session.add(job)
        await session.flush()
        result = await reminder_24h(session, ctx, job)
    assert result["sent"]
    sent = DryRunAdapter.last()
    assert "Приду" in sent.buttons
    assert "Перенести" in sent.buttons


async def test_reminder_is_skipped_for_a_cancelled_visit(tenant):
    from arix.verticals.clinic.handlers import reminder_24h

    DryRunAdapter.clear()
    async with tenant_session(tenant) as session:
        ctx, visit, _contact, _service = await _booked_visit(session, tenant)
        await bk.cancel_visit(session, ctx, visit.id, "test")
        job = ScheduledJob(
            tenant_id=tenant, kind="reminder_24h", run_at=datetime.now(UTC),
            payload={"visit_id": str(visit.id)}, dedupe_key="t:reminder2", status="running",
        )
        session.add(job)
        await session.flush()
        result = await reminder_24h(session, ctx, job)
    assert "skipped" in result
    assert DryRunAdapter.outbox == []


async def test_hold_expire_releases_the_slot_and_tells_the_patient(tenant):
    from arix.verticals.clinic.handlers import hold_expire

    DryRunAdapter.clear()
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        contact = Contact(
            tenant_id=tenant, phone="+77010000012", name="Пациент", lang="ru", source="bot"
        )
        session.add(contact)
        await session.flush()
        session.add(
            Conversation(
                tenant_id=tenant, contact_id=contact.id, channel_type="tg",
                peer_id=f"peer-{contact.id}", state="active",
            )
        )
        service = (
            await session.execute(
                select(Service).where(Service.tenant_id == tenant, Service.code == "hygiene")
            )
        ).scalar_one()
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        visit = await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start,
        )
        job = ScheduledJob(
            tenant_id=tenant, kind="hold_expire", run_at=datetime.now(UTC),
            payload={"visit_id": str(visit.id)}, dedupe_key="t:hold", status="running",
        )
        session.add(job)
        await session.flush()
        result = await hold_expire(session, ctx, job)
        released = (
            await session.execute(select(Visit).where(Visit.id == visit.id))
        ).scalar_one()
    assert result["released"]
    assert released.status == "cancelled"
    assert released.cancelled_reason == "hold_expired"


async def test_autocomplete_marks_done_and_schedules_a_review(tenant):
    from arix.verticals.clinic.handlers import visit_autocomplete

    async with tenant_session(tenant) as session:
        ctx, visit, contact, _service = await _booked_visit(session, tenant, days_ahead=-1)
        job = ScheduledJob(
            tenant_id=tenant, kind="visit_autocomplete", run_at=datetime.now(UTC),
            payload={"visit_id": str(visit.id)}, dedupe_key="t:auto", status="running",
        )
        session.add(job)
        await session.flush()
        result = await visit_autocomplete(session, ctx, job)
        done = (await session.execute(select(Visit).where(Visit.id == visit.id))).scalar_one()
        refreshed = (
            await session.execute(select(Contact).where(Contact.id == contact.id))
        ).scalar_one()
        kinds = {
            row.kind
            for row in (
                await session.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.tenant_id == tenant, ScheduledJob.status == "pending"
                    )
                )
            ).scalars()
        }
    assert result["completed"]
    assert done.status == "done"
    assert refreshed.last_visit_at is not None
    assert "review_request" in kinds
    assert "recall_invite" in kinds  # hygiene has recall_days = 180


async def test_review_5_stars_sends_the_public_link(tenant):
    from arix.seed.provision import publish_config
    from arix.verticals.clinic.reviews import record_review

    async with tenant_session(tenant) as session:
        await publish_config(
            session, tenant, {"reviews": {"links": {"2gis": "https://2gis.kz/x"}}}
        )
    DryRunAdapter.clear()
    async with tenant_session(tenant) as session:
        ctx, visit, contact, _service = await _booked_visit(session, tenant, days_ahead=-1)
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.contact_id == contact.id)
            )
        ).scalar_one()
        await record_review(session, ctx, visit.id, 5, conversation)
        review = (
            await session.execute(select(Review).where(Review.visit_id == visit.id))
        ).scalar_one()
    assert review.score == 5
    assert review.published_link_sent
    assert any("2gis.kz" in text for text in DryRunAdapter.texts())


async def test_review_2_stars_alerts_the_owner_and_never_asks_for_a_public_review(tenant):
    from arix.verticals.clinic.reviews import record_review

    DryRunAdapter.clear()
    async with tenant_session(tenant) as session:
        ctx, visit, contact, _service = await _booked_visit(session, tenant, days_ahead=-1)
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.contact_id == contact.id)
            )
        ).scalar_one()
        await record_review(session, ctx, visit.id, 2, conversation, text="долго ждала")
    texts = DryRunAdapter.texts()
    assert any("2гис" not in text.lower() and "2gis" not in text.lower() for text in texts)
    staff = [item for item in DryRunAdapter.outbox if item.peer_id.isdigit()]
    assert staff and "Низкая оценка" in staff[0].text


async def test_recall_is_skipped_when_the_patient_already_rebooked(tenant):
    from arix.verticals.clinic.handlers import recall_invite

    async with tenant_session(tenant) as session:
        ctx, visit, _contact, _service = await _booked_visit(session, tenant, days_ahead=3)
        job = ScheduledJob(
            tenant_id=tenant, kind="recall_invite", run_at=datetime.now(UTC),
            payload={"visit_id": str(visit.id)}, dedupe_key="t:recall", status="running",
        )
        session.add(job)
        await session.flush()
        result = await recall_invite(session, ctx, job)
    assert result == {"skipped": "already_booked"}


async def test_owner_report_renders_yesterdays_numbers(tenant):
    from arix.verticals.clinic.reports import build_daily_report, render_daily_report

    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        yesterday = local_date(datetime.now(UTC), TZ) - timedelta(days=1)
        session.add(
            Event(
                tenant_id=tenant, type="message_in", source="bot", data={},
                conversation_id=None,
                created_at=combine_local(
                    yesterday, datetime.min.time().replace(hour=12), TZ
                ),
            )
        )
        session.add(
            Event(
                tenant_id=tenant, type="booking_confirmed", source="bot_night", data={},
                created_at=combine_local(
                    yesterday, datetime.min.time().replace(hour=23), TZ
                ),
            )
        )
        await session.flush()
        report = await build_daily_report(session, ctx, yesterday)
        text = render_daily_report(ctx, report)
    assert report.bookings == 1
    assert report.bookings_at_night == 1
    assert "Записались 1" in text
    assert ctx.name in text
