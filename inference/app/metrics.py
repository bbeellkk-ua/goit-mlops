"""
Prometheus metrics для inference-сервісу — Блок A5.

Метрики:
  - inference_requests_total{method,endpoint,status,model_version}
  - inference_request_duration_seconds{endpoint,model_version}
  - inference_predictions_total{class,model_version}
  - inference_model_load_seconds
  - inference_model_info{version,run_id,checksum} (Info metric)
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

REQUESTS = Counter(
    "inference_requests_total",
    "Total number of inference HTTP requests.",
    labelnames=["method", "endpoint", "status", "model_version"],
    registry=REGISTRY,
)

REQUEST_DURATION = Histogram(
    "inference_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=["endpoint", "model_version"],
    # buckets: <5ms .. 5s — annotated for typical sklearn inference
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

PREDICTIONS = Counter(
    "inference_predictions_total",
    "Total number of successful predictions (by predicted class).",
    labelnames=["predicted_class", "model_version"],
    registry=REGISTRY,
)

MODEL_LOAD_DURATION = Histogram(
    "inference_model_load_seconds",
    "Time spent loading the model from MLflow Registry.",
    buckets=(0.5, 1, 2, 5, 10, 30, 60),
    registry=REGISTRY,
)

MODEL_INFO = Info(
    "inference_model",
    "Currently served model.",
    registry=REGISTRY,
)

MODEL_READY = Gauge(
    "inference_model_ready",
    "1 if the model is loaded and ready, 0 otherwise.",
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
