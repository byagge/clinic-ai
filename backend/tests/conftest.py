"""Test fixtures.

Unit tests (slots, quiet hours, guardrails, PII, templates) need nothing.
Integration tests need Postgres with ``btree_gist`` and ``pg_trgm``; point
``TEST_DATABASE_URL`` at a scratch database and they run, otherwise they skip.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

TEST_DB = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/arix_test"
)

# Settings must be in place before any arix module reads them.
os.environ.setdefault("DATABASE_URL", TEST_DB)
os.environ.setdefault("LLM_FORCE_SCRIPTED", "true")
os.environ.setdefault("CHANNELS_DRY_RUN", "true")
os.environ.setdefault("PII_MASKING_ENABLED", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key-at-least-32-chars-long")
os.environ.setdefault("LOG_LEVEL", "WARNING")


def _db_available() -> bool:
    import asyncio

    async def probe() -> bool:
        from arix.db.session import dispose_engine

        try:
            from sqlalchemy import text

            from arix.db.session import get_engine

            async with get_engine().connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await dispose_engine()

    try:
        return asyncio.run(probe())
    except Exception:
        return False


DB_AVAILABLE = _db_available()

requires_db = pytest.mark.skipif(
    not DB_AVAILABLE, reason="TEST_DATABASE_URL is not reachable"
)


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """Dispose the engine after every test.

    The engine and its asyncpg pool are module-level, while pytest-asyncio
    gives each test its own event loop; a pooled connection must never outlive
    the loop it was created on.
    """
    yield
    if DB_AVAILABLE:
        from arix.db.session import dispose_engine

        await dispose_engine()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Apply migrations once per session."""
    if not DB_AVAILABLE:
        return
    import subprocess
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["python3", "-m", "alembic", "upgrade", "head"],
        cwd=backend,
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(backend)},
    )


@pytest.fixture
async def clean_db() -> AsyncIterator[None]:
    """Truncate tenant data between tests, keeping the schema."""
    from sqlalchemy import text

    from arix.db.session import get_engine

    tables = (
        "reviews, visits, waitlist, treatment_plans, schedule_exceptions, schedules, "
        "doctor_services, services, doctors, rooms, daily_metrics, audit_log, "
        "outbound_counters, payments, events, handoffs, sequences, scheduled_jobs, "
        "kb_items, messages, conversations, contact_identities, contacts, channels, "
        "channel_routes, staff, tenant_config, tenants"
    )
    async with get_engine().begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield
    from arix.channels.dryrun import DryRunAdapter

    DryRunAdapter.clear()


@pytest.fixture
async def tenant(clean_db):
    """A provisioned dental clinic with a dry-run Telegram channel and staff."""
    from arix.db.session import admin_session
    from arix.seed.provision import create_tenant, ensure_channel, ensure_staff

    slug = f"test-{uuid.uuid4().hex[:8]}"
    async with admin_session() as session:
        result = await create_tenant(
            session,
            slug=slug,
            name="Dental Smile",
            city="Шымкент",
            timezone="Asia/Almaty",
            languages=["ru", "kz"],
            template_code="clinic_dental",
            status="pilot",
        )
        tenant_id = result.tenant.id
        await ensure_channel(
            session, tenant_id, "tg",
            credentials={"bot_token": "TEST:TOKEN"},
            settings={"dry_run": True}, status="connected",
        )
        await ensure_staff(session, tenant_id, role="admin", name="Админ", tg_user_id=111)
        await ensure_staff(session, tenant_id, role="owner", name="Владелец", tg_user_id=222)
    return tenant_id


@pytest.fixture
async def ctx(tenant):
    from arix.core.context import load_context
    from arix.db.session import tenant_session

    async with tenant_session(tenant) as session:
        return await load_context(session, tenant)


@pytest.fixture
def runtime():
    from arix.verticals.clinic.adapter import build_runtime
    from arix.verticals.clinic.handlers import register

    register()
    return build_runtime()


@pytest.fixture
def inbound_factory():
    """Builds unique inbound messages so dedupe never masks a test."""
    from arix.channels.base import InboundMessage

    def make(
        text: str | None = None,
        *,
        peer: str = "tg-test-peer",
        callback_data: str | None = None,
        kind: str = "text",
        name: str | None = "Асель",
        from_business: bool = False,
        external_id: str | None = None,
    ) -> InboundMessage:
        return InboundMessage(
            channel_type="tg",
            peer_id=peer,
            external_id=external_id or f"test-{uuid.uuid4().hex}",
            text=text,
            type=kind,
            sender_name=name,
            callback_data=callback_data,
            from_user_id="900001",
            from_business=from_business,
        )

    return make
