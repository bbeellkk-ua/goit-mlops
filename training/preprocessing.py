"""Preprocessing утиліти — виділені окремо для unit-тестів (Блок F1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd
from sklearn.datasets import load_iris
from sklearn.model_selection import train_test_split

# Feature-порядок фіксований — це контракт між training і inference.
FEATURE_NAMES: tuple[str, ...] = (
    "sepal_length",
    "sepal_width",
    "petal_length",
    "petal_width",
)

CLASS_NAMES: tuple[str, ...] = ("setosa", "versicolor", "virginica")


def load_dataset() -> tuple[pd.DataFrame, pd.Series]:
    """Завантажує Iris у DataFrame із фіксованими іменами колонок."""
    data = load_iris(as_frame=True)
    X = data.data.copy()
    X.columns = list(FEATURE_NAMES)
    y = data.target.copy()
    y.name = "target"
    return X, y


def split_dataset(
    X: pd.DataFrame,
    y: pd.Series,
    test_size: float = 0.2,
    random_state: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """Тонкий враппер над train_test_split з валідацією аргументів."""
    if not 0.0 < test_size < 1.0:
        raise ValueError(f"test_size повинен бути у (0,1), отримано {test_size}")
    return train_test_split(X, y, test_size=test_size, random_state=random_state, stratify=y)


def dataset_hash(X: pd.DataFrame, y: pd.Series) -> str:
    """
    Детермінований хеш датасету — записується в MLflow як `dataset.hash`,
    щоб трасувати «яка версія моделі — на яких даних натренована».
    """
    payload = {
        "X_columns": list(X.columns),
        "X_shape": list(X.shape),
        "X_sha256": hashlib.sha256(np.ascontiguousarray(X.to_numpy()).tobytes()).hexdigest(),
        "y_sha256": hashlib.sha256(np.ascontiguousarray(y.to_numpy()).tobytes()).hexdigest(),
    }
    serialized = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(serialized).hexdigest()


def summarize_dataset(X: pd.DataFrame, y: pd.Series) -> dict[str, Any]:
    """Легкий summary для логів (не для метрик)."""
    return {
        "n_samples": int(X.shape[0]),
        "n_features": int(X.shape[1]),
        "feature_names": list(X.columns),
        "class_counts": y.value_counts().sort_index().to_dict(),
    }
