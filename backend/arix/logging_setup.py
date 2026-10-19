"""Structured logging.

Every log line carries ``tenant_id``/``conversation_id`` when they are known so
that a single patient dialogue can be reconstructed from the log stream.
"""
from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

import structlog

#: Default is ``None``, not ``{}``: a mutable default on a ContextVar is shared
#: between contexts and would leak one request's fields into another.
request_context: ContextVar[dict | None] = ContextVar("request_context", default=None)


def bind_context(**kwargs: object) -> None:
    current = dict(request_context.get() or {})
    current.update({k: v for k, v in kwargs.items() if v is not None})
    request_context.set(current)


def clear_context() -> None:
    request_context.set(None)


def _inject_context(_logger, _name, event_dict):  # pragma: no cover - plumbing
    event_dict.update(request_context.get() or {})
    return event_dict


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO)
    )
    for noisy in ("httpx", "httpcore", "aiosqlite", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "arix") -> structlog.BoundLogger:
    return structlog.get_logger(name)
