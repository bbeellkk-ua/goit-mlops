"""Тести Pydantic-схем — Блок C1."""

from __future__ import annotations

import pytest

# Плагін-контейнер — імпортуємо з sys.path (додано у conftest)
from app.schemas import IrisFeatures, PredictRequest
from pydantic import ValidationError


def test_iris_features_ok():
    f = IrisFeatures(sepal_length=5.1, sepal_width=3.5, petal_length=1.4, petal_width=0.2)
    assert f.sepal_length == 5.1


def test_iris_features_rejects_string():
    with pytest.raises(ValidationError):
        IrisFeatures(sepal_length="foo", sepal_width=3.5, petal_length=1.4, petal_width=0.2)


def test_iris_features_rejects_negative():
    with pytest.raises(ValidationError):
        IrisFeatures(sepal_length=-1.0, sepal_width=3.5, petal_length=1.4, petal_width=0.2)


def test_iris_features_rejects_out_of_range():
    with pytest.raises(ValidationError):
        IrisFeatures(sepal_length=999.0, sepal_width=3.5, petal_length=1.4, petal_width=0.2)


def test_iris_features_rejects_extra_field():
    with pytest.raises(ValidationError):
        IrisFeatures(
            sepal_length=5.1,
            sepal_width=3.5,
            petal_length=1.4,
            petal_width=0.2,
            evil="bomb",
        )


def test_predict_request_empty_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(instances=[])


def test_predict_request_too_many_rejected():
    inst = {"sepal_length": 5.1, "sepal_width": 3.5, "petal_length": 1.4, "petal_width": 0.2}
    with pytest.raises(ValidationError):
        PredictRequest(instances=[inst] * 101)


def test_predict_request_ok():
    inst = {"sepal_length": 5.1, "sepal_width": 3.5, "petal_length": 1.4, "petal_width": 0.2}
    req = PredictRequest(instances=[inst])
    assert len(req.instances) == 1
