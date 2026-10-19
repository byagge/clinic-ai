"""Clinic vertical data model (ТЗ §4.2)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arix.db.base import GUID, Base, JSONColumn, PKMixin, TenantMixin, TimestampMixin

VISIT_STATUSES = (
    "hold",
    "booked",
    "confirmed",
    "arrived",
    "no_show",
    "cancelled",
    "done",
)
#: Statuses that physically occupy the doctor's calendar.
BLOCKING_STATUSES = ("hold", "booked", "confirmed", "arrived")
VISIT_SOURCES = ("bot", "admin", "miniapp", "import")


class Doctor(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "doctors"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    specialty: Mapped[str | None] = mapped_column(String(128))
    langs: Mapped[list[str]] = mapped_column(ARRAY(String(8)), nullable=False, default=list)
    photo_url: Mapped[str | None] = mapped_column(Text)
    bio_short: Mapped[str | None] = mapped_column(Text)
    tg_user_id: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class Service(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "services"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``{"ru": "...", "kz": "...", "uz": "..."}``
    name: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    category: Mapped[str | None] = mapped_column(String(96))
    price_from: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_to: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="KZT")
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    buffer_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prep_kb_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    recall_days: Mapped[int | None] = mapped_column(Integer)
    needs_consultation_first: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    online_bookable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Free-text complaints that should route to this service (``route_by_complaint``).
    complaint_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list
    )
    room_type: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_services_code"),
        CheckConstraint("duration_min > 0", name="duration_positive"),
        CheckConstraint(
            "price_to IS NULL OR price_from IS NULL OR price_to >= price_from",
            name="price_range_sane",
        ),
    )

    def label(self, lang: str = "ru") -> str:
        return self.name.get(lang) or self.name.get("ru") or self.code


class DoctorService(PKMixin, TenantMixin, Base):
    __tablename__ = "doctor_services"

    doctor_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("doctor_id", "service_id", name="uq_doctor_service"),
    )


class Room(PKMixin, TenantMixin, Base):
    __tablename__ = "rooms"

    name: Mapped[str] = mapped_column(String(96), nullable=False)
    room_type: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Schedule(PKMixin, TenantMixin, Base):
    """Weekly working pattern. ``weekday``: 0 = Monday."""

    __tablename__ = "schedules"

    doctor_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start: Mapped[time] = mapped_column(Time, nullable=False)
    end: Mapped[time] = mapped_column(Time, nullable=False)
    break_start: Mapped[time | None] = mapped_column(Time)
    break_end: Mapped[time | None] = mapped_column(Time)

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_valid"),
        CheckConstraint('"end" > "start"', name="end_after_start"),
        CheckConstraint(
            "(break_start IS NULL) = (break_end IS NULL)", name="break_pair"
        ),
        UniqueConstraint("doctor_id", "weekday", "start", name="uq_schedule_slot"),
    )


class ScheduleException(PKMixin, TenantMixin, Base):
    """Day off or a one-off custom window that overrides the weekly pattern."""

    __tablename__ = "schedule_exceptions"

    doctor_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    type: Mapped[str] = mapped_column(String(8), nullable=False, default="off")
    start: Mapped[time | None] = mapped_column(Time)
    end: Mapped[time | None] = mapped_column(Time)
    note: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint("type IN ('off', 'custom')", name="type_valid"),
        CheckConstraint(
            'type = \'off\' OR ("start" IS NOT NULL AND "end" IS NOT NULL AND "end" > "start")',
            name="custom_window_required",
        ),
        UniqueConstraint("doctor_id", "date", "type", "start", name="uq_schedule_exception"),
    )


class Visit(PKMixin, TimestampMixin, TenantMixin, Base):
    """A booked appointment.

    Double booking is impossible by construction: the ``EXCLUDE`` constraint
    below rejects any overlapping period for the same doctor while the visit is
    in a blocking status.  Application code never relies on a read-then-write
    check for this.
    """

    __tablename__ = "visits"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("doctors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("services.id", ondelete="RESTRICT"), nullable=False
    )
    room_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Generated range used by the exclusion constraint; kept in sync by DB.
    period: Mapped[object] = mapped_column(TSTZRANGE, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="hold")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="bot")
    attribution: Mapped[str] = mapped_column(String(32), nullable=False, default="bot")
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(190))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_reason: Mapped[str | None] = mapped_column(String(255))
    price_quoted: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    note: Mapped[str | None] = mapped_column(Text)

    doctor: Mapped[Doctor] = relationship(lazy="joined")
    service: Mapped[Service] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(f"status IN {VISIT_STATUSES}", name="status_valid"),
        CheckConstraint(f"source IN {VISIT_SOURCES}", name="source_valid"),
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_visits_idempotency"),
        ExcludeConstraint(
            ("doctor_id", "="),
            ("period", "&&"),
            using="gist",
            where=(f"status IN {BLOCKING_STATUSES}"),
            name="ex_visits_doctor_overlap",
        ),
        Index("ix_visits_tenant_status_start", "tenant_id", "status", "starts_at"),
    )


class Waitlist(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "waitlist"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    doctor_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    part_of_day: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("date_to >= date_from", name="date_range_sane"),
        CheckConstraint(
            "status IN ('open', 'offered', 'booked', 'closed')", name="status_valid"
        ),
    )


class TreatmentPlan(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "treatment_plans"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    #: ``[{"service_id": ..., "status": "planned|done", "due_date": "..."}]``
    steps: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class Review(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "reviews"

    visit_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("visits.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    score: Mapped[int | None] = mapped_column(SmallInteger)
    text: Mapped[str | None] = mapped_column(Text)
    published_link_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint("score IS NULL OR score BETWEEN 1 AND 5", name="score_range"),
        UniqueConstraint("visit_id", name="uq_reviews_visit"),
    )


__all__ = [
    "BLOCKING_STATUSES",
    "VISIT_STATUSES",
    "Doctor",
    "DoctorService",
    "Review",
    "Room",
    "Schedule",
    "ScheduleException",
    "Service",
    "TreatmentPlan",
    "Visit",
    "Waitlist",
]
