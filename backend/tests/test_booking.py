"""Booking: double-booking impossibility, holds, reschedule, idempotency."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from arix.core.context import load_context
from arix.db.models_core import Contact
from arix.db.session import tenant_session
from arix.verticals.clinic import booking as bk
from arix.verticals.clinic.models import Service, Visit

from .conftest import requires_db

pytestmark = requires_db


async def _contact(session, tenant_id, phone="+77010000001", name="Тест") -> Contact:
    contact = Contact(
        tenant_id=tenant_id, phone=phone, name=name, lang="ru", source="admin",
        consent_pd_at=datetime.now(UTC),
    )
    session.add(contact)
    await session.flush()
    return contact


async def _service(session, tenant_id, code="hygiene") -> Service:
    return (
        await session.execute(
            select(Service).where(Service.tenant_id == tenant_id, Service.code == code)
        )
    ).scalar_one()


async def test_find_slots_returns_bookable_times(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        service = await _service(session, tenant)
        offers = await bk.find_slots(session, ctx, service_id=service.id, count=3)
    assert len(offers) == 3
    assert all(offer.start > datetime.now(UTC) for offer in offers)
    assert all(offer.key.count("|") == 2 for offer in offers)


async def test_hold_then_confirm_marks_the_visit_booked(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        contact = await _contact(session, tenant)
        service = await _service(session, tenant)
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        visit = await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start,
        )
        assert visit.status == "hold"
        assert visit.hold_expires_at is not None
        confirmed = await bk.confirm_visit(session, ctx, visit.id)
    assert confirmed.status == "booked"
    assert confirmed.hold_expires_at is None


async def test_the_same_slot_cannot_be_held_twice(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        first = await _contact(session, tenant, "+77010000001", "Первый")
        second = await _contact(session, tenant, "+77010000002", "Второй")
        service = await _service(session, tenant)
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        await bk.hold_slot(
            session, ctx, contact_id=first.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start,
        )
        with pytest.raises(bk.SlotTaken):
            await bk.hold_slot(
                session, ctx, contact_id=second.id, doctor_id=offer.doctor_id,
                service_id=service.id, start=offer.start,
            )


async def test_overlapping_but_not_identical_slot_is_also_rejected(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        first = await _contact(session, tenant, "+77010000003")
        second = await _contact(session, tenant, "+77010000004")
        service = await _service(session, tenant)  # 60 min + 10 buffer
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        await bk.hold_slot(
            session, ctx, contact_id=first.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start,
        )
        with pytest.raises(bk.SlotTaken):
            await bk.hold_slot(
                session, ctx, contact_id=second.id, doctor_id=offer.doctor_id,
                service_id=service.id, start=offer.start + timedelta(minutes=30),
            )


async def test_a_taken_slot_disappears_from_find_slots(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        contact = await _contact(session, tenant)
        service = await _service(session, tenant)
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start,
        )
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        service = await _service(session, tenant)
        again = await bk.find_slots(session, ctx, service_id=service.id, count=6)
    assert all(
        not (slot.start == offer.start and slot.doctor_id == offer.doctor_id) for slot in again
    )


async def test_idempotency_key_returns_the_same_visit(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        contact = await _contact(session, tenant)
        service = await _service(session, tenant)
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        first = await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start, idempotency_key="same-key",
        )
        second = await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start, idempotency_key="same-key",
        )
    assert first.id == second.id


async def test_concurrent_booking_of_one_slot_produces_exactly_one_visit(tenant):
    """50 parallel dialogues on one slot — ТЗ §15 load requirement."""
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        service = await _service(session, tenant)
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        contacts = [
            (await _contact(session, tenant, f"+7701000{index:04d}", f"П{index}")).id
            for index in range(50)
        ]

    async def attempt(contact_id: uuid.UUID) -> bool:
        async with tenant_session(tenant) as session:
            ctx = await load_context(session, tenant)
            try:
                await bk.hold_slot(
                    session, ctx, contact_id=contact_id, doctor_id=offer.doctor_id,
                    service_id=offer.service_id, start=offer.start,
                    idempotency_key=f"race:{contact_id}",
                )
                return True
            except bk.SlotTaken:
                return False

    results = await asyncio.gather(*(attempt(cid) for cid in contacts))
    assert sum(results) == 1, f"{sum(results)} bookings slipped through"

    async with tenant_session(tenant) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Visit)
                .where(
                    Visit.tenant_id == tenant,
                    Visit.starts_at == offer.start,
                    Visit.status.in_(["hold", "booked", "confirmed", "arrived"]),
                )
            )
        ).scalar_one()
    assert count == 1


async def test_cancelled_visit_frees_the_slot(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        first = await _contact(session, tenant, "+77010000005")
        second = await _contact(session, tenant, "+77010000006")
        service = await _service(session, tenant)
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
        visit = await bk.hold_slot(
            session, ctx, contact_id=first.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start,
        )
        await bk.cancel_visit(session, ctx, visit.id, "test")
        # Now the same slot must be bookable again.
        reused = await bk.hold_slot(
            session, ctx, contact_id=second.id, doctor_id=offer.doctor_id,
            service_id=service.id, start=offer.start,
        )
    assert reused.status == "hold"


async def test_reschedule_moves_the_visit_and_frees_the_old_slot(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        contact = await _contact(session, tenant)
        service = await _service(session, tenant)
        offers = await bk.find_slots(session, ctx, service_id=service.id, count=6)
        original, target = offers[0], offers[-1]
        visit = await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=original.doctor_id,
            service_id=service.id, start=original.start,
        )
        await bk.confirm_visit(session, ctx, visit.id)
        moved = await bk.reschedule(
            session, ctx, visit.id, target.start, new_doctor_id=target.doctor_id
        )
        assert moved.id != visit.id
        assert moved.starts_at == target.start
        old = (
            await session.execute(select(Visit).where(Visit.id == visit.id))
        ).scalar_one()
    assert old.status == "cancelled"
    assert old.cancelled_reason == "rescheduled"


async def test_reschedule_into_a_taken_slot_raises(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        first = await _contact(session, tenant, "+77010000007")
        second = await _contact(session, tenant, "+77010000008")
        service = await _service(session, tenant)
        offers = await bk.find_slots(session, ctx, service_id=service.id, count=6)
        mine = await bk.hold_slot(
            session, ctx, contact_id=first.id, doctor_id=offers[0].doctor_id,
            service_id=service.id, start=offers[0].start,
        )
        await bk.hold_slot(
            session, ctx, contact_id=second.id, doctor_id=offers[-1].doctor_id,
            service_id=service.id, start=offers[-1].start,
        )
        with pytest.raises(bk.SlotTaken):
            await bk.reschedule(
                session, ctx, mine.id, offers[-1].start, new_doctor_id=offers[-1].doctor_id
            )


async def test_expire_holds_releases_only_stale_holds(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        contact = await _contact(session, tenant)
        service = await _service(session, tenant)
        offers = await bk.find_slots(session, ctx, service_id=service.id, count=2)
        stale = await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=offers[0].doctor_id,
            service_id=service.id, start=offers[0].start, ttl_minutes=10,
        )
        fresh = await bk.hold_slot(
            session, ctx, contact_id=contact.id, doctor_id=offers[1].doctor_id,
            service_id=service.id, start=offers[1].start, ttl_minutes=10,
        )
        stale.hold_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.flush()
        released = await bk.expire_holds(session, tenant)
    assert released == [stale.id]
    async with tenant_session(tenant) as session:
        rows = {
            row.id: row.status
            for row in (
                await session.execute(select(Visit).where(Visit.tenant_id == tenant))
            ).scalars()
        }
    assert rows[stale.id] == "cancelled"
    assert rows[fresh.id] == "hold"


async def test_service_not_bookable_online_is_flagged(tenant):
    async with tenant_session(tenant) as session:
        service = await _service(session, tenant, "implant")
    assert service.online_bookable is False


async def test_route_by_complaint_suggests_services_without_diagnosing(tenant):
    async with tenant_session(tenant) as session:
        matched = await bk.route_by_complaint(session, tenant, "болит зуб при сладком, дырка")
    codes = {service.code for service in matched}
    assert codes
    assert "filling" in codes


async def test_price_text_uses_stored_values_only(tenant):
    async with tenant_session(tenant) as session:
        service = await _service(session, tenant)
        text = bk.service_price_text(service)
    assert "15 000" in text and "25 000" in text


async def test_slot_key_round_trip(tenant):
    async with tenant_session(tenant) as session:
        ctx = await load_context(session, tenant)
        service = await _service(session, tenant)
        offer = (await bk.find_slots(session, ctx, service_id=service.id, count=1))[0]
    doctor_id, service_id, start = bk.SlotOffer.parse_key(offer.key)
    assert doctor_id == offer.doctor_id
    assert service_id == offer.service_id
    assert start == offer.start
