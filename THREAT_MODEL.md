# THREAT_MODEL — goit-mlops

Обсяг: платформа Iris `LogisticRegression` (train → register → serve →
observe → drift). Використовуємо спрощену STRIDE-подібну класифікацію
і перераховуємо лише загрози, для яких мітигація фактично живе
в **цьому** репозиторії.

Оцінка загрози: `Ймовірність × Вплив` (Low / Med / High).

---

## T1 — Отруєний артефакт моделі

**STRIDE**: Tampering.

**Сценарій**: атакувальник з креденшелами MinIO (або скомпрометований
training runner) перезаписує `s3://mlflow-artifacts/<run>/model/`
модифікованим `.pkl` вже після реєстрації версії. Наступний перезапуск
пода підтягує змінений файл і починає обслуговувати модель
під контролем атакувальника.

**Оцінка**: Med × High = **High**.

**Мітигації в цьому репозиторії**

- Тренування рахує SHA256 по відсортованому дереву артефактів
  (`training/mlflow_utils.py::compute_artifact_sha256`) і записує
  його як незмінний тег версії моделі `checksum.sha256`.
- Інференс **fails closed** на старті: `REQUIRE_CHECKSUM=true`
  (усі деплойменти), і будь-яка розбіжність кидає
  `ModelIntegrityError`, встановлює `inference_model_ready` у 0
  і не дозволяє `/readyz` повертати 200. Kubernetes ніколи не додасть
  под до Endpoints Service — трафік не обслуговується.
- PrometheusRule `MLOpsInferenceModelNotReady` (5 хв for) алертить
  саме на цей сценарій.

**Залишковий ризик**: атакувальник, який *також* може змінити тег
`checksum.sha256` версії всередині MLflow, обійде перевірку. Це вимагає
доступу до Postgres MLflow на рівні БД. Мітигація для цього — поза
цим репозиторієм (network policy + Postgres AUTH; див. T4).

---

## T2 — Adversarial / некоректний ввід

**STRIDE**: Denial-of-Service, Elevation-of-Privilege (через витік
внутрішньої інформації в exception).

**Сценарій**: клієнт шле величезні payload-и, необмежені float,
NaN/Inf, зайві поля — щоб покласти процес або витягнути внутрішню
інформацію через stack trace.

**Оцінка**: High × Med = **High**.

**Мітигації**

- Pydantic v2 схема з `model_config.extra = "forbid"` і поверховими
  межами (`ge=0.0, le=15.0`, `PredictRequest` приймає 1..100 instances).
  `inference/app/schemas.py`.
- Глобальні exception-хендлери повертають узагальнене
  `"invalid request"` плюс структурований список `errors` (наданий
  Pydantic) — **без** Python-трейсбеків
  (`inference/app/main.py::validation_error_handler`).
- Slowapi per-IP rate limit (60 rpm production, 120 rpm staging). Клієнт,
  який довбить endpoint, отримає 429 з `Retry-After: 60`, і кожна подія
  логується з `event_action: ratelimit.exceeded`.
- Ingress-nginx `limit-rpm` / `limit-connections` анотації забезпечують
  defence-in-depth, якщо app-side limiter відмовить.
- Обмеження body-size на ingress (`proxy-body-size: 1m`).

**Залишковий ризик**: розподілений абузер (багато IP) впиратиметься у
429 per-IP, але не глобально. Ingress-side rate limiting — лише per-IP;
для реального DDoS потрібні правила Cloudflare.

---

## T3 — Витік креденшелів (S3 / MLflow / kubeconfig)

**STRIDE**: Information Disclosure.

