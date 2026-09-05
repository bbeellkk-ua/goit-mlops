"""
Асинхронне запис прогнозів у S3 (MinIO) — використовується Evidently CronJob-ом
для аналізу drift (Блок E1).

Формат: JSONL, один batch = один файл `predictions/<env>/<yyyy>/<mm>/<dd>/<ts>-<uuid>.jsonl`.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

try:
    import boto3
    from botocore.client import Config as BotoConfig
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]
    BotoConfig = None  # type: ignore[assignment]

log = logging.getLogger("inference.sink")

# Стан-глобал зʼєднання (lazy).
_client_lock = threading.Lock()
_client = None


def _make_client():
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        if boto3 is None:
            return None
        endpoint = os.environ.get("MLFLOW_S3_ENDPOINT_URL") or os.environ.get("S3_ENDPOINT_URL")
        if not endpoint:
            return None
        _client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
            config=BotoConfig(signature_version="s3v4") if BotoConfig else None,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        return _client


def _bucket() -> str:
    return os.environ.get("PREDICTIONS_BUCKET", "mlflow-artifacts")


def _key_prefix() -> str:
    env = os.environ.get("ENVIRONMENT", "staging")
    now = datetime.now(tz=UTC)
    return f"predictions/{env}/{now:%Y/%m/%d}"


def sink_predictions(records: list[dict[str, Any]]) -> None:
    """
    Best-effort: не блокуємо запит, якщо MinIO недоступний — просто логуємо
    та повертаємось. Викликати з BackgroundTasks.
    """
    if not records:
        return
    client = _make_client()
    if client is None:
        log.debug("predictions_sink_disabled")
        return
    try:
        buf = io.BytesIO()
        for r in records:
            buf.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
        buf.seek(0)
        now = datetime.now(tz=UTC)
        key = f"{_key_prefix()}/{now:%H%M%S}-{uuid.uuid4().hex[:8]}.jsonl"
        client.put_object(Bucket=_bucket(), Key=key, Body=buf.getvalue())
        log.info(
            "predictions_written",
            extra={
                "extra": {
                    "event_action": "predictions.sink",
                    "count": len(records),
                    "s3_key": key,
                }
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "predictions_sink_failed",
            extra={"extra": {"event_action": "predictions.sink_failed", "error": str(exc)}},
        )
