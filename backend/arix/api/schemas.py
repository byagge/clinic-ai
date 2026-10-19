"""Request/response models for the admin and Mini App APIs."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field


class TenantCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str
    vertical: str = "clinic"
    country: str = "KZ"
    city: str | None = None
    timezone: str = "Asia/Almaty"
    languages: list[str] = Field(default_factory=lambda: ["ru"])
    template: str | None = "clinic_dental"
    status: str = "pilot"
    plan: str = "pilot"


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    vertical: str
    country: str
    city: str | None
    timezone: str
    languages: list[str]
    status: str
    plan: str
    template: str | None
    demo_expires_at: datetime | None


class ConfigPatch(BaseModel):
    data: dict
    note: str | None = None
    publish: bool = True


class StaffCreate(BaseModel):
    role: str
    name: str
    tg_user_id: int | None = None
    phone: str | None = None
    email: str | None = None
    password: str | None = None
    entity_id: uuid.UUID | None = None


class ChannelConnectTelegram(BaseModel):
    bot_token: str | None = None
    #: Also used for staff bots when the tenant runs its own bot.
    staff_bot_token: str | None = None
    business: bool = False
    set_webhook: bool = True


class ChannelConnectWhatsApp(BaseModel):
    mode: str = "baileys"
    phone_number_id: str | None = None
    access_token: str | None = None
    app_secret: str | None = None
    verify_token: str | None = None


class ServiceIn(BaseModel):
    code: str
    name: dict[str, str]
    category: str | None = None
    price_from: float | None = None
    price_to: float | None = None
    currency: str = "KZT"
    duration_min: int = 30
    buffer_min: int = 0
    recall_days: int | None = None
    needs_consultation_first: bool = False
    online_bookable: bool = True
    complaint_keywords: list[str] = Field(default_factory=list)
    active: bool = True
    sort: int = 100


class ServiceOut(ServiceIn):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID


class DoctorIn(BaseModel):
    name: str
    specialty: str | None = None
    langs: list[str] = Field(default_factory=list)
    bio_short: str | None = None
    tg_user_id: int | None = None
    active: bool = True
    service_ids: list[uuid.UUID] = Field(default_factory=list)


class DoctorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    specialty: str | None
    langs: list[str]
    bio_short: str | None
    active: bool


class ScheduleIn(BaseModel):
    doctor_id: uuid.UUID
    weekday: int = Field(ge=0, le=6)
    start: time
    end: time
    break_start: time | None = None
    break_end: time | None = None


class ScheduleExceptionIn(BaseModel):
    doctor_id: uuid.UUID
    date: date
    type: str = "off"
    start: time | None = None
    end: time | None = None
    note: str | None = None


class KBItemIn(BaseModel):
    type: str = "faq"
    question: str
    answer: str
    lang: str = "ru"
    tags: list[str] = Field(default_factory=list)
    verbatim: bool = False
    active: bool = True


class SlotQuery(BaseModel):
    service_id: uuid.UUID
    doctor_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    part_of_day: str | None = None
    count: int = 6


class SlotOut(BaseModel):
    slot_key: str
    start: datetime
    end: datetime
    doctor_id: uuid.UUID
    doctor: str
    service_id: uuid.UUID
    service: str
    time: str
    date: str


class ManualBooking(BaseModel):
    """Phone bookings MUST land here, otherwise the bot will double-book."""

    service_id: uuid.UUID
    doctor_id: uuid.UUID
    start: datetime
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_id: uuid.UUID | None = None
    note: str | None = None
    status: str = "booked"


class VisitOut(BaseModel):
    id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    status: str
    source: str
    attribution: str
    doctor_id: uuid.UUID
    doctor: str
    service_id: uuid.UUID
    service: str
    contact_id: uuid.UUID
    contact_name: str | None
    contact_phone: str | None


class VisitStatusPatch(BaseModel):
    status: str
    reason: str | None = None


class PriceQuickPatch(BaseModel):
    service_code: str
    price_from: float | None = None
    price_to: float | None = None


class DemoRequest(BaseModel):
    """2GIS-card-shaped input for the outreach demo generator (ТЗ §12)."""

    name: str
    address: str | None = None
    city: str | None = None
    phone: str | None = None
    rubrics: list[str] = Field(default_factory=list)
    working_hours: str | None = None
    link: str | None = None
    country: str = "KZ"
    timezone: str | None = None
    languages: list[str] | None = None


class DemoResponse(BaseModel):
    tenant_id: uuid.UUID
    slug: str
    template: str
    telegram_link: str | None
    expires_at: datetime | None
    seeded: dict


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    tenant_id: uuid.UUID | None


__all__ = [name for name in dir() if name[0].isupper()]