**Сценарій**: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` для MinIO
опиняються у git, у шарі образу або в публічному лозі.

**Оцінка**: Med × High = **High**.

**Мітигації**

- `.pre-commit-config.yaml` запускає **gitleaks** на кожен коміт;
  той самий скан біжить у CI
  (`.github/workflows/ci.yaml::lint`).
- Структуроване JSON-логування маршрутизоване через `logging_config.py`,
  який **не** серіалізує env-змінні.
- Креденшели тягнуться з `Secret`-ів через `secretKeyRef`,
  ніколи через `env: value:`, у кожному workload-маніфесті.
- `kubeconfig` для CI зберігається як Actions secret
  (`KUBECONFIG_B64`) і декодується per-job. Він дає доступ до
  runtime-дій *одного* неймспейсу через `training-sa` RBAC
  (див. `deploy/rbac/roles.yaml`) — це не cluster-admin.

**Шорткат у межах учбового завдання**: об'єкти Secret у цьому репозиторії
несуть plaintext `minio`/`minio123`. Це явно вказано у
`deploy/inference-production/secrets.yaml`. У реальному деплойменті це
має бути SealedSecrets / ExternalSecrets з ротацією за розкладом.

---

## T4 — Несанкціонований запис у registry / MLflow

**STRIDE**: Tampering, Elevation-of-Privilege.

**Сценарій**: атакувальник всередині кластера (припустимо, з сусіднього
застосунку в іншому неймспейсі) дотягується до MLflow і викликає
`create_model_version("iris-classifier", …)` з посиланням на фейковий
артефакт, після чого переводить його у Production.

**Оцінка**: Low × High = **Med**.

**Мітигації**

- NetworkPolicies deny-by-default у `mlops-system` і дозволяють
  ingress лише з `ingress-nginx`, `monitoring`, `mlops-staging`,
  `mlops-production`, `argo`
  (`deploy/networkpolicies/mlops-system.yaml`).
- RBAC:
  - `mlops-engineer` (Role, у staging + system): повний CRUD на
    MLflow-суміжних workload-ах.
  - `mlops-engineer-prod-restricted` (Role, у production): read-only +
    write лише на `Service` і `Ingress` (потрібно для Blue-Green flip) —
    без `exec`, без `delete deploy`, без запису у `secret`.
  - `mlops-viewer` (ClusterRole): чистий read.
  - `training-sa` (Role у `mlops-system`): create/get/list/watch на
    `argoproj.io/workflows,workflowtemplates,clusterworkflowtemplates` —
    достатньо для сабміту Workflow, недостатньо для видалення чи
    редагування шаблонів.
- Registry-переходи виконуються через `promote.py`/`rollback.py`, які:
  1. пишуть JSON-audit-події (`event_action: model.transition`) у Loki
     з полями `actor`, `reason`, `from_version`, `to_version`;
  2. запускаються лише з GitHub Actions workflow-ів
     `mlops-promote` / `mlops-rollback`, що вимагають
     `workflow_dispatch` + обов'язкові інпути (`actor`, `reason`) —
     без unattended-promotion.
- SHA256 checksum-теги (T1) не дозволяють тихо підмінити артефакт
  навіть якщо атакувальник отримає `create_model_version`.

**Залишковий ризик**: MLflow сам по собі не має аутентифікації
(`auth.enabled: false` у values чарта), оскільки обсяг завдання —
один оператор + один кластер. У реальних деплойментах MLflow треба
ставити за SSO (oauth2-proxy) або запускати у приватному mesh.

---

## T5 — Ingress-level DDoS / зловживання

**STRIDE**: Denial-of-Service.

**Сценарій**: скоординований флуд на `iris.squirell.pp.ua`
виснажує CPU нод, витісняє інші workload-и і кладе endpoint.

**Оцінка**: Med × Med = **Med**.

**Мітигації**

- Cloudflare Zero Trust + Cloudflare Tunnel (cloudflared) стоять перед
  ingress. Edge Cloudflare дропає volumetric-атаки до того, як вони
  дістануться кластера; Zero Trust Applications блокують не-автентифікованих
  клієнтів ще до відкриття TCP до cloudflared.
- Ingress-nginx per-Ingress `limit-rpm=600`, `limit-connections=100`
  (production). Staging — `limit-rpm=600`, `limit-connections=50`.
- Application-level slowapi (`REQUESTS_PER_MINUTE`) енфорсить per-IP
  квоти, навіть якщо ingress-limits дадуть збій.
- HPA масштабує `inference-blue`/`inference-green` до 6 реплік
  на колір, тож «легітимний сплеск» не спричиняє падіння, доки
  ліміти дають нам виграти час.
- PDB (`minAvailable: 1` на колір) захищають сервіс від одночасного
  eviction через cluster autoscaler / node drain під час атаки.
- `resources.limits` обмежують CPU/RAM на под, тож поганий payload не
  вирветься і не голодоморить ноду.

**Залишковий ризик**: кластерне вичерпання ресурсів — поза обсягом
цього проєкту. У реальному production ми б додали глобальний
rate-limit на edge Cloudflare та anti-affinity для inference-подів.

---

## T6 — Компрометація Argo Workflows / доступ до Argo Server

**STRIDE**: Elevation-of-Privilege, Tampering.

**Сценарій**: атакувальник дотягується до `argo-workflows-server`
(UI/API у неймспейсі `argo`) і сабмітить довільні Workflow — наприклад,
крок з privileged-контейнером, який маунтить хостову файлову систему
або витягує креденшели з інших неймспейсів.

**Оцінка**: Low × High = **Med**.

**Мітигації**

- `argo-workflows-server` експонується публічно за
  `argo.squirell.pp.ua`, але **позаду Cloudflare Zero Trust
  Application** (email/SSO): жоден HTTP-запит не досягає ingress-nginx
  без валідного `cf-access-jwt-assertion` cookie від Cloudflare Access.
  Всередині кластера сам Service — `ClusterIP`; альтернативно UI
  доступний через `kubectl port-forward` (валідний kubeconfig).
- Контролер `workflowNamespaces: [mlops-system]` — Argo обробляє
  Workflow-и лише з цього неймспейсу. Workflow, сабмічені в
  `default` чи інші неймспейси, ігноруються.
- `WorkflowTemplate iris-train` — єдиний legitimate template,
  версіонований у git; ArgoCD сам ресинхронізує його при спробі
  ручного редагування.
- Workflow-и запускаються під SA `training-sa`, який має обмежені
  RBAC (див. T4) — жодного `secrets` write, жодного доступу до
  інших неймспейсів, окрім `mlops-system`.
- NetworkPolicy у `mlops-system` дозволяє ingress з `argo`
  для викликів MLflow/MinIO, але Argo-server сам ізольований
  дефолтним deny-all для incoming з-поза кластера.
- Аудит: крок `audit` кожного Workflow пише JSON-подію
  `event_action: training.completed` у Loki з `git_commit`, `job_id`,
  `mlflow_run_id` — будь-який fake-запуск буде видно поруч зі списком
  сабмічень у Argo UI.

**Залишковий ризик**: MinIO-креденшели для `artifactRepository`
живуть у неймспейсі `argo` (Secret `argo-workflows-s3`). Компрометація
Argo-контролера → компрометація цих креденшелів → повний доступ до
`mlflow-artifacts` бакета. У виробничому середовищі це варто
винести в ExternalSecrets з IRSA/Workload Identity.

---

## Зведена таблиця

| ID | Загроза                              | Оцінка | Основна мітигація                                |
|----|--------------------------------------|--------|--------------------------------------------------|
| T1 | Отруєний артефакт моделі             | High   | SHA256-тег + fail-closed loader                  |
| T2 | Adversarial input / DoS-by-payload   | High   | Pydantic + slowapi + ingress limits              |
| T3 | Витік креденшелів                    | High   | gitleaks + secretKeyRef only + audit logging     |
| T4 | Несанкціонований запис у registry    | Med    | NetPol + RBAC + audited promote/rollback         |
| T5 | Ingress DDoS                         | Med    | Cloudflare + ingress limits + HPA + PDB          |
| T6 | Компрометація Argo Workflows         | Med    | CF Zero Trust + `workflowNamespaces` + audit     |
