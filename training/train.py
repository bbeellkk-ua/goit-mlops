"""
Training pipeline — Блок B1.

Обов'язки:
  * зчитати конфігурацію з ENV (тренінг у K8s Job → всі параметри через env);
  * натренувати `LogisticRegression` на Iris;
  * записати в MLflow параметри, метрики, tags (git.commit, dataset.hash),
    залогувати модель як artifact у MinIO;
  * зареєструвати нову версію моделі в Registry під назвою
    `iris-classifier` та перевести її у Stage=Staging;
  * обчислити SHA256 артефакту (Блок C4) і записати у model-version tags;
  * зробити audit-запис (Блок C5).

Опціональні ENV:
  MODEL_NAME              = "iris-classifier"
  LR_C                    = "1.0"
  LR_MAX_ITER             = "500"
  RANDOM_STATE            = "42"
  TEST_SIZE               = "0.2"
  GIT_COMMIT              (з CI)
  GIT_BRANCH              (з CI)
  DATASET_VERSION         = "iris-sklearn-2024"
"""

from __future__ import annotations

import os
import sys

import mlflow
import mlflow.sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, log_loss

from audit import audit_event, get_logger
from mlflow_utils import build_client, compute_artifact_sha256, tag_model_version_checksum
from preprocessing import (
    CLASS_NAMES,
    dataset_hash,
    load_dataset,
    split_dataset,
    summarize_dataset,
)

log = get_logger("training")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        log.warning("Некоректне значення %s, беру default=%s", name, default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        log.warning("Некоректне значення %s, беру default=%s", name, default)
        return default


def main() -> int:
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    if not tracking_uri:
        log.error("MLFLOW_TRACKING_URI не заданий — training неможливий")
        return 2

    model_name = os.environ.get("MODEL_NAME", "iris-classifier")
    experiment_name = os.environ.get("MLFLOW_EXPERIMENT_NAME", "iris-training")

    c = _env_float("LR_C", 1.0)
    max_iter = _env_int("LR_MAX_ITER", 500)
    random_state = _env_int("RANDOM_STATE", 42)
    test_size = _env_float("TEST_SIZE", 0.2)

    git_commit = os.environ.get("GIT_COMMIT", "local")
    git_branch = os.environ.get("GIT_BRANCH", "local")
    dataset_version = os.environ.get("DATASET_VERSION", "iris-sklearn-2024")

    log.info(
        "training_start",
        extra={
            "extra": {
                "model_name": model_name,
                "experiment": experiment_name,
                "git_commit": git_commit,
                "git_branch": git_branch,
                "dataset_version": dataset_version,
                "params": {"C": c, "max_iter": max_iter},
            }
        },
    )

    # -- data --------------------------------------------------------------
    X, y = load_dataset()
    ds_hash = dataset_hash(X, y)
    summary = summarize_dataset(X, y)
    log.info("dataset_loaded", extra={"extra": {"hash": ds_hash, **summary}})

    X_train, X_test, y_train, y_test = split_dataset(
        X, y, test_size=test_size, random_state=random_state
    )

    # -- MLflow ------------------------------------------------------------
    client = build_client()
    mlflow.set_experiment(experiment_name)

    with mlflow.start_run(run_name=f"iris-{git_commit[:8]}") as run:
        run_id = run.info.run_id

        # params + tags
        mlflow.log_params({"C": c, "max_iter": max_iter, "random_state": random_state})
        mlflow.set_tags(
            {
                "git.commit": git_commit,
                "git.branch": git_branch,
                "dataset.version": dataset_version,
                "dataset.hash": ds_hash,
                "framework": "sklearn",
                "model_family": "LogisticRegression",
            }
        )

        # train
        model = LogisticRegression(C=c, max_iter=max_iter, random_state=random_state)
        model.fit(X_train, y_train)

        # metrics
        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)
        acc = accuracy_score(y_test, y_pred)
        f1 = f1_score(y_test, y_pred, average="macro")
        loss = log_loss(y_test, y_proba, labels=list(range(len(CLASS_NAMES))))
        mlflow.log_metrics({"accuracy": acc, "f1_macro": f1, "log_loss": loss})

        log.info(
            "training_metrics",
            extra={
                "extra": {
                    "run_id": run_id,
                    "accuracy": acc,
                    "f1_macro": f1,
                    "log_loss": loss,
                }
            },
        )

        # log & register model
        signature = mlflow.models.infer_signature(X_train, y_pred)
        mlflow.sklearn.log_model(
            sk_model=model,
            artifact_path="model",
            signature=signature,
            registered_model_name=model_name,
            input_example=X_train.head(2),
        )

        # знаходимо створену версію
        versions = client.search_model_versions(f"name='{model_name}' and run_id='{run_id}'")
        if not versions:
            log.error("Не вдалось знайти нову model-version — реєстрація не відбулась")
            return 3
        new_version = max(versions, key=lambda v: int(v.version))

        # ------- SHA256 checksum (Блок C4) -------------------------------
        checksum = compute_artifact_sha256(run_id=run_id, artifact_path="model")
        tag_model_version_checksum(client, model_name, new_version.version, checksum)

        # description + додаткові tags для трасування
        client.update_model_version(
            name=model_name,
            version=new_version.version,
            description=(
                f"Trained on {dataset_version}\n"
                f"Git: {git_branch}@{git_commit}\n"
                f"Metrics: accuracy={acc:.4f}, f1_macro={f1:.4f}, log_loss={loss:.4f}"
            ),
        )
        for k, v in {
            "git.commit": git_commit,
            "git.branch": git_branch,
            "dataset.version": dataset_version,
            "dataset.hash": ds_hash,
            "metrics.accuracy": f"{acc:.6f}",
            "metrics.f1_macro": f"{f1:.6f}",
            "metrics.log_loss": f"{loss:.6f}",
        }.items():
            client.set_model_version_tag(model_name, new_version.version, k, v)

        # ------- transition -> Staging ----------------------------------
        client.transition_model_version_stage(
            name=model_name,
            version=new_version.version,
            stage="Staging",
            archive_existing_versions=False,
        )

        log.info(
            "model_registered",
            extra={
                "extra": {
                    "model_name": model_name,
                    "version": new_version.version,
                    "run_id": run_id,
                    "stage": "Staging",
                    "checksum_sha256": checksum,
                }
            },
        )

        audit_event(
            action="model.register",
            resource=f"{model_name}:v{new_version.version}",
            git_commit=git_commit,
            git_branch=git_branch,
            run_id=run_id,
            dataset_hash=ds_hash,
            checksum_sha256=checksum,
            metrics={"accuracy": acc, "f1_macro": f1, "log_loss": loss},
        )
        audit_event(
            action="model.transition",
            resource=f"{model_name}:v{new_version.version}",
            from_stage="None",
            to_stage="Staging",
            reason="auto-after-training",
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
