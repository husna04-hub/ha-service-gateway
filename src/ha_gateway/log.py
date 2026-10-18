"""Structured JSON/text logging for the gateway and demo backends."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

_LOGGER_NAME = "ha_gateway"
_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        event_name = getattr(record, "event", record.getMessage())
        fields = getattr(record, "fields", None) or {}
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        line = f"{stamp} {record.levelname:<7} {event_name} {record.getMessage()}"
        if suffix:
            line = f"{line} {suffix}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line.rstrip()


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    stream: TextIO | None = None,
) -> None:
    global _CONFIGURED
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_ha_gateway_handler", False):
            logger.removeHandler(handler)
            handler.close()
    formatter: logging.Formatter = (
        JsonFormatter() if fmt.lower() == "json" else TextFormatter()
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    handler._ha_gateway_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str | None = None) -> logging.Logger:
    suffix = f".{name}" if name else ""
    logger = logging.getLogger(f"{_LOGGER_NAME}{suffix}")
    if not _CONFIGURED and not logging.getLogger(_LOGGER_NAME).handlers:
        logging.getLogger(_LOGGER_NAME).addHandler(logging.NullHandler())
    return logger


def event(
    logger: logging.Logger,
    level: int,
    event_name: str,
    message: str,
    **fields: Any,
) -> None:
    logger.log(level, message, extra={"event": event_name, "fields": fields})
