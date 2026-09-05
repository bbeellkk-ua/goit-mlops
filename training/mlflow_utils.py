"""Спільні MLflow-утиліти: клієнт + SHA256 model artifact-у."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import mlflow
from mlflow.tracking import MlflowClient


def build_client() -> MlflowClient:
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        raise RuntimeError("MLFLOW_TRACKING_URI не заданий")
    mlflow.set_tracking_uri(tracking_uri)
    return MlflowClient(tracking_uri=tracking_uri)


def compute_artifact_sha256(run_id: str, artifact_path: str = "model") -> str:
    """
    Обчислює детермінований SHA256 усіх файлів моделі — Блок C4.

    Спочатку завантажує артефакти у tmpdir, потім хешує їх у sorted-by-path
    порядку. Sha256 записується у `mlflow_utils.tag_model_version_checksum`
    як version-tag `checksum.sha256`, а inference-сервіс перевіряє його
    перед завантаженням.
    """
    with tempfile.TemporaryDirectory() as td:
        local_path = mlflow.artifacts.download_artifacts(
            run_id=run_id, artifact_path=artifact_path, dst_path=td
        )
        root = Path(local_path)
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


def tag_model_version_checksum(
    client: MlflowClient,
    registered_model_name: str,
    version: str | int,
    checksum: str,
) -> None:
    client.set_model_version_tag(
        name=registered_model_name,
        version=str(version),
        key="checksum.sha256",
        value=checksum,
    )


def latest_version_in_stage(
    client: MlflowClient,
    registered_model_name: str,
    stage: str,
) -> str | None:
    """
    Повертає останню версію в даному stage або None.
    `search_model_versions` вживається замість deprecated `get_latest_versions`
    щоб код працював і на нових MLflow (>=2.9).
    """
    versions = client.search_model_versions(f"name='{registered_model_name}'")
    matching = [v for v in versions if v.current_stage == stage]
    if not matching:
        return None
    return max(matching, key=lambda v: int(v.version)).version
