SHELL := /bin/bash

# =============================================================================
# goit-mlops — Makefile
#
# Ціль — надати чіткий, послідовний набір команд для роботи з проєктом.
# Розбито на логічні фази життєвого циклу:
#
#   1) local-dev     — venv / lint / test / build (для розробника, без кластера)
#   2) infra         — bootstrap-послідовність ArgoCD Applications (у порядку)
#   3) train         — сабмітимо Argo WorkflowTemplate iris-train (in-cluster)
#   4) deploy-staging — накатуємо inference-staging + rolling-restart
#   5) promote       — сабмітимо iris-promote (Staging → Production у registry)
#   6) deploy-prod   — накатуємо inference-production (blue+green) + rolling-restart
#   7) deploy-evidently — накатуємо drift CronWorkflow
#
# Багато цільових команд викликають `argo submit` — сам binary тут не потрібен,
# але зручний. Fallback через `kubectl create -f ...` теж працює.
# =============================================================================

# -----------------------------------------------------------------------------
# Загальні змінні
# -----------------------------------------------------------------------------
IMG_REGISTRY   ?= ghcr.io/bbeellkk-ua
TAG            ?= dev

# For train / promote / rollback: параметри Argo Workflows
NAMESPACE_SYSTEM ?= mlops-system
NAMESPACE_STG    ?= mlops-staging
NAMESPACE_PROD   ?= mlops-production

# Метадані для audit-подій
ACTOR  ?= $(shell whoami)
REASON ?= manual-run

.PHONY: help
help:
	@echo ""
	@echo "goit-mlops — команди по фазам життєвого циклу"
	@echo ""
	@echo "── 1. Local dev ─────────────────────────────────────────────────"
	@echo "  make venv                    створити .venv (Python 3.12)"
	@echo "  make install                 залежності training + inference + dev"
	@echo "  make lint                    ruff + yamllint"
	@echo "  make fmt                     ruff format --fix"
	@echo "  make test                    pytest"
	@echo "  make build-inference         docker build inference image"
	@echo "  make build-training          docker build training image"
	@echo "  make build-evidently         docker build evidently image"
	@echo ""
	@echo "── 2. Infrastructure (bootstrap ArgoCD Applications) ───────────"
	@echo "  make infra-namespaces        00-namespaces + 10-rbac"
	@echo "  make infra-platform          20-mlops-system + 25-argo-workflows"
	@echo "  make infra-observability     30-monitoring + 35-networkpolicies"
	@echo "  make infra                   ↑ усе інфраструктурне разом (у правильному порядку)"
	@echo ""
	@echo "── 3. Training (in-cluster via Argo WorkflowTemplate iris-train) "
	@echo "  make train                   submit new training run"
	@echo "  make train-status            останні training-workflows"
	@echo "  make train-logs              логи останнього training run"
	@echo ""
	@echo "── 4. Staging deploy ───────────────────────────────────────────"
	@echo "  make deploy-staging          apply ArgoCD app 40-inference-staging"
	@echo "  make refresh-staging         rolling-restart staging pods (щоб підхопили нову Staging-версію моделі)"
	@echo "  make smoke-staging           curl проти iris-staging.squirell.pp.ua"
	@echo ""
	@echo "── 5. Promote (Staging → Production у MLflow Registry) ─────────"
	@echo "  make promote VERSION=5 [REASON=...] [ACTOR=...]"
	@echo "                               submit Argo WorkflowTemplate iris-promote"
	@echo "  make rollback [TO_VERSION=3] [REASON=...] [ACTOR=...]"
	@echo "                               submit Argo WorkflowTemplate iris-rollback"
	@echo ""
	@echo "── 6. Production deploy (Blue-Green) ───────────────────────────"
	@echo "  make deploy-prod             apply ArgoCD app 50-inference-production"
	@echo "  make refresh-prod            rolling-restart both blue + green"
	@echo "  make smoke-prod              curl проти iris.squirell.pp.ua"
	@echo ""
	@echo "── 7. Evidently drift detection ────────────────────────────────"
	@echo "  make deploy-evidently        apply ArgoCD app 60-evidently"
	@echo "  make drift-run               submit разовий drift-run (позаплановий)"
	@echo "  make drift-logs              логи останнього drift-run"
	@echo ""
	@echo "── Utility ─────────────────────────────────────────────────────"
	@echo "  make status                  огляд ArgoCD-apps + подів"
	@echo "  make teardown                видалити всі goit-mlops Applications"
	@echo ""

# =============================================================================
# 1. LOCAL DEV
# =============================================================================
.PHONY: venv install lint fmt test build-inference build-training build-evidently

venv:
	python3.12 -m venv .venv
	@echo "→ activate: source .venv/bin/activate"

