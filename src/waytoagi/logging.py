"""Structured logging setup — structlog với JSON output + correlation IDs.

Mỗi pipeline run, mỗi stage, mỗi record có correlation ID riêng để track audit
trail. Logs xuất JSON (production) hoặc console (development).

Usage:
    from waytoagi.logging import configure_logging, get_logger, bind_context

    configure_logging(level="INFO", format="json")
    logger = get_logger(__name__)
    logger.info("starting pipeline", workers=4, stt_range=(12800, 12876))

    # Bind context cho tất cả logs trong scope:
    with bind_context(correlation_id="run-123", stage="clone"):
        logger.info("cloning record", record_id="recXYZ", stt=12800)
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

if TYPE_CHECKING:
    from collections.abc import Iterator

    from structlog.types import Processor


def configure_logging(level: str = "INFO", format: str = "json") -> None:
    """Configure structlog — call once at app startup.

    Args:
        level: log level (DEBUG, INFO, WARNING, ERROR)
        format: 'json' (production) hoặc 'console' (development)
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Stdlib logging → structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # Common processors
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # Format-specific renderer
    if format == "json":
        renderer: Processor = structlog.processors.JSONRenderer(
            serializer=lambda obj, **kw: _json_dumps(obj),
        )
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _json_dumps(obj: object, **_: object) -> str:
    """JSON serialize với UTF-8 cho tiếng Việt (ensure_ascii=False)."""
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get structlog logger.

    Args:
        name: logger name (thường __name__)

    Returns:
        BoundLogger instance
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def new_correlation_id(prefix: str = "") -> str:
    """Sinh correlation ID mới.

    Args:
        prefix: optional prefix (vd 'pipe-', 'crawl-')

    Returns:
        Format: '{prefix}{8-char-hex}'
    """
    short = uuid.uuid4().hex[:8]
    return f"{prefix}{short}" if prefix else short


@contextmanager
def bind_context(**kwargs: Any) -> Iterator[None]:
    """Context manager — bind kwargs vào context cho tất cả logs trong scope.

    Usage:
        with bind_context(correlation_id="abc123", stage="clone"):
            logger.info("processing")  # logs có correlation_id + stage

    Args:
        **kwargs: key-value pairs để bind
    """
    bind_contextvars(**kwargs)
    try:
        yield
    finally:
        if kwargs:
            clear_contextvars()
