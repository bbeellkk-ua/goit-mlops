# goit-mlops — Фінальний проєкт

Production-ready MLOps-платформа для класифікатора Iris (`LogisticRegression`),
розгорнута на існуючому k3s-кластері (`mlops-k3s`) через ArgoCD. Проєкт
покриває всі шість блоків завдання (**A / B / C / D / E / F**).

**Модель:** sklearn `LogisticRegression` на датасеті Iris (150 записів, 4
ознаки, 3 класи). Датасет обрано свідомо — див. секцію
[«Чому Iris»](#чому-iris-і-чому-це-нормально).

**Інфраструктура:** існуючий k3s-кластер + ArgoCD + kube-prometheus-stack +
Loki. Terraform / EKS **не використовувалися** свідомо — див. секцію
[«Чому існуючий кластер, а не Terraform/EKS»](#чому-існуючий-кластер-а-не-terraformeks).

**Оркестрація тренування / promote / rollback:** **Argo Workflows у самому
кластері** (`WorkflowTemplate iris-train`, `iris-promote`, `iris-rollback`).
Тренування, промоушен і rollback виконуються *не* з GitHub Actions
runner-ів, а безпосередньо у kubernetes, бо GH public runners не мають
мережевого доступу до нашого приватного API-сервера — детально у секції
[«CI / CD»](#ci--cd). Обґрунтування вибору Argo — секція
[«Argo Workflows: плюси, мінуси та чому саме воно»](#argo-workflows-плюси-мінуси-та-чому-саме-воно).

---

## Зміст

1. [Порядок реалізації](#порядок-реалізації)
2. [Архітектура](#архітектура)
3. [Ключові рішення (і чому вони саме такі)](#ключові-рішення-і-чому-вони-саме-такі)
   - [Чому існуючий кластер, а не Terraform/EKS](#чому-існуючий-кластер-а-не-terraformeks)
   - [Чому Iris (і чому це нормально)](#чому-iris-і-чому-це-нормально)
   - [Argo Workflows: плюси, мінуси та чому саме воно](#argo-workflows-плюси-мінуси-та-чому-саме-воно)
   - [Blue-Green замість Canary](#blue-green-замість-canary)
4. [Структура репозиторію](#структура-репозиторію)
5. [DNS, TLS та Cloudflare Zero Trust](#dns-tls-та-cloudflare-zero-trust)
6. [Передумови](#передумови)
7. [Bootstrap-послідовність (Блок A)](#bootstrap-послідовність-блок-a)
8. [Локальна розробка](#локальна-розробка)
9. [Спостережуваність](#спостережуваність)
10. [CI / CD](#ci--cd)
11. [Рольова модель (Блок C3)](#рольова-модель-блок-c3)
12. [Доступність кластера для перевірки](#доступність-кластера-для-перевірки)
13. [Teardown](#teardown)
14. [Пов'язана документація](#повязана-документація)

---

## Порядок реалізації

Проєкт побудовано у такому порядку — це відображено і у структурі гілок,
і у номерах ArgoCD Applications (`00…60`):

| # | Блок | Що робимо | Артефакти |
|---|------|-----------|-----------|
| 1 | **A1–A4** | Базова інфраструктура: ArgoCD Applications → MLflow + MinIO + PostgreSQL + Argo Workflows + kube-prometheus-stack + Loki + Pushgateway | `argocd/apps/{00,10,20,25,30}-*.yaml`, `deploy/mlops-system/*`, `deploy/monitoring/*` |
| 2 | **C1–C2 + A5** | Inference-сервіс FastAPI: Pydantic v2 (`extra=forbid`), slowapi rate-limit, Prometheus-метрики, JSON-логи, SHA256-перевірка моделі, healthz/readyz/predict | `inference/app/*`, `deploy/inference-{staging,production}/*` |
| 3 | **B1–B3** | Training / promote / rollback як **in-cluster Argo WorkflowTemplates** (`iris-train`, `iris-promote`, `iris-rollback`) + MLflow Model Registry + `train.py`, `promote.py`, `rollback.py`, аудит-події | `training/*`, `deploy/mlops-system/{train,promote,rollback}-workflow-template.yaml` |
| 4 | **C3–C4 + D** | RBAC (viewer / engineer / prod-restricted), NetworkPolicies, gitleaks, THREAT_MODEL, ADR, RUNBOOK, ця README | `deploy/rbac/*`, `deploy/networkpolicies/*`, `THREAT_MODEL.md`, `ADR.md`, `RUNBOOK.md` |
| 5 | **E** | Evidently drift-детекція: Argo `CronWorkflow` `mlops-drift` кожні 6 год → DAG `fetch → compute → publish` → Pushgateway → PrometheusRule `MLOpsDriftDetected` | `evidently/*`, `deploy/evidently/drift-cronworkflow.yaml`, `deploy/monitoring/prometheus-rules.yaml` |
| 6 | **F** | CI quality-gates у GitHub Actions: ruff + yamllint + gitleaks + pytest + Trivy build/push → GHCR | `.github/workflows/ci.yaml`, `pyproject.toml`, `.pre-commit-config.yaml` |

---

## Архітектура

```
                                       ┌─────────────────────────────────┐
                                       │  GitHub bbeellkk-ua/goit-mlops   │
                                       │        (гілка: final)           │
                                       └───────────┬─────────────────────┘
                                                   │  git push
                                                   ▼
                               ┌──────────────────────────────────────┐
                               │  GitHub Actions (public runners)     │
                               │  lint • pytest • buildx → GHCR       │
                               │  (НЕ має доступу до k8s API)         │
                               └───────────┬──────────────────────────┘
                                           │ image push
                                           ▼         ┌─── argo submit (in-cluster)
                                        ghcr.io      │
                                           │         │
                                           ▼         │
┌──────────────────────────────────────────────────────────────────────────┐
│                    k3s-кластер `mlops-k3s` (існуючий)                    │
│                                                                          │
│  ┌───────────────┐  ┌─────────────────────┐  ┌───────────────────────┐   │
│  │ argocd/apps/  │  │ mlops-system        │  │ monitoring (існуючий) │   │
│  │ 00..60        │─▶│  MLflow (Postgres)  │  │  kube-prometheus-stack│   │
│  │ Applications  │  │  MinIO              │  │  Grafana + Loki       │   │
│  └───────────────┘  │  Pushgateway        │  │  Promtail             │   │
│                     │  Argo Workflows     │  └───────────┬───────────┘   │
│                     │  (train DAG)        │              │               │
│                     │  evidently CronJob  │              │               │
│                     └────────┬────────────┘              │               │
│                              │                           │               │
│                              ▼                           ▼               │
│              ┌──────────────────────────┐   ┌─────────────────────────┐  │
│              │  mlops-staging           │   │  mlops-production       │  │
│              │  inference (rolling)     │   │  inference-blue         │  │
│              │  iris-staging.squirell…  │   │  inference-green        │  │
│              │                          │   │  Service selector=blue  │  │
│              └────────┬─────────────────┘   └─────────┬───────────────┘  │
│                       │  ingress-nginx / cert-manager │                  │
└───────────────────────┼───────────────────────────────┼──────────────────┘
                        ▼                               ▼
              
    (Cloudflare Zero Trust Application → cloudflared → ingress-nginx → Service)
```

---

## Ключові рішення

### Чому існуючий кластер, а не Terraform/EKS

**Рішення.** Інфраструктура (k3s + ArgoCD + kube-prometheus-stack + Loki +
ingress-nginx + cert-manager + Cloudflare Tunnel + Cloudflare Zero Trust)
розгорнута **окремо** — поза цим репозиторієм, у моєму особистому
середовищі. У цьому репо немає Terraform-модулів під EKS.

**Причини.**

- **Не хочу подвоювати рахунок за AWS.** У мене є працюючий, повноцінно
  налаштований k3s-кластер з усіма компонентами, потрібними для завдання
  (ArgoCD, Prometheus, Loki, ingress, cert-manager, Cloudflare Tunnel,
  Cloudflare Zero Trust Applications). Розгортати EKS «на 24 години
  перевірки» — це витрачені гроші й час без жодної технічної переваги
  для перевіряючого.
- **Такий підхід прийнятний згідно з умовами завдання** — головна вимога
  це доступність кластера для перевірки протягом 24 годин після здачі.
  Кластер буде доступний — див. секцію
  [«Доступність кластера для перевірки»](#доступність-кластера-для-перевірки).
- **Terraform-код для EKS існує в окремій гілці цього ж репозиторію** —
  [`goit-mlops/tree/eks-terraform`](https://github.com/bbeellkk-ua/goit-mlops/tree/eks-terraform).
  Це базовий `terraform-aws-modules/eks/aws` + VPC-модуль з VPC-endpoints —
  стандартна річ, тримати її в основній гілці разом з робочим k3s-деплойем
  не мало сенсу.
- **Kubernetes API однаковий.** Все, що у цьому репо задеклароване через
  ArgoCD (`argocd/apps/*.yaml`) + маніфести у `deploy/*/`, застосовується
  до **будь-якого** k8s-кластера без змін — переніс на EKS/GKE/AKS
  зводиться до зміни `destination.server` та StorageClass у PVC.

---

### Чому Iris

**Рішення.** Модель — `sklearn.linear_model.LogisticRegression` на датасеті
Iris (150 записів, 4 ознаки, 3 класи).

**Обґрунтування.**

- Завдання явно каже, що **простий sklearn-класифікатор достатній** — мета
  курсу це MLOps-платформа, не складна модель.
- Iris **корисний саме тому, що він простий**: тренування триває < 1 секунди
  локально, що дає мені змогу зосередитися на:
  - MLflow Registry (реєстрація + tag `checksum.sha256` + промоція між
    stage), а не на GPU-плануванні;
  - контракті `Pydantic` (обмеження `ge=0.0, le=15.0` — семантично
    осмислені саме тому, що я знаю розподіл сепал/пелюстків Iris);
  - drift-детекції на Evidently (референс-датасет = training split; це
    робочий приклад PSI поверх стабільного baseline);
  - SHA256-перевірці артефакту (модель ~2 KB — можу без болю читати
    та порівнювати hash у тестах).
- Якщо завтра замінити `LogisticRegression` на `XGBoost` чи NN — усі 6 блоків
  залишаться без змін, зміниться лише вміст `training/train.py::train()`.

---

### Argo Workflows: плюси, мінуси та чому саме воно

**Рішення.** Тренування виконується як `Workflow` під керуванням
**Argo Workflows** — а не як простий `batch/v1 Job`. Шаблон описаний у
`deploy/mlops-system/train-workflow-template.yaml`
(`WorkflowTemplate iris-train`), CI підставляє параметри у submission-манифест
`training/train-workflow.tpl.yaml`.

**Пайплайн (DAG):**

```
                     ┌────────┐
                     │ train  │  <- виконує training/train.py у контейнері
                     └───┬────┘     (той самий образ ghcr.io/…/goit-mlops-training)
                         ▼
             ┌─────────────────────────┐
             │  verify-registration    │  <- Python: MLflow REST перевіряє,
             └───────────┬─────────────┘     що з'явилась нова версія у Staging
                         ▼
                     ┌────────┐
                     │ audit  │  <- JSON audit event → stdout → Promtail → Loki
                     └────────┘
```

#### ➕ Плюси Argo Workflows

- **Багатокроковий пайплайн з DAG-залежностями:** train → verify → audit
  описується декларативно, кожен крок має власні resources, retryStrategy,
  ttlStrategy. У «голому» `Job` цього довелося би досягати через костильну
  логіку в bash.
- **`WorkflowTemplate` як reusable ресурс.** CI подає тільки короткий
  `Workflow` з `workflowTemplateRef` + параметрами (~40 рядків замість
  ~100). Шаблон живе у git, версіонується ArgoCD, редагується один раз.
- **Логи, статуси і UI з коробки.** `argo -n mlops-system list`,
  `argo logs @latest`, вебморда із діаграмою DAG — це критично, коли крок
  `verify` фейлить після успішного `train`.
- **Artefacts + archiveLogs → S3/MinIO.** Логи кожного кроку автоматично
  архівуються у той самий MinIO, що і MLflow-артефакти. Ніяких додаткових
  Loki-запитів для post-mortem.
- **RBAC / audit-integration:** контролер сам створює Event'и, ставить
  labels на дочірні поди, TTL-очищає завершені Workflows. Один RBAC-Role
  замість самописної уборки.

#### ➖ Мінуси Argo Workflows

- **Крива навчання.** DAG-мова Argo нетривіальна для читача, який не
  бачив CRD `Workflow`. Це підвищує bus-factor.
- **Обмеження на образи.** Крок `verify` виконує inline-Python у тому ж
  training-образі — це працює, але я «змішую» ролі образу. У «правильному»
  проді краще мати окремий тонкий верифікатор.
- **Ще одне UI, яке треба ставити за SSO.** У цьому проєкті Argo UI
  експонується як Ingress `argo.squirell.pp.ua` і закритий Cloudflare
  Zero Trust Application (email/SSO), тож проблеми з auth немає — але це
  додатковий компонент у Zero Trust dashboard, який треба конфігурувати.

#### Чому все ж так

Argo Workflows - нативно працює в кубернетесі, надає веб інтерфейс
та дозволяє зручно передивлятись логи та розбивати сценарії на окремі кроки.
Також існує підтримка задач по розкладу та подальша оптимізація налаштувань.
Це CNCF Graduated проєкт з активною спільнотою і
DAG-моделлю, яка добре лягає на «тренуй → перевір реєстрацію → пиши
аудит». Для навчального проєкту це найкращий баланс: показати вміння
працювати з DAG-оркестратором без переускладення (Kubeflow/Flyte були б
перегиб для Iris). Плюс — готова платформа для наступних кроків
(batch training, backfill, HPO).

---

### Blue-Green замість Canary

Детальний ADR — у файлі [`ADR.md`](./ADR.md). Коротко:

- **Blue-Green:** дві незалежні `Deployment` (`inference-blue`,
  `inference-green`), один `Service` перемикається зміною
  `selector.color`. `git revert` = rollback за < 30 с.
- Canary через ingress-nginx annotations був відкинутий: додає складності
  на боці ingress, а для сервісу з очікуваним RPS < 1 статистичні
  переваги мінімальні.
- Shadow (Istio/Envoy fan-out) потребував би окремого mesh — не входить
  у scope.

---

## Структура репозиторію

```
goit-mlops/
├── Makefile                      # bootstrap-apps, test, build, lint, status
├── pyproject.toml                # ruff-конфіг (py312)
├── .pre-commit-config.yaml       # ruff / yamllint / gitleaks
├── .yamllint.yaml
│
├── training/                     # train.py, promote.py, rollback.py + тести
│   ├── train.py                  # тренує sklearn LR, логує у MLflow,
│   │                             #   ставить tag `checksum.sha256`
│   ├── promote.py                # Staging → Production, audit event
│   ├── rollback.py               # rollback на Archived
│   ├── mlflow_utils.py           # SHA256 по всьому дереву артефакту
│   ├── audit.py                  # JSON audit-події → stdout → Loki
│   ├── train-workflow.tpl.yaml   # Argo Workflow submission template
│   ├── Dockerfile
│   └── tests/
│
├── inference/                    # FastAPI-сервіс
│   ├── app/
│   │   ├── main.py               # /healthz /readyz /metrics /model /predict
│   │   ├── model_loader.py       # SHA256 verify (fail-closed) → pyfunc load
│   │   ├── schemas.py            # Pydantic v2 (extra=forbid, обмежені float-и)
│   │   ├── metrics.py            # Prometheus (власний CollectorRegistry)
│   │   ├── logging_config.py     # JSON-логи → stdout → Loki
│   │   └── predictions_sink.py   # best-effort JSONL → MinIO
│   ├── Dockerfile
│   └── tests/
│
├── evidently/                    # drift-джоб (Блок E)
│   ├── drift_job.py              # 4 CLI-режими: fetch / compute / publish (для
│   │                             # Argo DAG) + run (моноліт для локалу)
│   ├── Dockerfile
│   └── requirements.txt
│
├── deploy/                       # ArgoCD-керовані k8s-маніфести
│   ├── namespaces/               # mlops-system/staging/production + quotas + PSA
│   ├── rbac/                     # 4 SA + 3 Role (engineer/viewer/prod-restricted)
│   ├── mlops-system/             # MLflow-Postgres, MLflow, MinIO, Pushgateway,
│   │                             #   WorkflowTemplate `iris-train`
│   ├── inference-staging/        # Deployment / Service / Ingress / SMon / HPA / PDB
│   ├── inference-production/     # blue + green Deployments, Service (селектор),
│   │                             #   Ingress, HPA, PDB
│   ├── monitoring/               # PrometheusRule + Grafana dashboard CM
│   ├── networkpolicies/          # default-deny + explicit allow per namespace
│   └── evidently/                # drift-джоб через Argo Workflows:
│                                 #   drift-cronworkflow.yaml
│                                 #     WorkflowTemplate iris-drift (3-крок DAG)
│                                 #     CronWorkflow mlops-drift (0 */6 * * *)
│                                 #   cronjob.yaml (закоментований референс)
│
├── argocd/apps/                  # 9 root ArgoCD Applications
│   ├── 00-namespaces.yaml
│   ├── 10-rbac.yaml
│   ├── 20-mlops-system.yaml       # app-of-apps: MLflow, MinIO, Pushgateway
│   ├── 25-argo-workflows.yaml     # Helm-реліз Argo Workflows
│   ├── 30-monitoring.yaml
│   ├── 35-networkpolicies.yaml
│   ├── 40-inference-staging.yaml
│   ├── 50-inference-production.yaml
│   └── 60-evidently.yaml
│
└── .github/workflows/            # GitHub Actions: ci.yaml (lint / test / build+push GHCR)
```

---

## DNS, TLS та Cloudflare Zero Trust

Усі публічні endpoint-и розташовані у зоні `squirell.pp.ua` і фронтяться
Cloudflare Zero Trust Application-ами (email OTP + SSO), тому Ingress-и
у кластері не мають додаткового шару авторизації — авторизацію робить
Cloudflare *перед* тим, як запит дійде до `cloudflared`-тунеля.

| Хост                             | Компонент                                          |
|----------------------------------|----------------------------------------------------|
| `mlflow.squirell.pp.ua`         | MLflow UI                                          |
| `argo.squirell.pp.ua`           | Argo Workflows Server UI                           |
| `iris-staging.squirell.pp.ua`   | inference Staging (`Model.Stage=Staging`)          |
| `iris.squirell.pp.ua`           | inference Production (`Model.Stage=Production`), Blue-Green |

**TLS:** усі Ingress-и звертаються до ClusterIssuer `letsencrypt-dns01`
(cert-manager із DNS-01 solver через Cloudflare API-token). Це дозволяє
випускати сертифікати (включно з вайлдкардами `*.squirell.pp.ua`) без
відкриття HTTP-01 портів наружу — публічний доступ живе виключно на
edge Cloudflare.

**Автентифікація:** у Cloudflare Zero Trust dashboard для кожного хоста
створено `Application` з політикою «email в allowlist» або SSO
(залежно від сервісу). Токен зі cookie передається у `cf-access-jwt-assertion`
хедері; для сервісів, які приймають клієнтів (наприклад, `iris.squirell.pp.ua`
для програмного `curl`), це можна замінити на `CF-Access-Client-Id` +
`CF-Access-Client-Secret` service token-и — див. Cloudflare docs.

Grafana / ArgoCD / Prometheus залишаються на своїх існуючих хостах,
захищених таким самим Cloudflare Zero Trust налаштуванням.

---

## Передумови

- Доступ до цільового k3s-кластера (`kubectl config get-contexts`);
- ArgoCD у namespace `argocd` (уже є);
- kube-prometheus-stack у namespace `monitoring` з Helm-release-name
  `kube-prometheus-stack` (лейбли dashboards/rules покладаються на це);
- `ingress-nginx` + `cert-manager` з ClusterIssuer `letsencrypt-dns01`
  (DNS-01 solver через Cloudflare API-token);
- Cloudflare Tunnel (`cloudflared`), який форвардить
  `*.squirell.pp.ua` на `ingress-nginx`;
- Cloudflare Zero Trust Applications (email/SSO) перед відповідними
  хостами.

---

## Bootstrap-послідовність (Блок A)

Життєвий цикл проєкту розбито на фази у `Makefile` — див. `make help`. Швидкий
шлях від нуля до працюючого production:

```bash
# 1. Клон
git clone https://github.com/bbeellkk-ua/goit-mlops.git
cd goit-mlops
git checkout final

# 2. (опційно) перевірити локально
make venv install lint test

# 3. Запушити образи. Це робить CI автоматично (GitHub Actions → GHCR);
#    якщо хочете локально:
make build-inference build-training build-evidently

# 4. Інфраструктура (namespaces → RBAC → platform → observability):
make infra
# ↑ у правильному порядку залежностей ArgoCD-Applications 00 → 35.

# 5. Тренування першої версії моделі як Argo WorkflowTemplate у кластері:
make train
# Це створить `iris-classifier` версію 1 і переведе її у Staging.
# Argo UI: https://argo.squirell.pp.ua

# 6. Деплой staging + перевірка:
make deploy-staging
make refresh-staging      # rolling-restart, щоб пода підхопили Staging-версію
make smoke-staging

# 7. Промоція Staging → Production:
make promote VERSION=1 REASON="initial go-live" ACTOR=<you>

# 8. Деплой production (Blue-Green):
make deploy-prod
make refresh-prod
make smoke-prod

# 9. Drift detection (Evidently, кожні 6 год):
make deploy-evidently
```

---

## Локальна розробка

```bash
make venv       # створює .venv (Python 3.12)
make install    # ставить залежності training/inference/dev
make lint       # ruff + yamllint
make fmt        # ruff format
make test       # запускає pytest для training та inference
```

---

## Спостережуваність

- **Grafana** — дашборд `mlops-inference-dashboard` автоматично підбирається
  через ConfigMap з лейблом `grafana_dashboard: "1"`.
- **Prometheus** — правила з `PrometheusRule mlops-inference`
  (лейбл `release: kube-prometheus-stack`).
- **Loki** — audit-події доступні як
  `{namespace=~"mlops-.*"} |= "event_category" | json`. Кожна подія
  `predict`, `model.register`, `model.transition`, `ratelimit.exceeded`,
  `training.completed` несе `request_id` або `workflow_name`.
- **Argo Workflows UI** — публічно доступний за
  `https://argo.squirell.pp.ua` (за Cloudflare Zero Trust). Показує DAG
  останніх тренувань з логами кожного кроку. Локальна альтернатива —
  `kubectl -n argo port-forward svc/argo-workflows-server 2746:2746`.

---

## CI / CD

Ми свідомо розділили відповідальність між двома платформами: **GitHub Actions
робить те, що потребує тільки git+GHCR** (build, test, scan), а **все, що
потребує доступу до Kubernetes API — виконується у самому кластері як
Argo Workflow**. Причина технічна, і вона важлива:

### Чому GitHub Actions не робить train / promote / rollback

Наш k3s-кластер `mlops-k3s` знаходиться у приватній мережі:

- Немає публічно доступного `apiserver` endpoint-а (kubeconfig посилається
  на приватний IP + client-cert);
- Немає inbound-VPN, який би відкрився для GitHub-адрес;
- Використання [GitHub public runners](https://docs.github.com/en/actions/using-github-hosted-runners/about-github-hosted-runners/about-github-hosted-runners)
  означає, що job виконується на випадковій VM у Azure — вона фізично не
  може підключитися до нашого apiserver.

Це типова обмеженість: **inbound-only-приватні кластери непроксовані з GH**.
Стандартні рішення для «GHA-triggered training»:

1. **Ставити [self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners) у самому кластері** з ServiceAccount + Role,
   який може `argo submit` — тоді workflow_dispatch спокійно тригерить
   Argo Workflows. Це стандартний enterprise-паттерн.
2. **Використовувати чергу через reverse-tunnel** (наприклад, Argo Events
   з webhook-приймачем усередині кластера).

Обидва — валідні. Ми поки що не ставили self-hosted runner (проєкт
навчальний, це надлишок), тому train/promote/rollback запускаються
напряму — через `argo submit` до in-cluster WorkflowTemplate-ів. Ось
що робить кожна платформа зараз:

| Стадія                | GitHub Actions (`.github/workflows/ci.yaml`) | Argo Workflows (in-cluster) |
|-----------------------|----------------------------------------------|-----------------------------|
| lint / SAST           | ruff + yamllint + gitleaks                   | —                           |
| unit-тести            | pytest (training + inference)                | —                           |
| build + push          | docker buildx → `ghcr.io/bbeellkk-ua/*`      | —                           |
| image scan            | Trivy у тому ж job                           | —                           |
| **train**             | —                                            | `WorkflowTemplate iris-train` (DAG: train → verify → audit) |
| **promote**           | —                                            | `WorkflowTemplate iris-promote` (Staging → Production + audit) |
| **rollback**          | —                                            | `WorkflowTemplate iris-rollback` (Production → Archived, попередня → Production) |
| drift detection       | —                                            | `CronWorkflow mlops-drift` (щ0 6 год: fetch → compute → publish) |

### Як запускати in-cluster workflow-и

Через `Makefile`:

```bash
make train                                          # нова версія → Staging
make promote VERSION=4 REASON="v4 OK in staging"   # Staging → Production
make rollback TO_VERSION=3 REASON="v4 broken"      # відкат
make drift-run                                      # разовий drift-скан
```

Через `argo` CLI напряму:

```bash
argo -n mlops-system submit --from workflowtemplate/iris-train \
     --generate-name iris-train- \
     -p git-commit="$(git rev-parse HEAD)" \
     -p job-id="manual-$(date +%s)" \
     --wait

argo -n mlops-system submit --from workflowtemplate/iris-promote \
     --generate-name iris-promote- \
     -p version=4 -p actor="oleksandr" -p reason="v4 OK in staging" \
     --wait
```

Через Argo UI: `https://argo.squirell.pp.ua` → Workflow Templates →
`iris-{train,promote,rollback}` → **Submit** → заповнити параметри.

### GitHub Actions free-tier достатньо для build-фази

- Public repo → безкоштовно завжди;
- Private repo Free → 2 000 хв/міс, реальне споживання проєкту
  ~500–800 хв/міс (5–6 хв на push × 20 pushes/тиждень) — з великим запасом;
- Storage у GHCR публічних пакетів безлімітний.

---

## Рольова модель (Блок C3)

Три ролі, реалізовані як k8s-ServiceAccount + Role/RoleBinding
(`deploy/rbac/`):

| Роль                          | mlops-system   | mlops-staging | mlops-production           |
|-------------------------------|----------------|---------------|----------------------------|
| `mlops-viewer`                | read           | read          | read                       |
| `mlops-engineer`              | full           | full          | **read + patch на Service/Ingress**; заборонено `delete deploy`, `exec`, `secret write` |
| `inference-sa` (workload)     | —              | `configmap/secret read` | `configmap/secret read` |
| `training-sa` (workload)      | `workflow create` + `pod/log read` | — | —                         |

Отримання kubeconfig із token цих SA (без SSO):

```bash
kubectl -n mlops-system create token mlops-engineer --duration=8h
```

---

## Доступність кластера для перевірки

Кластер **буде доступний як мінімум 24 години після здачі проєкту**.

- Hostnames (`iris.squirell.pp.ua`, `iris-staging.squirell.pp.ua`,
  `mlflow.squirell.pp.ua`, `argo.squirell.pp.ua`) обслуговуються через
  Cloudflare Zero Trust → cloudflared → ingress-nginx; TLS через
  cert-manager + Let's Encrypt (DNS-01).
- Доступ до Cloudflare Zero Trust Applications надається за запитом:
  email додається до allowlist політики, після чого користувач проходить
  автентифікацію через OTP на пошту або SSO-провайдер.

---

## Teardown

```bash
make teardown
# = kubectl delete -f argocd/apps/60-evidently.yaml
#   kubectl delete -f argocd/apps/50-inference-production.yaml
#   kubectl delete -f argocd/apps/40-inference-staging.yaml
#   kubectl delete -f argocd/apps/35-networkpolicies.yaml
#   kubectl delete -f argocd/apps/30-monitoring.yaml
#   kubectl delete -f argocd/apps/25-argo-workflows.yaml
#   kubectl delete -f argocd/apps/20-mlops-system.yaml
#   kubectl delete -f argocd/apps/10-rbac.yaml
#   kubectl delete -f argocd/apps/00-namespaces.yaml
```

---

## Пов'язана документація

- [`RUNBOOK.md`](./RUNBOOK.md) — day-2 операції (деплой нової версії,
  rollback, тріаж алертів);
- [`ADR.md`](./ADR.md) — обґрунтування Blue-Green (ADR-001) та
  Argo Workflows (ADR-002);
- [`THREAT_MODEL.md`](./THREAT_MODEL.md) — 6 ключових загроз і мітигації;
- [гілка `eks-terraform`](https://github.com/bbeellkk-ua/goit-mlops/tree/eks-terraform)
  — Terraform-код для EKS (не використовується у цьому деплойменті,
  зберігається для довідки).
