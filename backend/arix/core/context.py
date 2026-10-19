"""TenantContext — the object every service takes instead of loose arguments."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from arix.db.models_core import Tenant, TenantConfig

from .tenant_config import TenantSettings
from .timeutil import to_tenant


@dataclass
class TenantContext:
    tenant: Tenant
    settings: TenantSettings
    config_version: int = 1
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def tz_name(self) -> str:
        return self.tenant.timezone

    @property
    def languages(self) -> list[str]:
        return list(self.tenant.languages or ["ru"])

    @property
    def default_lang(self) -> str:
        return self.languages[0] if self.languages else "ru"

    @property
    def currency(self) -> str:
        return self.settings.currency

    @property
    def name(self) -> str:
        return self.settings.display_name or self.tenant.name

    def local(self, moment: datetime) -> datetime:
        return to_tenant(moment, self.tz_name)

    def lang_for(self, requested: str | None) -> str:
        """Clamp a detected language to what this tenant actually supports."""
        if requested and requested in self.languages:
            return requested
        return self.default_lang


async def load_context(session: AsyncSession, tenant_id: uuid.UUID) -> TenantContext:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    row = (
        await session.execute(
            select(TenantConfig)
            .where(TenantConfig.tenant_id == tenant_id, TenantConfig.published_at.isnot(None))
            .order_by(TenantConfig.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    settings = TenantSettings.from_data(row.data if row else {})
    return TenantContext(tenant=tenant, settings=settings, config_version=row.version if row else 0)


async def load_context_by_slug(session: AsyncSession, slug: str) -> TenantContext | None:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if tenant is None:
        return None
    return await load_context(session, tenant.id)


__all__ = ["TenantContext", "load_context", "load_context_by_slug"]
