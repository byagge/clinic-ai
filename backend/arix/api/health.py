"""Liveness, readiness and Prometheus-ish metrics."""
from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import func, select, text

from arix.config import get_settings
from arix.db.models_core import Channel, Tenant
from arix.db.session import admin_session
from arix.scheduler.jobs import queue_health

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"ok": True, "env": s.env, "vertical": s.vertical, "llm": "claude" if s.llm_enabled else "scripted"}


@router.get("/health/ready")
async def ready() -> dict:
    """Readiness: DB reachable, migrations applied, queue not stalled."""
    out: dict = {"ok": True, "checks": {}}
    try:
        async with admin_session() as session:
            await session.execute(text("SELECT 1"))
            out["checks"]["db"] = "ok"
            version = (
                await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            ).scalar_one_or_none()
            out["checks"]["migration"] = version or "missing"
            if version is None:
                out["ok"] = False
            queue = await queue_health(session)
            out["checks"]["queue"] = queue
            if queue["oldest_pending_age_s"] > 600:
                out["ok"] = False
            tenants = (
                await session.execute(
                    select(func.count()).select_from(Tenant).where(Tenant.status != "archived")
                )
            ).scalar_one()
            out["checks"]["tenants"] = tenants
            broken = (
                await session.execute(
                    select(func.count()).select_from(Channel).where(Channel.status == "error")
                )
            ).scalar_one()
            out["checks"]["channels_error"] = broken
    except Exception as exc:
        out["ok"] = False
        out["checks"]["db"] = f"error: {exc!r}"[:200]
    return out


@router.get("/metrics")
async def metrics() -> Response:
    lines: list[str] = []
    try:
        async with admin_session() as session:
            queue = await queue_health(session)
            lines.append("# HELP arix_jobs_total Scheduled jobs by status")
            lines.append("# TYPE arix_jobs_total gauge")
            for status_name, count in queue["counts"].items():
                lines.append(f'arix_jobs_total{{status="{status_name}"}} {count}')
            lines.append("# HELP arix_queue_lag_seconds Age of the oldest due job")
            lines.append("# TYPE arix_queue_lag_seconds gauge")
            lines.append(f"arix_queue_lag_seconds {queue['oldest_pending_age_s']}")
            for label, column in (("tenants", Tenant.status), ("channels", Channel.status)):
                rows = (
                    await session.execute(select(column, func.count()).group_by(column))
                ).all()
                lines.append(f"# TYPE arix_{label} gauge")
                for value, count in rows:
                    lines.append(f'arix_{label}{{status="{value}"}} {count}')
    except Exception as exc:  # pragma: no cover
        lines.append(f"# error {exc!r}")
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


__all__ = ["router"]
