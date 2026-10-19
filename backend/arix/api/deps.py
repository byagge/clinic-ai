"""FastAPI dependencies: auth, tenant resolution, sessions."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from arix.config import get_settings
from arix.core.context import TenantContext, load_context
from arix.core.timeutil import now_utc
from arix.db.session import admin_session, tenant_session

ALGORITHM = "HS256"


async def db_admin() -> AsyncIterator[AsyncSession]:
    """Cross-tenant session — only for ARIX superadmin endpoints."""
    async with admin_session() as session:
        yield session


def issue_token(*, tenant_id: uuid.UUID | None, staff_id: uuid.UUID, role: str) -> str:
    s = get_settings()
    payload = {
        "sub": str(staff_id),
        "tid": str(tenant_id) if tenant_id else None,
        "role": role,
        "exp": now_utc() + timedelta(minutes=s.jwt_ttl_minutes),
    }
    return jwt.encode(payload, s.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


class Principal:
    def __init__(self, staff_id: uuid.UUID | None, tenant_id: uuid.UUID | None, role: str):
        self.staff_id = staff_id
        self.tenant_id = tenant_id
        self.role = role

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_arix_admin_token: Annotated[str | None, Header()] = None,
) -> Principal:
    s = get_settings()
    if x_arix_admin_token and x_arix_admin_token == s.admin_api_token:
        return Principal(None, None, "superadmin")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authorization required")
    claims = decode_token(authorization.split(" ", 1)[1])
    tid = claims.get("tid")
    return Principal(
        uuid.UUID(claims["sub"]) if claims.get("sub") else None,
        uuid.UUID(tid) if tid else None,
        claims.get("role", "admin"),
    )


async def require_superadmin(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "superadmin only")
    return principal


class TenantSession:
    """A tenant-scoped session plus its loaded context."""

    def __init__(self, session: AsyncSession, ctx: TenantContext):
        self.session = session
        self.ctx = ctx


async def tenant_scope(
    tenant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
) -> AsyncIterator[TenantSession]:
    if not principal.is_superadmin and principal.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "wrong tenant")
    async with tenant_session(tenant_id, bypass_rls=principal.is_superadmin) as session:
        ctx = await load_context(session, tenant_id)
        yield TenantSession(session, ctx)


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


__all__ = [
    "Principal",
    "TenantSession",
    "client_ip",
    "current_principal",
    "db_admin",
    "decode_token",
    "issue_token",
    "require_superadmin",
    "tenant_scope",
]
