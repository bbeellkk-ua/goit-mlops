"""Evidently drift job — розділений на 3 CLI-режими для Argo Workflows DAG.

Модуль підтримує два способи запуску:

1. **Monolith mode** (`python drift_job.py`) — CronJob-режим:
   виконує весь цикл fetch → compute → push у одному контейнері.
   Зберігається для локальної розробки.

2. **Argo DAG mode** — три кроки, які викликаються Argo Workflows як
   окремі pod-и з передачею artefacts через MinIO:

   ┌─────────────────┐  parquet  ┌──────────────────┐  json    ┌──────────────┐
   │ fetch-predictions├──────────▶│ compute-drift     ├─────────▶│ publish      │
   │ (S3 list+read)   │ current.pq│ (PSI vs reference)│ scores.  │ (Pushgateway │
   │                  │           │                    │ json     │  + audit)    │
   └─────────────────┘           └──────────────────┘          └──────────────┘

   Виклики:
     python drift_job.py fetch    --output /tmp/current.parquet
     python drift_job.py compute  --input  /tmp/current.parquet \
                                  --output /tmp/scores.json
     python drift_job.py publish  --input  /tmp/scores.json

Кожен крок читає та пише щонайменше один файл, тому Argo Workflows може
підняти його як artefact у MinIO і передати наступному крокові. Це дає:
- окремі pod-и з окремими логами → простіше post-mortem;
- retry на рівні кроку (наприклад, при мережевій помилці S3);
- візуалізацію DAG у Argo UI (`argo.squirell.pp.ua`) з подіями кожного
  кроку.

Environment variables (спільні для всіх режимів):
  MODEL_NAME            (default: iris-classifier)
  ENVIRONMENT           (default: production)
  MLFLOW_S3_ENDPOINT_URL
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  PREDICTIONS_BUCKET    (default: mlflow-artifacts)
  PREDICTIONS_PREFIX    (default: predictions/<ENVIRONMENT>/)
  LOOKBACK_HOURS        (default: 24)
  PUSHGATEWAY_URL       (default: monitoring pushgateway svc)
  LOG_LEVEL             (default: INFO)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import numpy as np
import pandas as pd
from botocore.config import Config
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
from sklearn.datasets import load_iris

FEATURE_NAMES = (
    "sepal_length",
    "sepal_width",
    "petal_length",
    "petal_width",
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": %(message)s}',
)
log = logging.getLogger("drift-job")


# ---------------------------------------------------------------------------
# Helpers (загальні для monolith та DAG-режимам)
# ---------------------------------------------------------------------------


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _load_reference() -> pd.DataFrame:
    ds = load_iris(as_frame=True)
    df = ds.frame.copy()
    df.columns = [*FEATURE_NAMES, "target"]
    return df[list(FEATURE_NAMES)]


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_env("MLFLOW_S3_ENDPOINT_URL"),
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _list_recent_objects(s3, bucket: str, prefix: str, since: datetime) -> list[dict[str, Any]]:
    paginator = s3.get_paginator("list_objects_v2")
    out: list[dict[str, Any]] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["LastModified"].astimezone(UTC) >= since:
                out.append(obj)
    log.info(json.dumps({"event": "s3_list", "bucket": bucket, "prefix": prefix, "found": len(out)}))
    return out


def _read_predictions(s3, bucket: str, objects: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for obj in objects:
        body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
        for line in body.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Expect either flat {feature_name: value, ...} or {"features": {...}, ...}
            features = rec.get("features", rec)
            row = {name: features.get(name) for name in FEATURE_NAMES}
            if all(v is not None for v in row.values()):
                rows.append(row)
    df = pd.DataFrame(rows)
    log.info(json.dumps({"event": "predictions_loaded", "rows": len(df)}))
    return df


def _psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index for a single feature."""
    edges = np.linspace(
        min(reference.min(), current.min()),
        max(reference.max(), current.max()),
        bins + 1,
    )
    ref_hist, _ = np.histogram(reference, bins=edges)
    cur_hist, _ = np.histogram(current, bins=edges)
    ref_pct = ref_hist / max(ref_hist.sum(), 1)
    cur_pct = cur_hist / max(cur_hist.sum(), 1)
    # Laplace smoothing to avoid log(0).
    ref_pct = np.where(ref_pct == 0, 1e-6, ref_pct)
    cur_pct = np.where(cur_pct == 0, 1e-6, cur_pct)
    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


