"""Reviews (ТЗ §7 С7): ask, promote 5★, escalate 1–3★."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.channels.base import Button
from arix.core.context import TenantContext
from arix.core.events import REVIEW_RECEIVED, log_event
from arix.db.models_core import Contact, Conversation
from arix.domain.messaging import send_message
from arix.domain.notify import notify_staff
from arix.templates import render

from .models import Review, Visit


def review_buttons(visit_id: uuid.UUID) -> list[Button]:
    return [Button("★" * score, f"review:{visit_id}:{score}") for score in range(1, 6)]


async def request_review(
    session: AsyncSession,
    ctx: TenantContext,
    visit: Visit,
    conversation: Conversation,
    contact: Contact,
) -> bool:
    text = render("review_request", contact.lang, overrides=ctx.settings.texts)
    outcome = await send_message(
        session, ctx, conversation, text,
        buttons=review_buttons(visit.id), contact=contact,
    )
    return outcome.ok


async def record_review(
    session: AsyncSession,
    ctx: TenantContext,
    visit_id: uuid.UUID,
    score: int,
    conversation: Conversation,
    *,
    text: str | None = None,
) -> Review:
    score = max(1, min(5, int(score)))
    review = (
        await session.execute(
            select(Review).where(Review.tenant_id == ctx.id, Review.visit_id == visit_id)
        )
    ).scalar_one_or_none()
    if review is None:
        review = Review(tenant_id=ctx.id, visit_id=visit_id, contact_id=conversation.contact_id)
        session.add(review)
    review.score = score
    if text:
        review.text = text
    await session.flush()

    contact = (
        await session.execute(select(Contact).where(Contact.id == conversation.contact_id))
    ).scalar_one()
    settings = ctx.settings

    if score >= settings.reviews.promote_from_score:
        link = settings.reviews.links.get("2gis") or settings.reviews.links.get("google")
        if link:
            await send_message(
                session, ctx, conversation,
                render("review_promote", contact.lang, overrides=settings.texts, link=link),
                contact=contact,
            )
            review.published_link_sent = True
        else:
            await send_message(
                session, ctx, conversation, "Спасибо за оценку!", contact=contact
            )
    elif score <= settings.reviews.alert_up_to_score:
        await send_message(
            session, ctx, conversation,
            render("review_apology", contact.lang, overrides=settings.texts),
            contact=contact,
        )
        await notify_staff(
            session, ctx, roles=["owner", "admin"],
            text=(
                f"⚠️ Низкая оценка {score}★\n"
                f"{contact.name or 'без имени'}"
                + (f" · {contact.phone}" if contact.phone else "")
                + (f"\n«{text}»" if text else "")
            ),
        )
    else:
        await send_message(
            session, ctx, conversation, "Спасибо, учтём!", contact=contact
        )

    await log_event(
        session, ctx.id, REVIEW_RECEIVED,
        contact_id=contact.id, conversation_id=conversation.id,
        entity_ref=str(visit_id), data={"score": score},
    )
    await session.flush()
    return review


__all__ = ["record_review", "request_review", "review_buttons"]
