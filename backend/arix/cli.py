"""Operational CLI.

    python -m arix.cli seed-demo                  # a ready-to-show clinic
    python -m arix.cli create-tenant --slug x ...
    python -m arix.cli demo-from-2gis --name ... --rubrics стоматология
    python -m arix.cli connect-telegram --slug x --token ...
    python -m arix.cli wa-qr --slug x
    python -m arix.cli import-prices --slug x --file prices.xlsx
    python -m arix.cli report --slug x
    python -m arix.cli run-due-jobs                # drain the queue once
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import date

from arix.config import get_settings
from arix.core.context import load_context, load_context_by_slug
from arix.db.session import admin_session, dispose_engine, tenant_session
from arix.logging_setup import setup_logging

DEMO_SLUG = "demo-dental"


async def _slug_to_id(slug: str):
    async with admin_session() as session:
        ctx = await load_context_by_slug(session, slug)
    if ctx is None:
        raise SystemExit(f"тенант {slug!r} не найден")
    return ctx.id


async def cmd_seed_demo(args: argparse.Namespace) -> int:
    """Create a demo clinic with services, doctors, staff, visits and dialogues."""
    from arix.seed.provision import (
        create_tenant,
        ensure_channel,
        ensure_staff,
        publish_config,
        seed_demo_data,
    )

    slug = args.slug or DEMO_SLUG
    s = get_settings()
    async with admin_session() as session:
        result = await create_tenant(
            session,
            slug=slug,
            name=args.name,
            vertical=s.vertical,
            country=args.country,
            city=args.city,
            timezone=args.timezone,
            languages=args.languages.split(","),
            template_code=args.template,
            status="pilot",
        )
        tenant_id = result.tenant.id
        await publish_config(
            session, tenant_id,
            {
                "address": args.address,
                "phone": args.phone,
                "map_url": "https://2gis.kz/shymkent",
                "reviews": {"links": {"2gis": "https://2gis.kz/shymkent"}},
            },
            note="seed-demo",
        )
        channel_settings = {"dry_run": True} if not s.telegram_shared_bot_token else {}
        await ensure_channel(
            session, tenant_id, "tg",
            credentials={"bot_token": s.telegram_shared_bot_token or "DEMO:TOKEN"},
            settings=channel_settings,
            status="connected",
        )
        await ensure_staff(
            session, tenant_id, role="owner", name="Владелец",
            email=f"owner@{slug}.local", password=args.password,
            tg_user_id=args.owner_tg,
        )
        await ensure_staff(
            session, tenant_id, role="admin", name="Администратор",
            email=f"admin@{slug}.local", password=args.password,
            tg_user_id=args.admin_tg,
        )

    async with tenant_session(tenant_id) as session:
        ctx = await load_context(session, tenant_id)
        seeded = await seed_demo_data(session, ctx)

    print(json.dumps({
        "tenant_id": str(tenant_id),
        "slug": slug,
        "counts": result.counts | seeded,
        "panel_login": f"owner@{slug}.local",
        "panel_password": args.password,
        "next": [
            f"API: {s.public_base_url}/docs",
            f"Отчёт: python -m arix.cli report --slug {slug}",
            "Подключить Telegram: python -m arix.cli connect-telegram --slug "
            f"{slug} --token <BOT_TOKEN>",
            f"Подключить WhatsApp: python -m arix.cli wa-qr --slug {slug}",
        ],
    }, ensure_ascii=False, indent=2))
    return 0


async def cmd_create_tenant(args: argparse.Namespace) -> int:
    from arix.seed.provision import create_tenant

    async with admin_session() as session:
        result = await create_tenant(
            session,
            slug=args.slug, name=args.name, vertical=get_settings().vertical,
            country=args.country, city=args.city, timezone=args.timezone,
            languages=args.languages.split(","), template_code=args.template,
            status=args.status,
        )
    print(json.dumps(
        {"tenant_id": str(result.tenant.id), "slug": args.slug, "counts": result.counts},
        ensure_ascii=False, indent=2,
    ))
    return 0


async def cmd_demo_from_2gis(args: argparse.Namespace) -> int:
    """The outreach demo generator of ТЗ §12, from the command line."""
    from arix.api.deps import Principal
    from arix.api.schemas import DemoRequest
    from arix.api.tenants import generate_demo

    body = DemoRequest(
        name=args.name, address=args.address, city=args.city, phone=args.phone,
        rubrics=[r.strip() for r in (args.rubrics or "").split(",") if r.strip()],
        working_hours=args.working_hours, link=args.link, country=args.country,
    )
    async with admin_session() as session:
        response = await generate_demo(body, Principal(None, None, "superadmin"), session)
    print(json.dumps(response.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


async def cmd_connect_telegram(args: argparse.Namespace) -> int:
    from arix.api.deps import TenantSession
    from arix.api.schemas import ChannelConnectTelegram
    from arix.api.tenants import connect_telegram

    tenant_id = await _slug_to_id(args.slug)
    async with tenant_session(tenant_id) as session:
        ctx = await load_context(session, tenant_id)
        result = await connect_telegram(
            tenant_id,
            ChannelConnectTelegram(
                bot_token=args.token, business=args.business, set_webhook=not args.no_webhook
            ),
            TenantSession(session, ctx),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


async def cmd_wa_qr(args: argparse.Namespace) -> int:
    from arix.channels.whatsapp import BaileysAdapter
    from arix.seed.provision import ensure_channel

    tenant_id = await _slug_to_id(args.slug)
    async with tenant_session(tenant_id) as session:
        await ensure_channel(
            session, tenant_id, "wa",
            settings={"mode": "baileys", "session_id": args.slug}, status="connecting",
        )
    adapter = BaileysAdapter(args.slug)
    await adapter.start()
    state = await adapter.qr()
    if state.get("qr"):
        import base64

        path = f"/tmp/arix-wa-{args.slug}.png"
        payload = base64.b64decode(state["qr"].split(",", 1)[1])
        await asyncio.to_thread(pathlib.Path(path).write_bytes, payload)
        state["qr_png"] = path
        state["qr"] = "<data-url saved to qr_png>"
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


async def cmd_import_prices(args: argparse.Namespace) -> int:
    from arix.seed.imports import import_doctors_xlsx, import_prices_xlsx

    tenant_id = await _slug_to_id(args.slug)
    async with tenant_session(tenant_id) as session:
        ctx = await load_context(session, tenant_id)
        if args.kind == "prices":
            report = await import_prices_xlsx(session, ctx, args.file)
        else:
            report = await import_doctors_xlsx(session, ctx, args.file)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


async def cmd_report(args: argparse.Namespace) -> int:
    from arix.verticals.clinic.reports import build_daily_report, render_daily_report

    tenant_id = await _slug_to_id(args.slug)
    day = date.fromisoformat(args.day) if args.day else None
    async with tenant_session(tenant_id) as session:
        ctx = await load_context(session, tenant_id)
        report = await build_daily_report(session, ctx, day)
        print(render_daily_report(ctx, report))
    return 0


async def cmd_run_due_jobs(args: argparse.Namespace) -> int:
    """Drain the job queue once — useful for demos and for debugging."""
    from arix.scheduler.worker import Worker, _register_vertical

    _register_vertical()
    worker = Worker(worker_id="cli")
    processed = await worker.tick()
    print(json.dumps({"processed": processed}, ensure_ascii=False))
    return 0


async def cmd_arm_daily(args: argparse.Namespace) -> int:
    from arix.scheduler.worker import Worker, _register_vertical

    _register_vertical()
    await Worker(worker_id="cli").housekeeping()
    print("ежедневные задания поставлены")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arix", description="ARIX operations")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed-demo", help="создать демо-клинику")
    seed.add_argument("--slug", default=DEMO_SLUG)
    seed.add_argument("--name", default="Dental Smile")
    seed.add_argument("--city", default="Шымкент")
    seed.add_argument("--country", default="KZ")
    seed.add_argument("--timezone", default="Asia/Almaty")
    seed.add_argument("--languages", default="ru,kz")
    seed.add_argument("--template", default="clinic_dental")
    seed.add_argument("--address", default="ул. Абая 10, Шымкент")
    seed.add_argument("--phone", default="+7 7252 00 00 00")
    seed.add_argument("--password", default="demo12345")
    seed.add_argument("--owner-tg", type=int, default=None)
    seed.add_argument("--admin-tg", type=int, default=None)
    seed.set_defaults(func=cmd_seed_demo)

    create = sub.add_parser("create-tenant", help="создать тенант")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--city", default=None)
    create.add_argument("--country", default="KZ")
    create.add_argument("--timezone", default="Asia/Almaty")
    create.add_argument("--languages", default="ru")
    create.add_argument("--template", default="clinic_dental")
    create.add_argument("--status", default="pilot")
    create.set_defaults(func=cmd_create_tenant)

    demo = sub.add_parser("demo-from-2gis", help="демо по карточке 2ГИС (ТЗ §12)")
    demo.add_argument("--name", required=True)
    demo.add_argument("--address", default=None)
    demo.add_argument("--city", default=None)
    demo.add_argument("--phone", default=None)
    demo.add_argument("--rubrics", default="")
    demo.add_argument("--working-hours", default=None)
    demo.add_argument("--link", default=None)
    demo.add_argument("--country", default="KZ")
    demo.set_defaults(func=cmd_demo_from_2gis)

    telegram = sub.add_parser("connect-telegram", help="подключить Telegram-бота")
    telegram.add_argument("--slug", required=True)
    telegram.add_argument("--token", default=None)
    telegram.add_argument("--business", action="store_true", help="Telegram Business")
    telegram.add_argument("--no-webhook", action="store_true")
    telegram.set_defaults(func=cmd_connect_telegram)

    wa = sub.add_parser("wa-qr", help="QR для привязки WhatsApp (Baileys)")
    wa.add_argument("--slug", required=True)
    wa.set_defaults(func=cmd_wa_qr)

    imports = sub.add_parser("import-prices", help="импорт прайса или врачей из Excel")
    imports.add_argument("--slug", required=True)
    imports.add_argument("--file", required=True)
    imports.add_argument("--kind", choices=["prices", "doctors"], default="prices")
    imports.set_defaults(func=cmd_import_prices)

    report = sub.add_parser("report", help="утренний отчёт владельцу")
    report.add_argument("--slug", required=True)
    report.add_argument("--day", default=None, help="YYYY-MM-DD, по умолчанию вчера")
    report.set_defaults(func=cmd_report)

    jobs = sub.add_parser("run-due-jobs", help="выполнить задания, которым пришло время")
    jobs.set_defaults(func=cmd_run_due_jobs)

    arm = sub.add_parser("arm-daily", help="поставить ежедневные задания всем тенантам")
    arm.set_defaults(func=cmd_arm_daily)

    return parser


async def _run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    try:
        return await args.func(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
