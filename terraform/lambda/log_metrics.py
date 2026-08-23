"""Mock Lambda for the LogMetrics step of the MLOps training pipeline.

In a real project this step would call the MLflow tracking server, push
metrics to the Model Registry, or forward results to a monitoring system.
For this homework we only simulate the behaviour.
"""

from __future__ import annotations

import json
from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    print("📈 Logging metrics to MLflow...")
    print(f"Received event: {json.dumps(event, default=str)}")

    # In a real pipeline:
    #   mlflow.log_metric("accuracy", event["accuracy"])
    #   requests.post(f"{MLFLOW_URL}/api/2.0/mlflow/runs/log-metric", ...)
    return {
        "status": "logged",
        "step": "LogMetrics",
        "input": event,
    }
