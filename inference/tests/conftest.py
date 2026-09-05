"""
pytest fixtures — готуємо MLflow file-store, тренуємо тестову модель,
підіймаємо FastAPI TestClient. Використовується у test_endpoint.py.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Дозволяємо імпорти з inference/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _compute_local_sha256(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        rel = str(f.relative_to(root)).encode()
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        with f.open("rb") as fp:
            for chunk in iter(lambda: fp.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="session")
def mlflow_env():
    """Тренує та реєструє тестову модель у file-store MLflow."""
    import mlflow
    import mlflow.sklearn
    from mlflow.tracking import MlflowClient
    from sklearn.datasets import load_iris
    from sklearn.linear_model import LogisticRegression

    td = tempfile.mkdtemp(prefix="mlflow-test-")
    tracking_uri = f"file://{td}"
    os.environ["MLFLOW_TRACKING_URI"] = tracking_uri
    os.environ["MODEL_NAME"] = "iris-classifier-it"
    os.environ["MODEL_STAGE"] = "Production"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["REQUESTS_PER_MINUTE"] = "5"  # маленький ліміт для перевірки

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("iris-it")

    X, y = load_iris(return_X_y=True, as_frame=True)
    with mlflow.start_run() as run:
        model = LogisticRegression(max_iter=500)
        model.fit(X, y)
        mlflow.sklearn.log_model(
            sk_model=model,
            artifact_path="model",
            registered_model_name="iris-classifier-it",
        )
        run_id = run.info.run_id

    client = MlflowClient(tracking_uri=tracking_uri)
    versions = client.search_model_versions("name='iris-classifier-it'")
    v = max(versions, key=lambda x: int(x.version))

    # SHA256 tag — щоб пройшла перевірка у model_loader
    dl = mlflow.artifacts.download_artifacts(
        run_id=run_id, artifact_path="model", dst_path=td + "/dl"
    )
    checksum = _compute_local_sha256(Path(dl))
    client.set_model_version_tag("iris-classifier-it", v.version, "checksum.sha256", checksum)
    client.transition_model_version_stage(
        "iris-classifier-it", v.version, stage="Production", archive_existing_versions=False
    )
    return {"tracking_uri": tracking_uri, "checksum": checksum, "version": v.version}


@pytest.fixture(scope="session")
def client(mlflow_env):
    """FastAPI TestClient з увімкненою моделлю."""
    # Важливо: reset модулів, щоб перечитали ENV
    for m in [
        "app.main",
        "app.model_loader",
        "app.metrics",
        "app.schemas",
        "app.logging_config",
        "app.predictions_sink",
    ]:
        sys.modules.pop(m, None)
    from app.main import app  # noqa: E402
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
