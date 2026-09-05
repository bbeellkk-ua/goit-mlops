"""
Structured audit logging — Блок C5.

Функції з цього модуля використовуються у train / promote / rollback,
щоб писати в stdout JSON-події з полем `event.category=audit`. Loki
підбирає JSON-логи через Promtail (loki-stack), тож у Grafana Explore
можна фільтрувати:

    {app="training"} | json | event_category="audit"
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            base.update(extra)
        return json.dumps(base, ensure_ascii=False, sort_keys=True)


def get_logger(name: str = "mlops") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger


def audit_event(
    action: str,
    *,
    actor: str | None = None,
    resource: str | None = None,
    outcome: str = "success",
    **details: Any,
) -> None:
    """
    Публікує audit-подію.

    action    — machine-readable ім'я дії, напр. `model.transition`.
    actor     — хто виконав (GITLAB_USER_LOGIN / GITHUB_ACTOR / cli-user@host).
    resource  — на чому виконано, напр. `iris-classifier:v3`.
    outcome   — success | failure.
    details   — довільні структуровані поля.
    """
    logger = get_logger("audit")
    payload: dict[str, Any] = {
        "event_category": "audit",
        "event_action": action,
        "actor": actor or os.getenv("GITHUB_ACTOR") or os.getenv("USER") or "unknown",
        "resource": resource,
        "outcome": outcome,
        "details": details,
    }
    logger.info(action, extra={"extra": payload})
