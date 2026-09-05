"""
Pydantic-схеми для inference-endpoint — Блок C1 (input validation).

Iris feature diapasons (з довідника scikit-learn) використані як
розсудливі bounds — виходи за їх межі вважаються некоректним входом
і повертають HTTP 400 без витоку деталей.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

CLASS_NAMES = ("setosa", "versicolor", "virginica")

# Boundaries на 3σ від Iris — далеко достатньо для нормальних кейсів,
# але відсіювають явно некоректні дані.
_LEN_MIN, _LEN_MAX = 0.0, 15.0
_WID_MIN, _WID_MAX = 0.0, 15.0


class IrisFeatures(BaseModel):
    """Один зразок iris."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        # робить помилки читабельними, але не витікає internals
        json_schema_extra={
            "example": {
                "sepal_length": 5.1,
                "sepal_width": 3.5,
                "petal_length": 1.4,
                "petal_width": 0.2,
            }
        },
    )

    sepal_length: Annotated[float, Field(ge=_LEN_MIN, le=_LEN_MAX)]
    sepal_width: Annotated[float, Field(ge=_WID_MIN, le=_WID_MAX)]
    petal_length: Annotated[float, Field(ge=_LEN_MIN, le=_LEN_MAX)]
    petal_width: Annotated[float, Field(ge=_WID_MIN, le=_WID_MAX)]


class PredictRequest(BaseModel):
    """Batch запит — до 100 зразків, щоб уникнути DoS через величезні пейлоади."""

    model_config = ConfigDict(extra="forbid")
    instances: Annotated[list[IrisFeatures], Field(min_length=1, max_length=100)]


class Prediction(BaseModel):
    predicted_class: int
    predicted_label: str
    probabilities: dict[str, float]


class PredictResponse(BaseModel):
    model_name: str
    model_version: str
    predictions: list[Prediction]


class HealthResponse(BaseModel):
    status: str
    model_ready: bool
    model_name: str | None = None
    model_version: str | None = None


class ModelInfoResponse(BaseModel):
    model_name: str
    model_version: str
    run_id: str | None = None
    checksum_sha256: str | None = None
    stage: str | None = None
    git_commit: str | None = None
    dataset_hash: str | None = None