install:
	python -m pip install -U pip
	python -m pip install -r inference/requirements.txt
	python -m pip install -r training/requirements.txt
	python -m pip install ruff pytest pre-commit yamllint

lint:
	ruff check .
	yamllint -c .yamllint.yaml deploy/ argocd/ .github/ 2>/dev/null || true

fmt:
	ruff format .
	ruff check --fix .

test:
	pytest -q

build-inference:
	docker build -t $(IMG_REGISTRY)/goit-mlops-inference:$(TAG) inference

build-training:
	docker build -t $(IMG_REGISTRY)/goit-mlops-training:$(TAG) training

build-evidently:
	docker build -t $(IMG_REGISTRY)/goit-mlops-evidently:$(TAG) evidently

# =============================================================================
# 2. INFRASTRUCTURE (ArgoCD apps, у порядку залежностей)
# =============================================================================
.PHONY: infra infra-namespaces infra-platform infra-observability

infra-namespaces:
	kubectl apply -f argocd/apps/00-namespaces.yaml
	kubectl apply -f argocd/apps/10-rbac.yaml
	@echo "→ дочекайтесь Sync/Healthy у mlops-namespaces + mlops-rbac"

infra-platform:
	kubectl apply -f argocd/apps/20-mlops-system.yaml
	@echo "→ дочекайтесь Sync/Healthy у mlops-system (MLflow, MinIO, Postgres, Pushgateway,"
	@echo "  а також in-cluster WorkflowTemplates iris-train / iris-promote / iris-rollback)"
	kubectl apply -f argocd/apps/25-argo-workflows.yaml
	@echo "→ дочекайтесь Sync/Healthy у mlops-argo-workflows"

infra-observability:
	kubectl apply -f argocd/apps/30-monitoring.yaml
	kubectl apply -f argocd/apps/35-networkpolicies.yaml

infra: infra-namespaces infra-platform infra-observability
	@echo ""
	@echo "✅ Інфраструктура задеплоєна. Наступний крок — 'make train' щоб зареєструвати першу версію моделі."

# =============================================================================
# 3. TRAINING (in-cluster via Argo WorkflowTemplate)
#
# Ми НЕ використовуємо GitHub Actions для запуску тренування, бо GH runners
# знаходяться поза VPN і не мають доступу до Kubernetes API нашого приватного
# кластера. Замість цього тренування виконує Argo Workflows у самому кластері,
# де MLflow / MinIO доступні напряму через ClusterIP. (Якщо є self-hosted
# runner у кластері з kubectl-доступом — можна тригерити ті самі submits через GHA.)
# =============================================================================
.PHONY: train train-status train-logs

train:
	@echo "→ Submitting Argo WorkflowTemplate iris-train..."
	argo -n $(NAMESPACE_SYSTEM) submit --from workflowtemplate/iris-train \
		--generate-name iris-train-manual- \
		-p git-commit="$$(git rev-parse HEAD 2>/dev/null || echo local)" \
		-p git-branch="$$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo local)" \
		-p job-id="manual-$$(date +%s)" \
		--serviceaccount training-sa \
		--wait
	@echo "→ Тренування завершено. Нова версія моделі має бути у Staging (див. MLflow UI)."

train-status:
	argo -n $(NAMESPACE_SYSTEM) list --prefix iris-train

train-logs:
	argo -n $(NAMESPACE_SYSTEM) logs @latest

# =============================================================================
# 4. STAGING DEPLOY
# =============================================================================
.PHONY: deploy-staging refresh-staging smoke-staging

deploy-staging:
	kubectl apply -f argocd/apps/40-inference-staging.yaml
	@echo "→ ArgoCD синхронізує mlops-inference-staging."

# Inference-сервіс завантажує модель з MLflow ЛИШЕ при старті пода. Тому після
# нового тренування треба зробити rolling-restart, щоб staging почав віддавати
# свіжу Staging-версію.
refresh-staging:
	kubectl -n $(NAMESPACE_STG) rollout restart deployment/inference
	kubectl -n $(NAMESPACE_STG) rollout status deployment/inference --timeout=180s

smoke-staging:
	@echo "→ Smoke-test staging (публічний хост Cloudflare Zero Trust):"
	curl -sf https://iris-staging.squirell.pp.ua/healthz | jq
	curl -sf https://iris-staging.squirell.pp.ua/readyz  | jq
	curl -sf https://iris-staging.squirell.pp.ua/model   | jq
	curl -sfX POST https://iris-staging.squirell.pp.ua/predict \
		-H 'content-type: application/json' \
		-d '{"features":[5.1,3.5,1.4,0.2]}' | jq

