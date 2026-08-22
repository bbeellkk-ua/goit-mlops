"""
ДЗ №9 — MLflow experiment tracking + Prometheus PushGateway.

Скрипт:
  1. Виконує серію запусків LogisticRegression на датасеті Iris із різними
     значеннями параметрів C та max_iter.
  2. Для кожного запуску:
       - логує параметри (C, max_iter) та метрики (accuracy, loss) у MLflow;
       - зберігає модель як artifact у MinIO (S3);
       - пушить метрики у Prometheus PushGateway з labels {run_id, run_name}.
  3. Після завершення знаходить run з максимальною accuracy та завантажує його
     model artifact у локальну директорію ../best_model/.

Змінні середовища беруться з файлу .env (див. .env.example).
"""

from __future__ import annotations

import os
import shutil
import sys
from itertools import product
from pathlib import Path

from dotenv import load_dotenv

# Завантажуємо .env ДО імпорту mlflow, щоб MLFLOW_* потрапили в оточення
load_dotenv()

import mlflow
import mlflow.sklearn
from mlflow.artifacts import download_artifacts

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
from sklearn.datasets import load_iris
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.model_selection import train_test_split


# ---------------------------------------------------------------------------
# Конфігурація
# ---------------------------------------------------------------------------

EXPERIMENT_NAME = "Iris Classification HW9"
PUSHGATEWAY_JOB = "mlflow_experiments"

# Сітка гіперпараметрів: 4 x 2 = 8 запусків.
PARAM_GRID = {
    "C": [0.01, 0.1, 1.0, 10.0],
    "max_iter": [100, 500],
}

# Директорія для найкращої моделі (../best_model/ відносно цього файла).
BEST_MODEL_DIR = Path(__file__).resolve().parent.parent / "best_model"


# ---------------------------------------------------------------------------
# Допоміжні функції
# ---------------------------------------------------------------------------

def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"❌ Змінна оточення {name} не задана. Перевірте .env", file=sys.stderr)
        sys.exit(1)
    return value


def ensure_experiment(name: str) -> str:
    """Створює експеримент, якщо його немає. Повертає experiment_id."""
    experiment = mlflow.get_experiment_by_name(name)
    if experiment is None:
        experiment_id = mlflow.create_experiment(name)
        print(f"✅ Створено експеримент '{name}' (ID={experiment_id})")
    else:
        experiment_id = experiment.experiment_id
        print(f"ℹ️  Використовується існуючий експеримент '{name}' (ID={experiment_id})")
    return experiment_id


def push_metrics(
    pushgateway_url: str,
    run_id: str,
    run_name: str,
    accuracy: float,
    loss: float,
) -> None:
    """Пушить метрики експерименту в Prometheus PushGateway.

    Кожен run отримує унікальний grouping key (run_id), тому метрики
    різних запусків не перезаписують одна одну.
    """
    registry = CollectorRegistry()

    accuracy_gauge = Gauge(
        "mlflow_accuracy",
        "Accuracy MLflow-експерименту",
        labelnames=["run_id", "run_name"],
        registry=registry,
    )
    loss_gauge = Gauge(
        "mlflow_loss",
        "Log loss MLflow-експерименту",
        labelnames=["run_id", "run_name"],
        registry=registry,
    )

    accuracy_gauge.labels(run_id=run_id, run_name=run_name).set(accuracy)
    loss_gauge.labels(run_id=run_id, run_name=run_name).set(loss)

    push_to_gateway(
        pushgateway_url,
        job=PUSHGATEWAY_JOB,
        registry=registry,
        # grouping_key дає унікальний ключ для кожного run у PushGateway
        grouping_key={"run_id": run_id},
    )


