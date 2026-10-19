"""Panel login for staff with e-mail + password."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from arix.db.models_core import Staff

from .deps import db_admin, issue_token
from .schemas import LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session=Depends(db_admin)) -> TokenResponse:
    from arix.core.passwords import verify_password

    member = (
        await session.execute(
            select(Staff).where(Staff.email == body.email, Staff.active.is_(True))
        )
    ).scalars().first()
    # ``verify_password`` always runs a bcrypt comparison, so a missing account
    # and a wrong password take the same time.
    valid = verify_password(body.password, member.password_hash if member else None)
    if not (member and valid):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    return TokenResponse(
        access_token=issue_token(
            tenant_id=member.tenant_id, staff_id=member.id, role=member.role
        ),
        role=member.role,
        tenant_id=member.tenant_id,
    )


__all__ = ["router"]
