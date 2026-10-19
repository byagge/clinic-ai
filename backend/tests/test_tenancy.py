"""Multi-tenant isolation: application scoping and Postgres row level security."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text

from arix.core.context import load_context
from arix.db.models_core import Contact
from arix.db.session import admin_session, tenant_session
from arix.seed.provision import create_tenant

from .conftest import requires_db

pytestmark = requires_db


@pytest.fixture
async def two_tenants(clean_db):
    async with admin_session() as session:
        first = await create_tenant(
            session, slug=f"iso-a-{uuid.uuid4().hex[:6]}", name="Клиника А",
            template_code="clinic_dental",
        )
        second = await create_tenant(
            session, slug=f"iso-b-{uuid.uuid4().hex[:6]}", name="Клиника Б",
            template_code="clinic_cosmetology",
        )
        for tenant_id, phone in ((first.tenant.id, "+77010000101"), (second.tenant.id, "+77010000102")):
            session.add(
                Contact(tenant_id=tenant_id, phone=phone, name="П", lang="ru", source="admin")
            )
        await session.flush()
    return first.tenant.id, second.tenant.id


async def test_tenant_scoped_queries_return_only_their_own_rows(two_tenants):
    """The application-level filter.

    Tests connect as the database owner, which bypasses RLS by design, so this
    asserts the query-side contract; ``test_rls_blocks_cross_tenant_reads``
    proves the database-side rule with a non-superuser role.
    """
    first, second = two_tenants
    async with tenant_session(first) as session:
        mine = list(
            (await session.execute(select(Contact).where(Contact.tenant_id == first))).scalars()
        )
        theirs = list(
            (await session.execute(select(Contact).where(Contact.tenant_id == second))).scalars()
        )
    assert len(mine) == 1 and mine[0].phone == "+77010000101"
    assert len(theirs) == 1 and theirs[0].phone == "+77010000102"


async def test_templates_are_isolated_per_tenant(two_tenants):
    first, second = two_tenants
    from arix.verticals.clinic.models import Service

    async with tenant_session(first) as session:
        codes_a = {
            row.code
            for row in (
                await session.execute(select(Service).where(Service.tenant_id == first))
            ).scalars()
        }
    async with tenant_session(second) as session:
        codes_b = {
            row.code
            for row in (
                await session.execute(select(Service).where(Service.tenant_id == second))
            ).scalars()
        }
    assert "hygiene" in codes_a
    assert "botox" in codes_b
    assert codes_a != codes_b


async def test_admin_session_requires_explicit_bypass():
    with pytest.raises(ValueError):
        async with tenant_session(None):
            pass


async def test_rls_blocks_cross_tenant_reads(two_tenants):
    """Prove the database itself refuses cross-tenant access.

    Migrations and tests run as the owner/superuser, which bypasses RLS, so the
    check uses a dedicated non-superuser role — the same configuration
    ``scripts/init-db.sql`` creates for production.
    """
    first, second = two_tenants
    role = f"rls_probe_{uuid.uuid4().hex[:8]}"
    async with admin_session() as session:
        await session.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'probe\''))
        await session.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"')
        )
        await session.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))

    import asyncpg

    url = os.environ["DATABASE_URL"]
    host_part = url.split("@", 1)[1]
    host, _, dbname = host_part.partition("/")
    hostname, _, port = host.partition(":")
    connection = await asyncpg.connect(
        user=role, password="probe", host=hostname, port=int(port or 5432), database=dbname
    )
    try:
        await connection.execute("SELECT set_config('arix.tenant_id', $1, false)", str(first))
        rows = await connection.fetch("SELECT tenant_id FROM contacts")
        assert rows, "the tenant must see its own rows"
        assert {row["tenant_id"] for row in rows} == {first}

        await connection.execute("SELECT set_config('arix.tenant_id', $1, false)", str(second))
        rows = await connection.fetch("SELECT tenant_id FROM contacts")
        assert {row["tenant_id"] for row in rows} == {second}

        # No tenant set at all → nothing is visible.
        await connection.execute("SELECT set_config('arix.tenant_id', '', false)")
        assert await connection.fetch("SELECT tenant_id FROM contacts") == []

        # Writing another tenant's row is refused by the WITH CHECK clause.
        await connection.execute("SELECT set_config('arix.tenant_id', $1, false)", str(first))
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await connection.execute(
                "INSERT INTO contacts (id, tenant_id, lang, source, tags, attrs, created_at,"
                " updated_at) VALUES ($1, $2, 'ru', 'admin', '{}', '{}', now(), now())",
                uuid.uuid4(), second,
            )
    finally:
        await connection.close()
        async with admin_session() as session:
            await session.execute(
                text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"')
            )
            await session.execute(text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"'))
            await session.execute(text(f'DROP ROLE "{role}"'))


async def test_tenant_context_exposes_config(two_tenants):
    first, _ = two_tenants
    async with tenant_session(first) as session:
        ctx = await load_context(session, first)
    assert ctx.settings.quiet is not None
    assert ctx.settings.features.followup is True
    assert ctx.default_lang == "ru"
