"""Mock Lambda for the ValidateData step of the MLOps training pipeline.

In a real project this step would validate the incoming payload, check the
schema of the training dataset (e.g. via Great Expectations) or verify that
the required S3 objects exist. Here we keep the logic minimal on purpose --
the goal is to demonstrate the AWS Step Functions state machine wiring.
"""

from __future__ import annotations

import json
from typing import Any


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    print("✅ Validating input data...")
    print(f"Received event: {json.dumps(event, default=str)}")

    # In a real pipeline: JSON schema validation, CSV column checks, etc.
    return {
        "status": "valid",
        "step": "ValidateData",
        "input": event,
    }