def train_one(
    C: float,
    max_iter: int,
    X_train,
    X_test,
    y_train,
    y_test,
    experiment_id: str,
    pushgateway_url: str,
) -> tuple[str, str, float, float]:
    """Тренує модель, логує все в MLflow та пушить у PushGateway."""
    run_name = f"C={C}_iter={max_iter}"

    with mlflow.start_run(experiment_id=experiment_id, run_name=run_name) as run:
        run_id = run.info.run_id

        mlflow.log_param("C", C)
        mlflow.log_param("max_iter", max_iter)
        mlflow.set_tag("hw", "lesson-9")
        mlflow.set_tag("model_family", "LogisticRegression")

        model = LogisticRegression(C=C, max_iter=max_iter)
        model.fit(X_train, y_train)

        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)

        acc = accuracy_score(y_test, y_pred)
        loss = log_loss(y_test, y_proba)

        mlflow.log_metric("accuracy", acc)
        mlflow.log_metric("loss", loss)

        # Зберігаємо модель як artifact у MinIO (bucket mlflow-artifacts)
        mlflow.sklearn.log_model(model, artifact_path="model")

        # Push у Prometheus PushGateway
        try:
            push_metrics(pushgateway_url, run_id, run_name, acc, loss)
            print(f"   → метрики запушено в PushGateway ({pushgateway_url})")
        except Exception as exc:  # noqa: BLE001
            print(
                f"   ⚠️  Не вдалося запушити метрики в PushGateway: {exc}",
                file=sys.stderr,
            )

        print(
            f"✅ run {run_name}: accuracy={acc:.4f} loss={loss:.4f} "
            f"(run_id={run_id})"
        )
        return run_id, run_name, acc, loss


def download_best_model(experiment_id: str) -> None:
    """Шукає run з найкращою accuracy та копіює його model artifact у best_model/."""
    print("\n🔎 Пошук найкращого запуску за метрикою accuracy...")

    runs = mlflow.search_runs(
        experiment_ids=[experiment_id],
        order_by=["metrics.accuracy DESC", "metrics.loss ASC"],
        max_results=1,
    )
    if runs.empty:
        print("❌ Не знайдено жодного запуску в експерименті.", file=sys.stderr)
        return

    best_row = runs.iloc[0]
    best_run_id = best_row["run_id"]
    best_acc = best_row["metrics.accuracy"]
    best_loss = best_row["metrics.loss"]

    print(
        f"🏆 Найкращий run: run_id={best_run_id} "
        f"accuracy={best_acc:.4f} loss={best_loss:.4f}"
    )

    # Готуємо чисту директорію
    if BEST_MODEL_DIR.exists():
        for entry in BEST_MODEL_DIR.iterdir():
            if entry.name == ".gitkeep":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        BEST_MODEL_DIR.mkdir(parents=True, exist_ok=True)

    # Завантажуємо artifact-и моделі з MLflow (тобто з MinIO)
    local_path = download_artifacts(
        run_id=best_run_id,
        artifact_path="model",
        dst_path=str(BEST_MODEL_DIR),
    )
    print(f"📦 Модель завантажено у: {local_path}")

    # Пишемо short summary для зручності
    (BEST_MODEL_DIR / "BEST_RUN.txt").write_text(
        "Best MLflow run for experiment "
        f"'{EXPERIMENT_NAME}'\n"
        f"run_id:   {best_run_id}\n"
        f"accuracy: {best_acc:.6f}\n"
        f"loss:     {best_loss:.6f}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    tracking_uri = require_env("MLFLOW_TRACKING_URI")
    pushgateway_url = require_env("PUSHGATEWAY_URL")
    # Наступні змінні MLflow бере з оточення сам (для доступу до MinIO):
    require_env("AWS_ACCESS_KEY_ID")
    require_env("AWS_SECRET_ACCESS_KEY")
    require_env("MLFLOW_S3_ENDPOINT_URL")

    mlflow.set_tracking_uri(tracking_uri)
    print(f"🔗 MLflow Tracking URI: {tracking_uri}")
    print(f"🔗 PushGateway URL:     {pushgateway_url}")

    experiment_id = ensure_experiment(EXPERIMENT_NAME)

    X, y = load_iris(return_X_y=True)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )

    combos = list(product(PARAM_GRID["C"], PARAM_GRID["max_iter"]))
    print(f"\n🚀 Запускаємо {len(combos)} експериментів...\n")

    results: list[tuple[str, str, float, float]] = []
    for C, max_iter in combos:
        result = train_one(
            C=C,
            max_iter=max_iter,
            X_train=X_train,
            X_test=X_test,
            y_train=y_train,
            y_test=y_test,
            experiment_id=experiment_id,
            pushgateway_url=pushgateway_url,
        )
        results.append(result)

    # Красивий підсумок по всім запускам
    print("\n📊 Підсумок запусків:")
    print(f"   {'run_name':<25} {'accuracy':>10} {'loss':>10}")
    for _, run_name, acc, loss in sorted(results, key=lambda r: -r[2]):
        print(f"   {run_name:<25} {acc:>10.4f} {loss:>10.4f}")

    download_best_model(experiment_id)

    print("\n✅ Готово. Перевірте MLflow UI та Grafana Explore.")


if __name__ == "__main__":
    main()