# =============================================================================
# 5. PROMOTE / ROLLBACK (in-cluster via Argo WorkflowTemplate)
# =============================================================================
.PHONY: promote rollback

# Usage: make promote VERSION=5 REASON="v5 verified in staging" ACTOR=oleksandr
promote:
	@if [ -z "$(VERSION)" ]; then \
		echo "→ VERSION не задано — буде взяти latest Staging"; \
	fi
	argo -n $(NAMESPACE_SYSTEM) submit --from workflowtemplate/iris-promote \
		--generate-name iris-promote-manual- \
		$(if $(VERSION),-p version="$(VERSION)") \
		-p actor="$(ACTOR)" \
		-p reason="$(REASON)" \
		--serviceaccount training-sa \
		--wait

# Usage: make rollback TO_VERSION=3 REASON="v4 SLA breach"
rollback:
	argo -n $(NAMESPACE_SYSTEM) submit --from workflowtemplate/iris-rollback \
		--generate-name iris-rollback-manual- \
		$(if $(TO_VERSION),-p to-version="$(TO_VERSION)") \
		-p actor="$(ACTOR)" \
		-p reason="$(REASON)" \
		--serviceaccount training-sa \
		--wait

# =============================================================================
# 6. PRODUCTION DEPLOY (Blue-Green)
# =============================================================================
.PHONY: deploy-prod refresh-prod smoke-prod

deploy-prod:
	kubectl apply -f argocd/apps/50-inference-production.yaml
	@echo "→ ArgoCD синхронізує mlops-inference-production (blue + green)."
	@echo "   Nb: selfHeal=false для прода — після ручної правки Service/Ingress"
	@echo "   під час blue-green flip зроби 'argocd app sync mlops-inference-production'."

# Rolling-restart обох кольорів так, щоб кожен зачитав актуальну Production-версію
# з MLflow. У Blue-Green продакшн-процедурі краще робити це послідовно:
# спочатку неактивний колір → smoke-test через preview-Service → потім активний.
refresh-prod:
	kubectl -n $(NAMESPACE_PROD) rollout restart deployment/inference-blue
	kubectl -n $(NAMESPACE_PROD) rollout restart deployment/inference-green
	kubectl -n $(NAMESPACE_PROD) rollout status deployment/inference-blue  --timeout=180s
	kubectl -n $(NAMESPACE_PROD) rollout status deployment/inference-green --timeout=180s

smoke-prod:
	@echo "→ Smoke-test production:"
	curl -sf https://iris.squirell.pp.ua/healthz | jq
	curl -sf https://iris.squirell.pp.ua/readyz  | jq
	curl -sf https://iris.squirell.pp.ua/model   | jq
	curl -sfX POST https://iris.squirell.pp.ua/predict \
		-H 'content-type: application/json' \
		-d '{"features":[5.1,3.5,1.4,0.2]}' | jq

# =============================================================================
# 7. EVIDENTLY (drift detection via Argo CronWorkflow)
# =============================================================================
.PHONY: deploy-evidently drift-run drift-logs

deploy-evidently:
	kubectl apply -f argocd/apps/60-evidently.yaml

drift-run:
	argo -n $(NAMESPACE_SYSTEM) submit --from workflowtemplate/iris-drift \
		--generate-name mlops-drift-manual- \
		--wait

drift-logs:
	argo -n $(NAMESPACE_SYSTEM) logs @latest

# =============================================================================
# UTILITY
# =============================================================================
.PHONY: status teardown

status:
	@echo "=== ArgoCD Applications ==="
	kubectl -n argocd get applications
	@echo ""
	@echo "=== Namespaces ==="
	kubectl get ns $(NAMESPACE_SYSTEM) $(NAMESPACE_STG) $(NAMESPACE_PROD) 2>&1 || true
	@echo ""
	@echo "=== Pods ==="
	kubectl -n $(NAMESPACE_SYSTEM) get pods 2>&1 || true
	kubectl -n $(NAMESPACE_STG)    get pods 2>&1 || true
	kubectl -n $(NAMESPACE_PROD)   get pods 2>&1 || true

teardown:
	@echo "⚠️  Це видалить всі Applications і namespace-и goit-mlops."
	@read -r -p "Впевнені? [yes/N] " ans && [[ "$$ans" == "yes" ]] || (echo "abort"; exit 1)
	kubectl -n argocd delete application \
		mlops-namespaces mlops-rbac mlops-system mlops-argo-workflows mlops-monitoring \
		mlops-networkpolicies mlops-inference-staging \
		mlops-inference-production mlops-evidently \
		--ignore-not-found
	kubectl delete namespace $(NAMESPACE_SYSTEM) $(NAMESPACE_STG) $(NAMESPACE_PROD) --ignore-not-found
	@echo "✅ teardown complete."
