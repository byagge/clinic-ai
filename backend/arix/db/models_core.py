"""Core data model — shared by every vertical (clinic, edu, ...)."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import GUID, Base, JSONColumn, PKMixin, TenantMixin, TimestampMixin

# ---------------------------------------------------------------------------
# Enumerated values are kept as CHECK-constrained strings rather than PG enums:
# adding a value must never require a migration lock on a live tenant table.
# ---------------------------------------------------------------------------

VERTICALS = ("clinic", "edu")
TENANT_STATUSES = ("demo", "pilot", "active", "paused", "archived")
CHANNEL_TYPES = ("wa", "tg", "tg_business", "ig", "miniapp", "web")
CHANNEL_STATUSES = ("pending", "connecting", "connected", "error", "disabled")
CONVERSATION_STATES = ("new", "active", "awaiting_human", "with_human", "closed")
MESSAGE_DIRECTIONS = ("in", "out")
MESSAGE_TYPES = ("text", "voice", "image", "document", "video", "location", "system", "button")
JOB_STATUSES = ("pending", "running", "done", "cancelled", "failed", "dead")
HANDOFF_PRIORITIES = ("normal", "high", "urgent")
STAFF_ROLES = ("owner", "admin", "doctor", "teacher", "superadmin")
PAYMENT_STATUSES = ("created", "pending", "paid", "failed", "refunded", "expired")
ATTRIBUTION_SOURCES = (
    "organic",
    "bot",
    "bot_night",
    "miniapp",
    "followup",
    "reactivation",
    "recall",
    "trial_followup",
    "referral",
    "admin",
)


class Tenant(PKMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vertical: Mapped[str] = mapped_column(String(16), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="KZ")
    city: Mapped[str | None] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Almaty")
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(String(8)), nullable=False, default=lambda: ["ru"]
    )
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="pilot")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="demo")
    data_region: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    template: Mapped[str | None] = mapped_column(String(64))
    # Demo tenants self-destruct; see scheduler handler ``demo_expire``.
    demo_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_ref: Mapped[dict | None] = mapped_column(JSONColumn)

    __table_args__ = (
        CheckConstraint(f"vertical IN {VERTICALS}", name="vertical_valid"),
        CheckConstraint(f"status IN {TENANT_STATUSES}", name="status_valid"),
        Index("ix_tenants_status", "status"),
    )


class TenantConfig(PKMixin, TimestampMixin, TenantMixin, Base):
    """Versioned tenant configuration. Exactly one row may be ``published``."""

    __tablename__ = "tenant_config"

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (UniqueConstraint("tenant_id", "version", name="uq_tenant_config_version"),)


class Channel(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "channels"

    type: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(128))
    #: Encrypted at rest with ``arix.core.crypto`` (Fernet).
    credentials_enc: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(f"type IN {CHANNEL_TYPES}", name="type_valid"),
        CheckConstraint(f"status IN {CHANNEL_STATUSES}", name="status_valid"),
        UniqueConstraint("tenant_id", "type", "external_id", name="uq_channels_identity"),
    )


class Contact(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "contacts"

    phone: Mapped[str | None] = mapped_column(String(24), index=True)
    name: Mapped[str | None] = mapped_column(String(160))
    lang: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="organic")
    consent_pd_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_marketing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False, default=list)
    last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attrs: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("tenant_id", "phone", name="uq_contacts_tenant_phone"),
    )

    @property
    def marketing_allowed(self) -> bool:
        return self.consent_marketing_at is not None and self.opted_out_at is None


class ContactIdentity(PKMixin, TimestampMixin, TenantMixin, Base):
    """Maps a channel-specific user id (tg user id, wa jid) to a contact."""

    __tablename__ = "contact_identities"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_type", "external_id", name="uq_contact_identity_external"
        ),
    )


class Conversation(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "conversations"

    contact_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("channels.id", ondelete="SET NULL")
    )
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False, default="tg")
    #: Where to deliver outbound messages on this channel (chat id / jid).
    peer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Telegram Business connection this dialogue belongs to, when applicable.
    business_connection_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="new")
    assigned_to: Mapped[str] = mapped_column(String(64), nullable=False, default="bot")
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 24h service window for WhatsApp Cloud API tenants.
    window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Rolling LLM-generated summary of older turns, keeps prompts small.
    summary: Mapped[str | None] = mapped_column(Text)
    scratch: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)

    contact: Mapped[Contact] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(f"state IN {CONVERSATION_STATES}", name="state_valid"),
        UniqueConstraint("tenant_id", "channel_type", "peer_id", name="uq_conversation_peer"),
    )


class Message(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "messages"

    #: Monotonic order within the deployment.  ``created_at`` defaults to the
    #: transaction timestamp, so an inbound message and the reply written in
    #: the same transaction share it; ordering history by time alone could then
    #: interleave turns and make two patient messages look like one.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(4), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    text: Mapped[str | None] = mapped_column(Text)
    stt_text: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    #: Provider message id. Unique per tenant → replayed webhooks are no-ops.
    external_id: Mapped[str | None] = mapped_column(String(190))
    sender: Mapped[str] = mapped_column(String(32), nullable=False, default="contact")
    meta: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(f"direction IN {MESSAGE_DIRECTIONS}", name="direction_valid"),
        UniqueConstraint("tenant_id", "external_id", name="uq_messages_external_id"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )


class KBItem(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "kb_items"

    type: Mapped[str] = mapped_column(String(24), nullable=False, default="faq")
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str] = mapped_column(String(8), nullable=False, default="ru")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False, default=list)
    #: Verbatim facts (refund policy, prices in text form) must not be
    #: paraphrased by the model — the agent is told to quote them as-is.
    verbatim: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (Index("ix_kb_items_tenant_lang", "tenant_id", "lang"),)


class ScheduledJob(PKMixin, TimestampMixin, TenantMixin, Base):
    """Durable timer. Nothing in ARIX ever uses ``sleep``."""

    __tablename__ = "scheduled_jobs"

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(190), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"status IN {JOB_STATUSES}", name="status_valid"),
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_scheduled_jobs_dedupe"),
        Index("ix_scheduled_jobs_due", "status", "run_at"),
    )


class Sequence(PKMixin, TimestampMixin, TenantMixin, Base):
    """Configurable multi-touch sequence (follow-up, recall, payment ladder)."""

    __tablename__ = "sequences"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    steps: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_sequences_code"),)


class Handoff(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "handoffs"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Human readable sequence number shown to admins ("передача #42").
    number: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    summary: Mapped[str | None] = mapped_column(Text)
    taken_by: Mapped[uuid.UUID | None] = mapped_column(GUID())
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Night-time handoffs are queued and delivered with the morning report.
    queued_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"priority IN {HANDOFF_PRIORITIES}", name="priority_valid"),
        Index("ix_handoffs_open", "tenant_id", "resolved_at"),
    )


class Event(PKMixin, TenantMixin, Base):
    """Append-only analytics log. Every meaningful action writes one row."""

    __tablename__ = "events"

    type: Mapped[str] = mapped_column(String(64), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(GUID())
    entity_ref: Mapped[str | None] = mapped_column(String(190))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="organic")
    data: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (Index("ix_events_tenant_type_created", "tenant_id", "type", "created_at"),)


class Payment(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "payments"

    contact_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), index=True)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="KZT")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="created")
    external_id: Mapped[str | None] = mapped_column(String(190))
    #: Revenue attribution for the revenue-share report (§10).
    attributed_to: Mapped[str] = mapped_column(String(32), nullable=False, default="organic")
    entity_ref: Mapped[str | None] = mapped_column(String(190))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(f"status IN {PAYMENT_STATUSES}", name="status_valid"),
        UniqueConstraint("tenant_id", "provider", "external_id", name="uq_payments_external"),
    )


class Staff(PKMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "staff"

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    phone: Mapped[str | None] = mapped_column(String(24))
    email: Mapped[str | None] = mapped_column(String(190))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Links a doctor/teacher staff row to its domain entity.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID())

    __table_args__ = (
        CheckConstraint(f"role IN {STAFF_ROLES}", name="role_valid"),
        UniqueConstraint("tenant_id", "tg_user_id", name="uq_staff_tg_user"),
    )


class OutboundCounter(PKMixin, TenantMixin, Base):
    """Monthly outbound counter per channel — WhatsApp free-tier alerting."""

    __tablename__ = "outbound_counters"

    channel_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    billable: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "channel_id", "period", name="uq_outbound_counter_period"),
    )


class AuditLog(PKMixin, TenantMixin, Base):
    """Access journal required for KZ personal-data compliance (§13)."""

    __tablename__ = "audit_log"

    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ChannelRoute(PKMixin, TimestampMixin, Base):
    """Routes a shared-bot user to the tenant they are talking to.

    The shared ARIX Telegram bot serves every demo and every tenant without
    its own bot, so one webhook receives traffic for many tenants.  A
    ``/start t_<slug>`` deeplink writes this row; subsequent messages from
    that Telegram user are routed to the same tenant until they open another
    deeplink.  It is deliberately *not* tenant-scoped — it is the router.
    """

    __tablename__ = "channel_routes"

    channel_type: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("channel_type", "external_id", name="uq_channel_routes_identity"),
    )


class DailyMetric(PKMixin, TenantMixin, Base):
    """Pre-aggregated daily counters for the owner report and panel charts."""

    __tablename__ = "daily_metrics"

    day: Mapped[date] = mapped_column(Date, nullable=False)
    data: Mapped[dict] = mapped_column(JSONColumn, nullable=False, default=dict)

    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_daily_metrics_day"),)


__all__ = [
    "ATTRIBUTION_SOURCES",
    "AuditLog",
    "Channel",
    "ChannelRoute",
    "Contact",
    "ContactIdentity",
    "Conversation",
    "DailyMetric",
    "Event",
    "Handoff",
    "KBItem",
    "Message",
    "OutboundCounter",
    "Payment",
    "ScheduledJob",
    "Sequence",
    "Staff",
    "Tenant",
    "TenantConfig",
]
