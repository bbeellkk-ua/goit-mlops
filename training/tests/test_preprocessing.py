"""Unit tests for training/preprocessing.py — Блок F1."""

from __future__ import annotations

import pandas as pd
import pytest

from preprocessing import (
    CLASS_NAMES,
    FEATURE_NAMES,
    dataset_hash,
    load_dataset,
    split_dataset,
    summarize_dataset,
)


def test_load_dataset_shape_and_columns():
    X, y = load_dataset()
    assert isinstance(X, pd.DataFrame)
    assert isinstance(y, pd.Series)
    assert X.shape == (150, 4)
    assert tuple(X.columns) == FEATURE_NAMES
    assert set(y.unique()) == {0, 1, 2}
    assert len(CLASS_NAMES) == 3


def test_split_dataset_default():
    X, y = load_dataset()
    X_tr, X_te, y_tr, y_te = split_dataset(X, y)
    assert len(X_tr) == 120
    assert len(X_te) == 30
    # stratify зберігає розподіл
    assert set(y_te.unique()) == {0, 1, 2}


def test_split_dataset_invalid_test_size():
    X, y = load_dataset()
    with pytest.raises(ValueError):
        split_dataset(X, y, test_size=0.0)
    with pytest.raises(ValueError):
        split_dataset(X, y, test_size=1.5)


def test_dataset_hash_deterministic():
    X, y = load_dataset()
    h1 = dataset_hash(X, y)
    h2 = dataset_hash(X, y)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_dataset_hash_changes_when_data_changes():
    X, y = load_dataset()
    h1 = dataset_hash(X, y)
    X_perturbed = X.copy()
    X_perturbed.iloc[0, 0] += 1.0
    h2 = dataset_hash(X_perturbed, y)
    assert h1 != h2


def test_summarize_dataset():
    X, y = load_dataset()
    summary = summarize_dataset(X, y)
    assert summary["n_samples"] == 150
    assert summary["n_features"] == 4
    assert summary["class_counts"] == {0: 50, 1: 50, 2: 50}
    assert summary["feature_names"] == list(FEATURE_NAMES)
