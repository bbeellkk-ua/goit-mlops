"""
Rollback Production до попередньої версії — Блок B4.

Використання:
    python rollback.py                       # автовизначення попередньої (найновіша Archived)
    python rollback.py --to-version 2        # явно

Логіка:
  1. Знаходимо поточну Production version (X) і цільову T.
     Якщо --to-version не задано — беремо max(Archived).
  2. Archives поточну X.
  3. Transitions T назад у Production.
  4. Audit event.
"""

from __future__ import annotations

import argparse
import os
import sys

from audit import audit_event, get_logger
from mlflow_utils import build_client, latest_version_in_stage

log = get_logger("rollback")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rollback Production → previous version")
    p.add_argument("--model", default=os.environ.get("MODEL_NAME", "iris-classifier"))
    p.add_argument("--to-version", default=None)
    p.add_argument(
        "--actor",
        default=os.environ.get("GITHUB_ACTOR")
        or os.environ.get("GITLAB_USER_LOGIN")
        or os.environ.get("USER")
        or "cli",
    )
    p.add_argument("--reason", default="manual-rollback")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    client = build_client()

    current_prod = latest_version_in_stage(client, args.model, "Production")
    if not current_prod:
        log.error("Немає активної Production-версії, немає що rollback-ати.")
        return 1

    target = args.to_version
    if target is None:
        target = latest_version_in_stage(client, args.model, "Archived")
    if not target:
        log.error("Немає жодної Archived-версії — rollback неможливий.")
        return 1
    if str(target) == str(current_prod):
        log.error("Target version = current Production. Nothing to do.")
        return 1

    # Спочатку archive current
    client.transition_model_version_stage(
        name=args.model,
        version=current_prod,
        stage="Archived",
        archive_existing_versions=False,
    )
    # Потім target -> Production
    client.transition_model_version_stage(
        name=args.model,
        version=target,
        stage="Production",
        archive_existing_versions=False,
    )

    log.info(
        "rollback_done",
        extra={
            "extra": {
                "model": args.model,
                "from_version": current_prod,
                "to_version": target,
                "actor": args.actor,
                "reason": args.reason,
            }
        },
    )

    audit_event(
        action="model.transition",
        resource=f"{args.model}:v{current_prod}",
        from_stage="Production",
        to_stage="Archived",
        actor=args.actor,
        reason=f"rollback-to-v{target}",
    )
    audit_event(
        action="model.transition",
        resource=f"{args.model}:v{target}",
        from_stage="Archived",
        to_stage="Production",
        actor=args.actor,
        reason=args.reason,
    )

    print(f"::rolled_back_from={current_prod}::")
    print(f"::rolled_back_to={target}::")
    return 0


if __name__ == "__main__":
    sys.exit(main())