def compute_drift(reference: pd.DataFrame, current: pd.DataFrame) -> dict[str, float]:
    scores: dict[str, float] = {}
    for feature in FEATURE_NAMES:
        scores[feature] = _psi(reference[feature].to_numpy(), current[feature].to_numpy())
    scores["_overall"] = float(np.mean(list(scores.values())))
    return scores


def push_metrics(
    pushgateway_url: str,
    model_name: str,
    environment: str,
    drift_scores: dict[str, float],
    sample_count: int,
) -> None:
    registry = CollectorRegistry()
    drift_gauge = Gauge(
        "mlops_drift_score",
        "PSI-style drift score of live features vs training reference",
        labelnames=("model_name", "environment", "feature"),
        registry=registry,
    )
    ts_gauge = Gauge(
        "mlops_drift_last_run_timestamp_seconds",
        "Unix timestamp of last successful drift computation",
        labelnames=("model_name", "environment"),
        registry=registry,
    )
    samples_gauge = Gauge(
        "mlops_drift_sample_count",
        "Number of live predictions used in the drift computation",
        labelnames=("model_name", "environment"),
        registry=registry,
    )

    for feature, score in drift_scores.items():
        drift_gauge.labels(
            model_name=model_name,
            environment=environment,
            feature=feature,
        ).set(score)

    ts_gauge.labels(model_name=model_name, environment=environment).set(time.time())
    samples_gauge.labels(model_name=model_name, environment=environment).set(sample_count)

    push_to_gateway(
        pushgateway_url,
        job="mlops-drift",
        grouping_key={"model_name": model_name, "environment": environment},
        registry=registry,
    )
    log.info(json.dumps({"event": "pushgateway_ok", "url": pushgateway_url}))


# ---------------------------------------------------------------------------
# DAG mode entrypoints — виконуються Argo Workflows як окремі кроки
# ---------------------------------------------------------------------------


def _empty_current_df() -> pd.DataFrame:
    """Порожній DataFrame з правильними стовпцями (для no-data випадку)."""
    return pd.DataFrame(columns=list(FEATURE_NAMES))


def cmd_fetch(args: argparse.Namespace) -> int:
    """Крок 1 DAG: список та завантаження предикцій з MinIO у Parquet."""
    environment = os.environ.get("ENVIRONMENT", "production")
    bucket = os.environ.get("PREDICTIONS_BUCKET", "mlflow-artifacts")
    prefix = os.environ.get("PREDICTIONS_PREFIX", f"predictions/{environment}/")
    lookback_hours = int(os.environ.get("LOOKBACK_HOURS", "24"))

    since = datetime.now(UTC) - timedelta(hours=lookback_hours)

    log.info(json.dumps({
        "event": "fetch_start",
        "bucket": bucket,
        "prefix": prefix,
        "lookback_hours": lookback_hours,
    }))

    s3 = _s3_client()
    objects = _list_recent_objects(s3, bucket, prefix, since)

    if not objects:
        log.warning(json.dumps({
            "event": "no_predictions_found",
            "prefix": prefix,
            "since": since.isoformat(),
        }))
        df = _empty_current_df()
    else:
        df = _read_predictions(s3, bucket, objects)
        if df.empty:
            log.warning(json.dumps({"event": "empty_current_df"}))

    df.to_parquet(args.output, index=False)
    log.info(json.dumps({"event": "fetch_done", "rows": len(df), "output": args.output}))
    return 0


