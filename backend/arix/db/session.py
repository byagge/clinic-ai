"""Async engine/session factory with per-transaction tenant scoping.

Tenant isolation is enforced twice:

1. **Postgres row level security.** Every tenant-scoped table has a policy on
   ``current_setting('arix.tenant_id')``.  ``tenant_session`` sets that GUC for
   the lifetime of the transaction, so a forgotten ``WHERE tenant_id = ...``
   returns zero rows instead of another clinic's data.
2. **Application guard.** ``arix.core.tenancy.TenantScope`` adds the filter for
   you and raises in debug builds if a tenant table is queried unscoped.

The database role used by the application must *not* be a superuser, otherwise
Postgres skips RLS.  ``scripts/init-db.sql`` creates ``arix_app`` for this.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from arix.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        s = get_settings()
        kwargs: dict = {"echo": False, "pool_pre_ping": True, "future": True}
        if not s.database_url.startswith("sqlite"):
            kwargs.update(pool_size=s.db_pool_size, max_overflow=s.db_max_overflow)
        _engine = create_async_engine(s.database_url, **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False, class_=AsyncSession
        )
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def _apply_guc(session: AsyncSession, tenant_id: uuid.UUID | None, bypass: bool) -> None:
    if session.bind.dialect.name != "postgresql":  # pragma: no cover - sqlite tests
        return
    # set_config(..., true) = transaction-local, reverted on commit/rollback.
    await session.execute(
        text("SELECT set_config('arix.tenant_id', :tid, true)"),
        {"tid": str(tenant_id) if tenant_id else ""},
    )
    await session.execute(
        text("SELECT set_config('arix.bypass_rls', :flag, true)"),
        {"flag": "on" if bypass else "off"},
    )


@asynccontextmanager
async def tenant_session(
    tenant_id: uuid.UUID | None = None, *, bypass_rls: bool = False
) -> AsyncIterator[AsyncSession]:
    """Open a transaction scoped to one tenant.

    ``bypass_rls=True`` is reserved for ARIX-level operations (tenant creation,
    cross-tenant superadmin views, migrations) and is audited by the caller.
    """
    if tenant_id is None and not bypass_rls:
        raise ValueError("tenant_session requires tenant_id or explicit bypass_rls=True")
    async with get_sessionmaker()() as session:
        await _apply_guc(session, tenant_id, bypass_rls)
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    """Cross-tenant session for the ARIX superadmin surface."""
    async with tenant_session(None, bypass_rls=True) as session:
        yield session


__all__ = [
    "admin_session",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "tenant_session",
]
