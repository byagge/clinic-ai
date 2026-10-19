"""API assembly."""
from __future__ import annotations

from fastapi import APIRouter

from . import auth, health, miniapp, panel, tenants, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(webhooks.router, prefix="/webhooks")
api_router.include_router(tenants.router)
api_router.include_router(panel.router)
api_router.include_router(miniapp.router)

__all__ = ["api_router"]
