# ADR-001 — Стратегія розгортання: Blue-Green

## Рішення

Використовуємо **Blue-Green**, реалізовану як **два Deployment-и Kubernetes**
(`inference-blue`, `inference-green`), що спільно використовують один Service.
Трафік перемикається виключно через мітку `selector.color` у Service.
Git-коміт, що змінює цей селектор — атомарна точка перемикання;
`git revert` — це відкат.

## Розглянуті альтернативи

### Canary (вагове перемикання трафіку через ingress-nginx)

Плюси:
- поступова експозиція (1 % → 10 % → 100 %) — статистичні регресії
  ловляться раніше, ніж при різкому перемиканні;
- дозволяє тримати обидві версії живими у контрольованих пропорціях.

Мінуси для цього проєкту:
- потребує canary-анотацій ingress-nginx на **двох Ingress-об'єктах**
  та акуратної липкості cookie/header — обсяг ingress-логіки перевищує
  фактичні зміни у ML;
- відкат — це *не одна* git-операція (треба зменшити вагу і чекати
  завершення поточних з'єднань);
- для stateless класифікаційного сервісу з очікуваним трафіком < 1 rps
  статистичні гарантії Canary дають мало користі.

### A/B (shadow)

Плюси:
- нульовий вплив на користувача — нова модель бачить трафік, але її
  вихід відкидається;
- ідеальний для офлайн-порівняння A/B.

Мінуси для цього проєкту:
- потребує in-cluster fan-out проксі (envoy, istio), якого у поточному
  k3s-кластері немає;
- не відповідає на питання «чи можна пустити на неї реальний трафік» —
  усе одно потрібен другий крок перемикання.

### Recreate

Не розглядався — завдання вимагає zero-downtime.

## Наслідки

**Позитивні**
- Відкат є git-нативним: `git revert <flip-commit>` + `argocd app sync`.
  Час відновлення < 30 с, якщо попередній колір ще прогрітий.
- Blue та green можна масштабувати та спостерігати незалежно
  (ServiceMonitor скрейпить обидва — дашборди розділяють за label `color`).
- Ідеальний колір прогрівається на вимогу (`replicas: 0` → 2 перед
  перемиканням); у стаціонарному стані ми не платимо CPU/пам'ять
  за два повні флоти.

**Негативні**
- Дворівнева промоція (scale-up green + перемикання Service) трохи важча
  за `kubectl set image`. Компенсовано документацією в RUNBOOK.
- Оскільки саме селектор Service маршрутизує трафік, `selfHeal` в ArgoCD
  для production **вимкнено** (див. `argocd/apps/50-inference-production.yaml`).
  Це задокументований компроміс: інженери повинні вручну виконати
  `argocd app sync` після коміту у production.
- Blue-Green не може виразити частковий трафік (напр., 20 %/80 %). Якщо
  такі вимоги з'являться, треба перейти на Canary або shadow.

## Вплив на платформу

- `deploy/inference-production/service.yaml` — єдине джерело правди
  про те, який колір активний. Анотація `mlops.goit/blue-green-active` —
  людяна підказка; селектор — машиночитне джерело істини.
- Обидва деплойменти несуть `DEPLOYMENT_COLOR` env → відображається в
  JSON-логах та в info-метриці Prometheus `inference_model`, тож ми
  можемо постфактум визначити, який колір обслуговував конкретний запит.
- HPA — окремий на кожен колір, щоб масштабування одного не впливало
  на другий.
- PDB — окремі на кожен колір, щоб voluntary disruption ніколи не
  прибирали обидва одночасно.

## Продовження (поза обсягом цього проєкту)

- Автоматичні soak-тести проти idle-кольору перед дозволом перемикання
  (Argo Rollouts `AnalysisTemplate`).
- Preview-хост `iris-preview.squirell.pp.ua`, що завжди вказує
  на idle-колір, для аутентифікованого тестового трафіку.
- Міграція на Argo Rollouts BlueGreen, як тільки кластер його запустить,
  щоб перемикання стало одним CR-патчем, а не редагуванням селектора.

---

# ADR-002 — Argo Workflows як оркестратор тренування

## Рішення

Перевести повний конвеєр тренування на **Argo Workflows**. Замість одного
Kubernetes Job запускається `Workflow`, що посилається на `WorkflowTemplate
iris-train` у неймспейсі `mlops-system` і виконує DAG із трьох кроків:

1. **`train`** — власне тренування (той самий контейнер, що й раніше),
   з передачею гіперпараметрів як параметрів Workflow.
2. **`verify-registration`** — inline-Python-крок, який через
   `MlflowClient().search_model_versions("name='iris-classifier'")`
   перевіряє, що нова версія дійсно зареєстрована.
3. **`audit`** — inline-крок, що емітить JSON-подію
   `event_action: training.completed` до stdout (Promtail → Loki).

Компоненти:
- **ArgoCD Application** `mlops-argo-workflows` (Helm-чарт `argo-workflows`
  версії 0.42.3), розгортає контролер у неймспейс `argo`.
- **`WorkflowTemplate iris-train`** (`deploy/mlops-system/train-workflow-template.yaml`) —
  версіонована в git специфікація DAG.
- **Submission template** `training/train-workflow.tpl.yaml` — з
  плейсхолдерами `__JOB_ID__`, `__GIT_COMMIT__`, `__TRAINING_IMAGE__`,
  `__LR_C__` тощо, підставляються з CI.
- **Artifact repository** — MinIO (той самий бакет, що для MLflow),
  через `artifactRepository.s3` у values чарта.
- **RBAC** — до Role `training-runtime` додано доступ до
  `argoproj.io/workflows,workflowtemplates,clusterworkflowtemplates`
  (verbs: `get,list,watch,create`).
- **CI** — GitHub Actions встановлюють `argo CLI v3.5.10`,
  сабмітять Workflow (`kubectl create`), очікують `argo wait` та
  збирають логи через `argo logs`.

## Розглянуті альтернативи

### Залишити `batch/v1 Job` + `kubectl apply`

Плюси:
- нульова додаткова інфраструктура — контролер Kubernetes уже є;
- проста ментальна модель для команд, що не знайомі з Argo.

Мінуси:
- нема нативного DAG — verify/audit доводиться робити як bash-хвіст
  у CI, поза кластером;
- нема артефактного репозиторію — треба руками керувати MinIO у
  тренувальному коді;
- нема UI для істо́рії запусків — `kubectl get jobs` + `kubectl logs`;
- retry-політика примітивна (`backoffLimit`), без стратегій на рівні
  окремих кроків.

### Kubeflow Pipelines

Плюси:
- багатий ML-специфічний DSL (Python-first);
- нативна інтеграція з MLflow, KServe тощо.

Мінуси для цього проєкту:
- важкий стек (KFP + Metadata + Argo під капотом + Istio-опціональний);
- вимагає окремої SQL-БД для metadata store — додатковий Postgres;
- для однієї Iris-моделі це надмірно.

## Плюси Argo Workflows у цьому проєкті

- **CNCF Graduated** — стабільний, з розвиненою екосистемою.
- **DAG з умовними переходами** — `verify` виконується після `train`,
  `audit` — після обох, з наочним UI історії.
- **Artifact passing** — вбудований механізм передачі артефактів між
  кроками через S3 (той самий MinIO).
- **`WorkflowTemplate` як версіонована специфікація** — DAG живе в git,
  ArgoCD синхронізує його як звичайний Kubernetes-ресурс.
- **Спостережуваність** — Argo Server експонує UI + метрики Prometheus
  на кожен крок (`argo_workflows_count`, `argo_workflows_duration`).
- **Retry / TTL** — політики на рівні кроку та Workflow загалом
  (`retryStrategy`, `ttlStrategy: secondsAfterCompletion: 3600`).
- **`argo wait` у CI** — простий та надійний примітив для CI-інтеграції
  замість власного `kubectl wait --for=condition=complete`.

## Мінуси / компроміси

- **Додатковий компонент** — контролер Argo Workflows у неймспейсі `argo`
  (≈100 MB RAM у idle-стані).
- **Крива навчання** — YAML-DSL Argo та концепції templates/steps/DAG
  вимагають часу для команд, знайомих лише зі стандартним Kubernetes.
- **YAML-плейсхолдерна підстановка** у CI — залишили простий `sed` замість
  Argo `parameters` через збережений формат старого шаблону; це
  ідемпотентне рішення, яке працює однаково для GHA та GitLab CI.
- **Дублювання секретів** — MinIO-креденшели потрібні і training-крокам,
  і `artifactRepository`, тому створюємо два ідентичні Secret-и
  (`training-s3` та `argo-workflows-s3`). Мінімізовано ExternalSecrets
  на пізніший етап.

## Наслідки

**Позитивні**
- Пайплайн стає декларативним ресурсом Kubernetes → GitOps-контроль на
  кожен запуск.
- Історія запусків доступна в Argo UI без парсингу CI-логів.
- Verify та audit кроки виконуються **в кластері**, з тими самими
  секретами MinIO/MLflow — жодного повторного розповсюдження креденшелів
  у CI.
- Легко розширити пайплайн (evaluate, promote-if-metric-good, notify) —
  просто додаткові кроки в DAG.

**Негативні**
- Додано новий керований компонент (Argo Workflows controller + server),
  який треба апгрейдити.
- Онбординг для нових інженерів включає прочитання документації
  Argo Workflows.

## Продовження

- Заміна `sed`-плейсхолдерів на `argo submit --parameter` після того, як
  завершиться період стабілізації.
- Розширення DAG додатковим кроком `evaluate` (тестова точність < порогу
  → fail), який заблокує промоцію поганих моделей ще до реєстрації.
- Ввімкнення `metrics.enabled: true` у чарті + додавання Grafana-дашборду
  для тренувань.

---

# ADR-005 — Drift-джоб на Argo Workflows (CronWorkflow) замість `batch/v1 CronJob`

## Рішення

Замінити `batch/v1 CronJob` на **Argo `CronWorkflow` + `WorkflowTemplate
iris-drift`** з трьохкроковим DAG:

```
┌──────────────────┐  parquet   ┌──────────────────┐  json      ┌────────────────┐
│ fetch-predictions├───────────▶│ compute-drift    ├──────────▶│ publish        │
│ (S3 list+read)   │  current.pq│ (PSI vs reference)│ scores.json│ (Pushgateway   │
│                  │            │                    │            │  + audit stdout)│
└──────────────────┘            └──────────────────┘            └────────────────┘
```

Кожен крок — окремий Kubernetes Pod з окремими логами, retry-політикою
і артефактом, який Argo автоматично збирає до MinIO-бакета
`argo-workflows` (той самий, що для тренування — див. ADR-002).

Компоненти:
- **`WorkflowTemplate iris-drift`** (`deploy/evidently/drift-cronworkflow.yaml`)
  у namespace `mlops-system` — версіонована в git специфікація DAG,
  синхронізується ArgoCD Application `mlops-evidently`
  (`argocd/apps/60-evidently.yaml`).
- **`CronWorkflow mlops-drift`** — той самий розклад `0 */6 * * *`,
  `concurrencyPolicy: Forbid`, історія 3+3.
- **`evidently/drift_job.py`** розділено на CLI-режими: `fetch`,
  `compute`, `publish` + `run` (моноліт для локалу). Логіка PSI/pushgateway
  винесена у shared-хелпери, тому обидва режими викликають той самий код.
- **`deploy/evidently/cronjob.yaml`** залишено як **закоментований
  референс** — щоб можна було швидко порівняти обидва підходи.

## Розглянуті альтернативи

### Залишити `batch/v1 CronJob`

Плюси:
- нульова додаткова інфраструктура (Argo Workflows вже є для тренування,
  але формально можна було б обійтися без нього для drift);
- проста ментальна модель — «поставити крон, він запускає pod».

Мінуси:
- нема DAG-візуалізації: якщо `push_to_gateway` фейлить після успішного
  `compute_drift`, зрозуміти це можна лише з логів контейнера;
- нема artefact-passing: `parquet` з предикціями і `scores.json` живуть
  тільки в `/tmp` pod-а і зникають разом з ним;
- retry — груба: `backoffLimit` перезапускає **весь** pod, а не крок,
  що коштує повторного S3-list та повторного load reference;
- фрагментація оркестрації: тренування — на Argo Workflows, drift — на
  batch/v1. Дві точки правди про «cluster batch jobs».

### KEDA `ScaledJob` + черга

Плюси:
- event-driven — можна тригерити drift-джоб, коли з'явився новий batch
  предикцій, а не за розкладом;
- гарантія паралелізації.

Мінуси:
- значне додавання інфраструктури (KEDA + черга) заради однієї джоби;
- порушує принцип «один оркестратор для всіх batch-задач у mlops-system»,
  який ми встановили у ADR-002.

### Kubeflow Pipelines / Flyte

Ті ж мінуси, що і в ADR-002 (важкий стек, окрема БД метаданих), плюс
Iris-drift — тривіальна задача, для якої повний ML-workflow-engine
непотрібний.

## Плюси Argo CronWorkflow у цьому проєкті

- **DAG-візуалізація** — у Argo UI (`argo.squirell.pp.ua`) видно
  діаграму 3-х кроків, статус кожного, тривалість, повторення.
- **Окремі pod-и → окремі логи** — `argo logs @latest -c fetch`,
  `-c compute`, `-c publish` без парсингу спільного stdout.
- **Artefact-passing через S3** — `current.parquet` і `scores.json`
  залишаються у бакеті `argo-workflows` після завершення, тому можна
  replay/re-compute поза кластером.
- **Retry на рівні кроку** — `fetch-step` має
  `retryStrategy.limit: 2, retryPolicy: OnError`; якщо MinIO timeout,
  ретрайається лише `fetch`, а не весь пайплайн.
- **Уніфікація з ADR-002** — той самий namespace, SA-патерн,
  `ttlStrategy`, `podGC`, MinIO artifact repo. Мінус одна ментальна
  модель для операторів.
- **CronWorkflow** — nativna заміна `batch/v1 CronJob`: `schedule`,
  `concurrencyPolicy`, `successfulJobsHistoryLimit` — API майже
  ідентичний.

## Мінуси / компроміси

- **Додаткова залежність від Argo Workflows** — але вона вже є для
  тренування, тому це не new-cost.
- **Overhead pod-startup** — три pod-и замість одного додають ~30-60 с
  до end-to-end runtime (image pull + init). Для 6-годинного розкладу —
  прийнятно.
- **YAML anchors** (`&drift_env`, `*drift_env`) — використали для DRY
  env-блоку між кроками. Не всі Kubernetes-linter-и (kubeval, kubeconform)
  однаково добре їх розуміють; довелося перевіряти вручну.
- **Дублювання секрету MinIO** — той самий `evidently-s3` створюється
  цим самим маніфестом, як і раніше. У продакшн-варіанті — замінити на
  `ExternalSecret` (Vault/AWS SM), як і `training-s3` з ADR-002.

## Наслідки

**Позитивні**
- Один оркестратор batch-задач → однаковий RUNBOOK для тренування і
  drift.
- Явна DAG-візуалізація значно спрощує тріаж алерту
  `DriftPipelineStale` — оператор одразу бачить, який саме крок падає.
- Artefact-репо у MinIO зберігає повний slice предикцій, що дає змогу
  reproduce drift-score локально без re-fetch.

**Негативні**
- Оператор має пам'ятати різницю між ArgoCD Application (`mlops-evidently`)
  та Argo Workflow (`iris-drift`) — два продукти зі схожими назвами.
- Rollback до старого CronJob можливий (файл `cronjob.yaml` залишений
  закоментованим для референсу), але потребує ручного uncomment + видалення
  CronWorkflow.

## Продовження

- Додати крок `evaluate-drift`, який приймає `scores.json` та за
  перевищення `DRIFT_THRESHOLD` створює `Event` у namespace + emit
  Prometheus-подію `drift.threshold.exceeded` (замість пасивного alert
  на Pushgateway-gauge).
- Опціонально: `notify-slack`-крок як `onExit` handler у Workflow.
- Замінити YAML anchors на Argo `templateRef` до shared step-template,
  коли з'явиться час на рефакторинг.
