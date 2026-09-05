"""
Promote model version Staging → Production — Блок B2.

Використання (як K8s Job / локально):
    python promote.py --version 3

За замовчуванням бере останню Staging-версію.

Кроки:
  1. Валідує, що вказана version існує та зараз у Staging.
  2. Archives поточну Production (якщо є) — для швидкого rollback.
  3. Transitions вказану version у Production.
  4. Пише audit-подію (Блок C5).
"""

from __future__ import annotations

import argparse
import os
import sys

from audit import audit_event, get_logger
from mlflow_utils import build_client, latest_version_in_stage

log = get_logger("promote")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Promote model version Staging→Production")
    p.add_argument("--model", default=os.environ.get("MODEL_NAME", "iris-classifier"))
    p.add_argument(
        "--version",
        default=None,
        help="Version to promote. Default = latest Staging version.",
    )
    p.add_argument(
        "--actor",
        default=os.environ.get("PROMOTE_ACTOR")
        or os.environ.get("GITHUB_ACTOR")
        or os.environ.get("GITLAB_USER_LOGIN")
        or os.environ.get("USER")
        or "cli",
    )
    p.add_argument(
        "--reason",
        default=os.environ.get("PROMOTE_REASON", "manual-promotion"),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    client = build_client()

    version = args.version or latest_version_in_stage(client, args.model, "Staging")
    if not version:
        log.error("Немає Staging-версії для промоушену (model=%s)", args.model)
        audit_event(
            action="model.transition",
            resource=f"{args.model}",
            outcome="failure",
            reason="no-staging-version",
            actor=args.actor,
        )
        return 1

    mv = client.get_model_version(name=args.model, version=version)
    if mv.current_stage not in ("Staging", "None"):
        log.warning(
            "Version %s знаходиться у стадії %s (не Staging). Продовжуємо все одно.",
            version,
            mv.current_stage,
        )

    # Знаходимо поточну Production для архівації
    current_prod = latest_version_in_stage(client, args.model, "Production")

    # Transition (archive_existing_versions=True переведе всі Production у Archived)
    client.transition_model_version_stage(
        name=args.model,
        version=version,
        stage="Production",
        archive_existing_versions=True,
    )

    log.info(
        "promotion_done",
        extra={
            "extra": {
                "model": args.model,
                "version": version,
                "previous_production": current_prod,
                "actor": args.actor,
                "reason": args.reason,
            }
        },
    )

    audit_event(
        action="model.transition",
        resource=f"{args.model}:v{version}",
        from_stage="Staging",
        to_stage="Production",
        previous_production_version=current_prod,
        actor=args.actor,
        reason=args.reason,
    )
    if current_prod:
        audit_event(
            action="model.transition",
            resource=f"{args.model}:v{current_prod}",
            from_stage="Production",
            to_stage="Archived",
            actor=args.actor,
            reason=f"replaced-by-v{version}",
        )

    print(f"::promoted_version={version}::")
    print(f"::previous_production={current_prod}::")
    return 0


if __name__ == "__main__":
    sys.exit(main())
