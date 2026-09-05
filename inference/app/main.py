"""
FastAPI inference-сервіс для iris-classifier.

Ендпоінти:
  GET  /healthz     — liveness (завжди 200)
  GET  /readyz      — readiness (200 тільки якщо модель готова)
  GET  /metrics     — Prometheus scrape (з нашого CollectorRegistry)
  GET  /model       — детальна інформація про завантажену версію
  POST /predict     — batch inference

Security (Блок C):
  - вхід валідується Pydantic (`schemas.PredictRequest`);
  - все, що не пройшло валідацію → 400 з generic message, без internals;
  - rate limit через slowapi (`REQUESTS_PER_MINUTE`, per-IP);
  - модель завантажується з перевіркою SHA256 (`model_loader`).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from .logging_config import configure_logging
from .metrics import PREDICTIONS, REQUEST_DURATION, REQUESTS, render_metrics
from .model_loader import LoadedModel, ModelIntegrityError, load_model
from .predictions_sink import sink_predictions
from .schemas import (
    CLASS_NAMES,
    HealthResponse,
    ModelInfoResponse,
    PredictRequest,
    PredictResponse,
    Prediction,
)

log = configure_logging()

# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

STATE: dict[str, Any] = {"model": None, "error": None}

REQUESTS_PER_MINUTE = os.environ.get("REQUESTS_PER_MINUTE", "60")
limiter = Limiter(key_func=get_remote_address, default_limits=[f"{REQUESTS_PER_MINUTE}/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Завантажуємо модель на старті. Якщо checksum не збігається — сервіс не readiness."""
    try:
        model = load_model()
        STATE["model"] = model
        # Промотуємо ENV, щоб logging_config додавав правильні поля.
        os.environ["MODEL_VERSION"] = model.version
        log.info(
            "startup_complete",
            extra={
                "extra": {
                    "event_action": "app.startup",
                    "model_name": model.name,
                    "model_version": model.version,
                    "stage": model.stage,
                }
            },
        )
    except ModelIntegrityError as exc:
        STATE["error"] = f"integrity: {exc}"
        log.error(
            "startup_integrity_failure",
            extra={"extra": {"event_action": "app.startup_failed", "error": str(exc)}},
        )
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = str(exc)
        log.error(
            "startup_failure",
            extra={"extra": {"event_action": "app.startup_failed", "error": str(exc)}},
            exc_info=True,
        )
    yield
    log.info("shutdown", extra={"extra": {"event_action": "app.shutdown"}})


