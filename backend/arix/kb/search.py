"""Knowledge-base retrieval.

Hybrid lexical search over ``kb_items``: Postgres full-text (``russian``
configuration, which also handles KZ/UZ latin acceptably) combined with
trigram similarity so typos and inflections still match.  pgvector is used
when the extension is present *and* embeddings have been computed; otherwise
the lexical path alone answers, which keeps demos and CI free of any embedding
provider.

The agent never sees anything that is not returned from here, so recall matters
more than precision: we return up to ``limit`` items and let the model pick.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import Float, cast, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession

from arix.db.models_core import KBItem

STOPWORDS = {
    "и", "в", "на", "с", "по", "а", "но", "что", "как", "это", "для", "от", "до",
    "ли", "же", "бы", "у", "за", "из", "не", "ну", "вы", "мы", "я", "мне", "вас",
}


#: Below this combined score a hit is noise: answering with it would be worse
#: than saying "уточню у администратора".
MIN_RELEVANCE = 0.3


@dataclass(frozen=True)
class KBHit:
    id: uuid.UUID
    type: str
    question: str
    answer: str
    lang: str
    verbatim: bool
    score: float

    def as_fact(self) -> dict:
        return {
            "id": str(self.id),
            "type": self.type,
            "question": self.question,
            "answer": self.answer,
            "verbatim": self.verbatim,
            "score": round(self.score, 3),
        }


def tokenize(query: str) -> list[str]:
    words = re.findall(r"[\w\-]{3,}", (query or "").lower(), flags=re.UNICODE)
    return [w for w in words if w not in STOPWORDS][:12]


def to_tsquery(query: str) -> str:
    """Build a forgiving OR-of-prefixes tsquery: ``цена:* | имплант:*``."""
    tokens = tokenize(query)
    if not tokens:
        return ""
    return " | ".join(f"{t}:*" for t in tokens)


async def search_kb(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    *,
    lang: str | None = None,
    types: list[str] | None = None,
    limit: int = 5,
    min_relevance: float | None = None,
) -> list[KBHit]:
    tsq = to_tsquery(query)
    haystack = func.coalesce(KBItem.question, "") + literal(" ") + func.coalesce(KBItem.answer, "")

    conds = [KBItem.tenant_id == tenant_id, KBItem.active.is_(True)]
    if types:
        conds.append(KBItem.type.in_(types))
    # Language is a soft filter: a KZ patient still benefits from a RU fact if
    # that is all the clinic entered.
    if lang:
        conds.append(or_(KBItem.lang == lang, KBItem.lang == "ru"))

    if session.bind.dialect.name != "postgresql":  # pragma: no cover - sqlite tests
        rows = (await session.execute(select(KBItem).where(*conds).limit(200))).scalars().all()
        tokens = tokenize(query)
        scored = []
        for item in rows:
            blob = f"{item.question} {item.answer}".lower()
            score = sum(1 for t in tokens if t in blob) / max(len(tokens), 1)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda p: -p[0])
        floor = MIN_RELEVANCE if min_relevance is None else min_relevance
        return [_to_hit(item, value) for value, item in scored[:limit] if value >= floor]

    # ``to_tsvector`` needs a regconfig, not a varchar; the cast also makes the
    # expression match the functional index created in the migration.
    config = cast(literal("russian"), REGCONFIG)
    tsvector = func.to_tsvector(config, haystack)
    rank = literal(0.0, Float)
    if tsq:
        tsquery = func.to_tsquery(config, literal(tsq))
        rank = func.ts_rank(tsvector, tsquery)
    similarity = func.similarity(haystack, literal(query or ""))
    score = (rank * 2.0 + similarity).label("score")

    stmt = (
        select(KBItem, score)
        .where(*conds)
        .where(
            or_(
                similarity > 0.12,
                tsvector.op("@@")(func.to_tsquery(config, literal(tsq)))
                if tsq
                else text("false"),
            )
        )
        .order_by(score.desc(), KBItem.verbatim.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    floor = MIN_RELEVANCE if min_relevance is None else min_relevance
    return [
        _to_hit(item, float(score or 0))
        for item, score in rows
        if float(score or 0) >= floor
    ]


def _to_hit(item: KBItem, score: float) -> KBHit:
    return KBHit(
        id=item.id,
        type=item.type,
        question=item.question,
        answer=item.answer,
        lang=item.lang,
        verbatim=item.verbatim,
        score=score,
    )


__all__ = ["MIN_RELEVANCE", "KBHit", "search_kb", "to_tsquery", "tokenize"]
