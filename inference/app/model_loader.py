"""
Model loader — Блок C4 (immutable model artifacts / checksum verification).

Кроки:
  1. З MLflow Registry знаходимо model version у заданій стадії (Production/Staging).
  2. Читаємо model-version tag `checksum.sha256`, який training-pipeline
     розрахував і записав.
  3. Завантажуємо артефакти у tmpdir і рахуємо SHA256 своїм алгоритмом
     (той самий, що в training).
  4. Порівнюємо — якщо не збігається, сервіс НЕ стартує (fail-closed).
  5. Завантажуємо модель через `mlflow.pyfunc.load_model` вже з локального
     шляху (щоб не тягнути двічі).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import mlflow
import mlflow.pyfunc
from mlflow.tracking import MlflowClient

from .metrics import MODEL_INFO, MODEL_LOAD_DURATION, MODEL_READY

log = logging.getLogger("inference.model_loader")


class ModelIntegrityError(RuntimeError):
    """Raised when checksum verification fails — fail-closed."""


@dataclass
class LoadedModel:
    name: str
    version: str
    stage: str
    run_id: str
    checksum_sha256: str
    pyfunc: object  # mlflow.pyfunc.PyFuncModel — не типізуємо, щоб не тягти
    tags: dict[str, str] = field(default_factory=dict)


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


def _pick_version(client: MlflowClient, model_name: str, stage: str) -> object:
    versions = client.search_model_versions(f"name='{model_name}'")
    matching = [v for v in versions if v.current_stage == stage]
    if not matching:
        raise RuntimeError(f"У Registry немає version у стадії {stage} для {model_name}")
    return max(matching, key=lambda v: int(v.version))


def load_model(
    model_name: str | None = None,
    stage: str | None = None,
    tracking_uri: str | None = None,
    require_checksum: bool | None = None,
) -> LoadedModel:
    model_name = model_name or os.environ.get("MODEL_NAME", "iris-classifier")
    stage = stage or os.environ.get("MODEL_STAGE", "Production")
    tracking_uri = tracking_uri or os.environ.get("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        raise RuntimeError("MLFLOW_TRACKING_URI не заданий")
    require_checksum = (
        require_checksum
        if require_checksum is not None
        else os.environ.get("REQUIRE_CHECKSUM", "true").lower() == "true"
    )

    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient(tracking_uri=tracking_uri)

    log.info(
        "model_load_start",
        extra={"extra": {"event_action": "model.load", "model": model_name, "stage": stage}},
    )
    t0 = time.perf_counter()
    mv = _pick_version(client, model_name, stage)
    tags = dict(mv.tags or {})
    expected = tags.get("checksum.sha256")

    with tempfile.TemporaryDirectory() as td:
        local_path = mlflow.artifacts.download_artifacts(
            run_id=mv.run_id, artifact_path="model", dst_path=td
        )
        actual = _compute_local_sha256(Path(local_path))

        if require_checksum:
            if not expected:
                MODEL_READY.set(0)
                raise ModelIntegrityError(
                    "У model-version tags відсутній `checksum.sha256`. "
                    "Всі production-версії мають бути registered через training-pipeline."
                )
            if actual != expected:
                MODEL_READY.set(0)
                raise ModelIntegrityError(f"Checksum mismatch: expected={expected} actual={actual}")
            log.info(
                "checksum_verified",
                extra={"extra": {"event_action": "model.checksum_verified", "sha256": actual}},
            )
        else:
            log.warning(
                "checksum_skipped",
                extra={"extra": {"event_action": "model.checksum_skipped"}},
            )

        pyfunc = mlflow.pyfunc.load_model(local_path)

    elapsed = time.perf_counter() - t0
    MODEL_LOAD_DURATION.observe(elapsed)
    MODEL_READY.set(1)

    info_labels = {
        "name": model_name,
        "version": str(mv.version),
        "stage": stage,
        "run_id": mv.run_id or "",
        "checksum_sha256": actual,
        "git_commit": tags.get("git.commit", ""),
        "dataset_hash": tags.get("dataset.hash", ""),
    }
    MODEL_INFO.info(info_labels)

    log.info(
        "model_loaded",
        extra={
            "extra": {
                "event_action": "model.loaded",
                "model": model_name,
                "version": mv.version,
                "stage": stage,
                "run_id": mv.run_id,
                "elapsed_sec": round(elapsed, 3),
                "checksum_sha256": actual,
            }
        },
    )

    return LoadedModel(
        name=model_name,
        version=str(mv.version),
        stage=stage,
        run_id=mv.run_id or "",
        checksum_sha256=actual,
        pyfunc=pyfunc,
        tags=tags,
    )
