"""Structured JSON logging для inference-сервісу — читається Loki через Promtail."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """
    Кожен запис — валідний JSON у stdout. Поля extra з `extra={"extra": {...}}`
    зʼєднуються з базовими на топ-рівні, тому в Loki зручно фільтрувати:
        {app="inference"} | json | event_action="predict"
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        base: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "app": os.getenv("APP_NAME", "inference"),
            "app_version": os.getenv("APP_VERSION", "dev"),
            "model_version": os.getenv("MODEL_VERSION", "unknown"),
            "environment": os.getenv("ENVIRONMENT", "unknown"),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            base.update(extra)
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False, sort_keys=True)


def configure_logging(level: str | None = None) -> logging.Logger:
    log_level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(log_level)

    # Uvicorn / FastAPI пишуть свої логи — приводимо до того ж формату.
    for lname in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(lname)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(log_level)

    return logging.getLogger("inference")
