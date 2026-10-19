"""Declarative base, shared column types and mixins."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class JSONColumn(TypeDecorator):
    """JSONB on Postgres, plain JSON elsewhere (keeps SQLite tests working)."""

    impl = JSONB
    cache_ok = True

    def load_dialect_impl(self, dialect):  # pragma: no cover - dialect plumbing
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        from sqlalchemy import JSON

        return dialect.type_descriptor(JSON())


class GUID(TypeDecorator):
    """UUID on Postgres, 36-char string elsewhere."""

    impl = UUID
    cache_ok = True

    def load_dialect_impl(self, dialect):  # pragma: no cover - dialect plumbing
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        from sqlalchemy import String

        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):  # pragma: no cover
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):  # pragma: no cover
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict: JSONColumn, uuid.UUID: GUID}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class PKMixin:
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


#: Tables that carry ``tenant_id`` and therefore must never be queried without a
#: tenant filter.  ``arix.core.tenancy`` enforces this at runtime.
TENANT_SCOPED_TABLES: set[str] = set()


class TenantMixin:
    """Adds ``tenant_id`` and registers the table for tenant-scope enforcement."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), nullable=False, index=True
    )

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        table = getattr(cls, "__tablename__", None)
        if table:
            TENANT_SCOPED_TABLES.add(table)


__all__ = [
    "Base",
    "GUID",
    "JSONColumn",
    "PKMixin",
    "TENANT_SCOPED_TABLES",
    "TenantMixin",
    "TimestampMixin",
    "new_uuid",
    "utcnow",
]