def cmd_compute(args: argparse.Namespace) -> int:
    """Крок 2 DAG: рахунок PSI по фічах, запис JSON scores."""
    log.info(json.dumps({"event": "compute_start", "input": args.input}))
    current = pd.read_parquet(args.input)
    reference = _load_reference()

    if current.empty:
        scores = {**{f: 0.0 for f in FEATURE_NAMES}, "_overall": 0.0}
        payload = {"scores": scores, "sample_count": 0, "empty": True}
    else:
        scores = compute_drift(reference, current)
        payload = {"scores": scores, "sample_count": int(len(current)), "empty": False}

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    log.info(json.dumps({"event": "compute_done", **payload}))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    """Крок 3 DAG: push у Pushgateway + JSON audit-подія у stdout."""
    log.info(json.dumps({"event": "publish_start", "input": args.input}))

    model_name = os.environ.get("MODEL_NAME", "iris-classifier")
    environment = os.environ.get("ENVIRONMENT", "production")
    pushgateway_url = os.environ.get(
        "PUSHGATEWAY_URL",
        "http://pushgateway-prometheus-pushgateway.monitoring.svc.cluster.local:9091",
    )

    with open(args.input, encoding="utf-8") as fh:
        payload = json.load(fh)

    scores: dict[str, float] = payload["scores"]
    sample_count: int = int(payload["sample_count"])

    push_metrics(pushgateway_url, model_name, environment, scores, sample_count=sample_count)

    # Audit-подія у stdout — Promtail → Loki.
    audit = {
        "event_category": "drift",
        "event_action": "drift.computed",
        "workflow_name": os.environ.get("WORKFLOW_NAME", ""),
        "model_name": model_name,
        "environment": environment,
        "sample_count": sample_count,
        "overall_score": scores.get("_overall", 0.0),
        "empty_window": bool(payload.get("empty", False)),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(json.dumps(audit), flush=True)
    log.info(json.dumps({"event": "publish_done", "url": pushgateway_url}))
    return 0


def cmd_run(_: argparse.Namespace) -> int:
    """Monolith mode: CronJob-цикл в одному контейнері."""
    model_name = os.environ.get("MODEL_NAME", "iris-classifier")
    environment = os.environ.get("ENVIRONMENT", "production")
    bucket = os.environ.get("PREDICTIONS_BUCKET", "mlflow-artifacts")
    prefix = os.environ.get("PREDICTIONS_PREFIX", f"predictions/{environment}/")
    lookback_hours = int(os.environ.get("LOOKBACK_HOURS", "24"))
    pushgateway_url = os.environ.get(
        "PUSHGATEWAY_URL",
        "http://pushgateway-prometheus-pushgateway.monitoring.svc.cluster.local:9091",
    )

    log.info(json.dumps({
        "event": "start",
        "mode": "monolith",
        "model_name": model_name,
        "environment": environment,
        "bucket": bucket,
        "prefix": prefix,
        "lookback_hours": lookback_hours,
    }))

    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    reference = _load_reference()
    s3 = _s3_client()
    objects = _list_recent_objects(s3, bucket, prefix, since)

    if not objects:
        log.warning(json.dumps({
            "event": "no_predictions_found",
            "prefix": prefix,
            "since": since.isoformat(),
        }))
        push_metrics(
            pushgateway_url,
            model_name,
            environment,
            {**{f: 0.0 for f in FEATURE_NAMES}, "_overall": 0.0},
            sample_count=0,
        )
        return 0

    current = _read_predictions(s3, bucket, objects)
    if current.empty:
        log.warning(json.dumps({"event": "empty_current_df"}))
        push_metrics(
            pushgateway_url,
            model_name,
            environment,
            {**{f: 0.0 for f in FEATURE_NAMES}, "_overall": 0.0},
            sample_count=0,
        )
        return 0

    scores = compute_drift(reference, current)
    log.info(json.dumps({"event": "drift_computed", "scores": scores, "n_current": len(current)}))
    push_metrics(pushgateway_url, model_name, environment, scores, sample_count=len(current))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MLOps drift job (Iris)")
    sub = p.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch", help="DAG step 1: fetch predictions → parquet")
    p_fetch.add_argument("--output", required=True, help="Path to write current.parquet")
    p_fetch.set_defaults(func=cmd_fetch)

    p_compute = sub.add_parser("compute", help="DAG step 2: compute PSI → json")
    p_compute.add_argument("--input", required=True, help="Path to current.parquet")
    p_compute.add_argument("--output", required=True, help="Path to write scores.json")
    p_compute.set_defaults(func=cmd_compute)

    p_publish = sub.add_parser("publish", help="DAG step 3: push metrics + audit")
    p_publish.add_argument("--input", required=True, help="Path to scores.json")
    p_publish.set_defaults(func=cmd_publish)

    p_run = sub.add_parser("run", help="Monolith mode (CronJob behavior)")
    p_run.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Без аргументів → monolith mode (backward compatibility).
        return cmd_run(args)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
