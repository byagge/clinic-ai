"""ARIX backend entrypoint."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from arix.api.router import api_router
from arix.config import get_settings
from arix.db.session import dispose_engine
from arix.logging_setup import bind_context, clear_context, get_logger, setup_logging

log = get_logger("arix.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    # Registering the vertical wires its scheduler handlers, so an in-process
    # call path (tests, admin actions) behaves exactly like the worker.
    from arix.scheduler.worker import _register_vertical

    _register_vertical()
    log.info(
        "startup",
        env=s.env,
        vertical=s.vertical,
        brain="claude" if s.llm_enabled else "scripted",
        wa_provider=s.wa_provider,
    )
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="ARIX — ИИ-менеджер клиники",
        version="1.0.0",
        description=(
            "Мультитенантный ИИ-менеджер: Telegram (включая Telegram Business), "
            "WhatsApp через Baileys, запись на приём, напоминания, дожим, отчёты."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if s.env in ("local", "demo") else [s.public_base_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.monotonic()
        bind_context(path=request.url.path, method=request.method)
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request_failed")
            clear_context()
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not request.url.path.startswith(("/health", "/metrics")):
            log.info("request", status=response.status_code, ms=elapsed_ms)
        clear_context()
        response.headers["X-Response-Time-ms"] = str(elapsed_ms)
        return response

    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "service": "arix",
                "vertical": s.vertical,
                "docs": "/docs",
                "health": "/health/ready",
            }
        )

    @app.get("/pay/{payment_id}", include_in_schema=False, response_class=PlainTextResponse)
    async def manual_payment_page(payment_id: str) -> str:
        """Fallback payment page used in demos and for manual confirmation."""
        return (
            "Оплата по счёту " + payment_id + "\n\n"
            "Это демо-страница. В продакшене здесь редирект в Kaspi / Click / Payme.\n"
            "Администратор может отметить оплату в панели."
        )

    return app


app = create_app()
