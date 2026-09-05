"""
Smoke integration-тест на train.main() — Блок F1.

Не потребує реального MLflow-сервера: використовуємо file-based tracking URI
у tempdir. Реєстрація моделі у file-store MLflow працює, тож увесь код-шлях
`log_model → search_model_versions → transition` проходиться end-to-end.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # прибираємо змінні, що могли лишитися з інших джерел
    for k in ("MLFLOW_S3_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_train_main_end_to_end(monkeypatch, tmp_path):
    tracking_dir = tmp_path / "mlruns"
    tracking_dir.mkdir()
    tracking_uri = f"file://{tracking_dir}"

    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("MODEL_NAME", "iris-classifier-test")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", "smoke-test")
    monkeypatch.setenv("LR_C", "1.0")
    monkeypatch.setenv("LR_MAX_ITER", "200")
    monkeypatch.setenv("GIT_COMMIT", "0123abcdef" * 4)
    monkeypatch.setenv("GIT_BRANCH", "test")

    # чистий модульний state
    sys.modules.pop("train", None)
    sys.modules.pop("mlflow_utils", None)
    sys.modules.pop("audit", None)
    sys.modules.pop("preprocessing", None)

    import train  # noqa: E402  (потрібне після monkeypatch.setenv)

    rc = train.main()
    assert rc == 0

    # Перевірка: у MLflow Registry є нова версія у Staging
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=tracking_uri)
    versions = client.search_model_versions("name='iris-classifier-test'")
    assert versions, "Модель не зареєстрована"
    staging = [v for v in versions if v.current_stage == "Staging"]
    assert staging, "Немає Staging-версії"
    # SHA256 tag присутній
    mv = client.get_model_version(name="iris-classifier-test", version=staging[0].version)
    tags = mv.tags or {}
    assert "checksum.sha256" in tags, f"Немає checksum tag; наявні: {list(tags)}"
    assert len(tags["checksum.sha256"]) == 64