app = FastAPI(
    title="iris-classifier inference",
    version=os.getenv("APP_VERSION", "dev"),
    docs_url="/docs" if os.getenv("ENABLE_DOCS", "true").lower() == "true" else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.state.limiter = limiter


# ---------------------------------------------------------------------------
# Middleware: request_id + metrics + latency
# ---------------------------------------------------------------------------


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        model: LoadedModel | None = STATE.get("model")
        mv = model.version if model else "unknown"

        start = time.perf_counter()
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:  # noqa: BLE001
            status_code = 500
            raise
        finally:
            elapsed = time.perf_counter() - start
            endpoint = request.url.path
            REQUESTS.labels(
                method=request.method,
                endpoint=endpoint,
                status=str(status_code),
                model_version=mv,
            ).inc()
            # dontlazily observe /metrics itself
            if endpoint != "/metrics":
                REQUEST_DURATION.labels(endpoint=endpoint, model_version=mv).observe(elapsed)

            log.info(
                "http_request",
                extra={
                    "extra": {
                        "event_action": "http.request",
                        "request_id": request_id,
                        "method": request.method,
                        "endpoint": endpoint,
                        "status": status_code,
                        "duration_ms": round(elapsed * 1000, 2),
                        "client_ip": get_remote_address(request),
                        "user_agent": request.headers.get("user-agent", ""),
                    }
                },
            )

        response.headers["x-request-id"] = request_id
        return response


app.add_middleware(ObservabilityMiddleware)


# ---------------------------------------------------------------------------
# Exception handlers — не витікаємо internals (Блок C1)
# ---------------------------------------------------------------------------


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    log.warning(
        "validation_failed",
        extra={
            "extra": {
                "event_action": "validation.failed",
                "request_id": getattr(request.state, "request_id", None),
                "error_count": len(exc.errors()),
                # errors містять info про поля, це corollary для клієнта, не витік
                "errors": [
                    {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ],
            }
        },
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "invalid request", "errors": exc.errors()},
    )


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    log.warning(
        "rate_limit_exceeded",
        extra={
            "extra": {
                "event_action": "ratelimit.exceeded",
                "client_ip": get_remote_address(request),
                "limit": str(exc.detail),
            }
        },
    )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "rate limit exceeded"},
        headers={"Retry-After": "60"},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness — сервіс живий, навіть якщо модель ще не готова."""
    model: LoadedModel | None = STATE.get("model")
    return HealthResponse(
        status="ok",
        model_ready=model is not None,
        model_name=model.name if model else None,
        model_version=model.version if model else None,
    )


@app.get("/readyz", response_model=HealthResponse)
async def readyz(response: Response) -> HealthResponse:
    """Readiness — 200 тільки коли модель завантажена і checksum валідний."""
    model: LoadedModel | None = STATE.get("model")
    if model is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="not_ready", model_ready=False)
    return HealthResponse(
        status="ready",
        model_ready=True,
        model_name=model.name,
        model_version=model.version,
    )


@app.get("/metrics")
async def metrics_endpoint():
    payload, content_type = render_metrics()
    return PlainTextResponse(payload, media_type=content_type)


@app.get("/model", response_model=ModelInfoResponse)
async def model_info() -> ModelInfoResponse:
    model: LoadedModel | None = STATE.get("model")
    if model is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "model not loaded"},
        )
    return ModelInfoResponse(
        model_name=model.name,
        model_version=model.version,
        run_id=model.run_id,
        checksum_sha256=model.checksum_sha256,
        stage=model.stage,
        git_commit=model.tags.get("git.commit"),
        dataset_hash=model.tags.get("dataset.hash"),
    )


@app.post("/predict", response_model=PredictResponse)
@limiter.limit(f"{REQUESTS_PER_MINUTE}/minute")
async def predict(
    request: Request,
    payload: PredictRequest,
    background: BackgroundTasks,
) -> PredictResponse:
    model: LoadedModel | None = STATE.get("model")
    if model is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "model not loaded"},
        )

    # Формуємо DataFrame з тим самим порядком колонок, що й у training.
    df = pd.DataFrame([f.model_dump() for f in payload.instances])
    df = df[["sepal_length", "sepal_width", "petal_length", "petal_width"]]

    t0 = time.perf_counter()
    raw = model.pyfunc.predict(df)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Отримати probability-и напряму з sklearn моделі, якщо це можливо.
    # `mlflow.pyfunc.load_model` для sklearn.log_model повертає pyfunc обгортку,
    # у якої під капотом sklearn — дістати predict_proba можна через _model_impl.
    proba = None
    impl = getattr(model.pyfunc, "_model_impl", None)
    sklearn_model = getattr(impl, "sklearn_model", None)
    if sklearn_model is not None and hasattr(sklearn_model, "predict_proba"):
        try:
            proba = sklearn_model.predict_proba(df)
        except Exception:  # noqa: BLE001
            proba = None

    request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
    predictions: list[Prediction] = []
    sink_records: list[dict[str, Any]] = []

    for i, cls in enumerate(list(raw)):
        cls_int = int(cls)
        label = CLASS_NAMES[cls_int] if 0 <= cls_int < len(CLASS_NAMES) else str(cls_int)
        probs = (
            {CLASS_NAMES[j]: float(proba[i][j]) for j in range(len(CLASS_NAMES))}
            if proba is not None
            else {label: 1.0}
        )
        predictions.append(
            Prediction(predicted_class=cls_int, predicted_label=label, probabilities=probs)
        )
        PREDICTIONS.labels(predicted_class=label, model_version=model.version).inc()
        sink_records.append(
            {
                "request_id": request_id,
                "timestamp": time.time(),
                "model_name": model.name,
                "model_version": model.version,
                "features": payload.instances[i].model_dump(),
                "predicted_class": cls_int,
                "predicted_label": label,
                "probabilities": probs,
            }
        )

    log.info(
        "predict_done",
        extra={
            "extra": {
                "event_action": "predict",
                "request_id": request_id,
                "batch_size": len(predictions),
                "duration_ms": round(elapsed_ms, 2),
                "model_version": model.version,
            }
        },
    )

    # Best-effort запис у MinIO для drift-аналізу.
    background.add_task(sink_predictions, sink_records)

    return PredictResponse(
        model_name=model.name,
        model_version=model.version,
        predictions=predictions,
    )


# --- Root -----------------------------------------------------------------


@app.get("/")
async def root():
    return {
        "service": "iris-classifier inference",
        "environment": os.getenv("ENVIRONMENT", "unknown"),
        "endpoints": ["/healthz", "/readyz", "/metrics", "/model", "/predict"],
    }
