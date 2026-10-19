"""Event log — the single source of truth for reports and attribution."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from arix.db.models_core import AuditLog, Event

from .timeutil import now_utc

# Canonical event types. Reports read these; nothing else should invent names.
INBOUND = "message_in"
OUTBOUND = "message_out"
FIRST_RESPONSE = "first_response"
INTEREST = "interest"
HANDOFF_CREATED = "handoff_created"
HANDOFF_TAKEN = "handoff_taken"
BOOKING_CREATED = "booking_created"
BOOKING_CONFIRMED = "booking_confirmed"
BOOKING_CANCELLED = "booking_cancelled"
BOOKING_RESCHEDULED = "booking_rescheduled"
VISIT_DONE = "visit_done"
VISIT_NO_SHOW = "visit_no_show"
REVIEW_RECEIVED = "review_received"
RECALL_SENT = "recall_sent"
FOLLOWUP_SENT = "followup_sent"
PAYMENT_PAID = "payment_paid"
OPT_OUT = "opt_out"
RED_FLAG = "red_flag"
TOOL_CALL = "tool_call"
LLM_USAGE = "llm_usage"
DELIVERY_FAILED = "delivery_failed"
# edu vertical
TRIAL_BOOKED = "trial_booked"
TRIAL_ATTENDED = "trial_attended"
TRIAL_NO_SHOW = "trial_no_show"
ENROLLED = "enrolled"
INVOICE_CREATED = "invoice_created"
INVOICE_PAID = "invoice_paid"
ABSENCE = "absence"
CHURN_RISK = "churn_risk"
LEVEL_TEST_DONE = "level_test_done"


async def log_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    type: str,
    *,
    contact_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    entity_ref: str | None = None,
    amount: Decimal | float | None = None,
    currency: str | None = None,
    source: str = "organic",
    data: dict | None = None,
) -> Event:
    event = Event(
        tenant_id=tenant_id,
        type=type,
        contact_id=contact_id,
        conversation_id=conversation_id,
        entity_ref=str(entity_ref) if entity_ref else None,
        amount=Decimal(str(amount)) if amount is not None else None,
        currency=currency,
        source=source,
        data=data or {},
        created_at=now_utc(),
    )
    session.add(event)
    return event


async def audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor: str,
    action: str,
    *,
    entity: str | None = None,
    entity_id: str | None = None,
    ip: str | None = None,
    data: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id else None,
            ip=ip,
            data=data or {},
            created_at=now_utc(),
        )
    )


__all__ = [name for name in dir() if name.isupper()] + ["audit", "log_event"]
