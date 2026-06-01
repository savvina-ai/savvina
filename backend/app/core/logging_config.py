# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Structured logging configuration — JSON (production) or human-readable (dev)."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import logging.config
import traceback

from .log_filter import HealthCheckFilter, SensitiveQueryParamFilter
from .request_context import request_id_var, user_id_var


class RequestContextFilter(logging.Filter):
    """Inject request-scoped ContextVar values into every LogRecord.

    Downstream formatters can reference ``record.request_id``,
    ``record.user_id``, and ``record.org_id`` without the log call site
    needing to pass them explicitly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()  # type: ignore[attr-defined]
        record.user_id = user_id_var.get()  # type: ignore[attr-defined]
        return True


class JSONFormatter(logging.Formatter):
    """Single-line JSON log formatter for machine-parseable output."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "user_id": getattr(record, "user_id", None),
            "module": record.module,
            "function": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry, default=str)


_TEXT_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-5s [%(request_id)s] [%(user_id)s] %(name)s — %(message)s"
)
_TEXT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _NoneToHyphen(logging.Formatter):
    """Text formatter that replaces None context values with '-' for readability."""

    def format(self, record: logging.LogRecord) -> str:
        for attr in ("request_id", "user_id"):
            if getattr(record, attr, None) is None:
                setattr(record, attr, "-")
        return super().format(record)


def configure_logging(level: str, fmt: str = "text") -> None:
    """Set up the root logger and uvicorn loggers with structured output.

    Args:
        level: Log level name (e.g. ``"INFO"``).
        fmt: ``"json"`` for single-line JSON output, ``"text"`` for
            human-readable output with request context prefix.
    """
    level = level.upper()
    use_json = fmt.lower() == "json"

    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setLevel(level)

    if use_json:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(_NoneToHyphen(fmt=_TEXT_FORMAT, datefmt=_TEXT_DATE_FORMAT))

    handler.addFilter(RequestContextFilter())
    root.addHandler(handler)

    for uvi_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvi_logger = logging.getLogger(uvi_name)
        uvi_logger.handlers.clear()
        uvi_logger.propagate = True

    # SQLAlchemy's echo=True (set at engine construction, before lifespan runs) adds a
    # legacy StreamHandler with default Python format directly to the "sqlalchemy.engine.Engine"
    # child logger and sets its level to INFO. We must clear handlers AND reset levels to
    # NOTSET on every SA child logger so they inherit from the "sqlalchemy" parent, which
    # we control here. Without NOTSET the explicit INFO level SA sets survives our cleanup.
    sa_level = logging.DEBUG if level == "DEBUG" else logging.WARNING
    sa_root = logging.getLogger("sqlalchemy")
    sa_root.handlers.clear()
    sa_root.propagate = True
    sa_root.setLevel(sa_level)

    for _sa_child in (
        "sqlalchemy.engine",
        "sqlalchemy.engine.Engine",
        "sqlalchemy.pool",
        "sqlalchemy.pool.impl",
        "sqlalchemy.orm",
    ):
        _child = logging.getLogger(_sa_child)
        _child.handlers.clear()
        _child.propagate = True
        _child.setLevel(logging.NOTSET)  # inherit from "sqlalchemy" parent

    access_logger = logging.getLogger("uvicorn.access")
    for f in (HealthCheckFilter(), SensitiveQueryParamFilter()):
        if not any(isinstance(existing, type(f)) for existing in access_logger.filters):
            access_logger.addFilter(f)
