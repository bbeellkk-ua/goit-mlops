# RUNBOOK — goit-mlops

Day-2 операції для платформи інференсу Iris. Кожна процедура нижче
є GitOps-first: будь-яка зміна production-трафіку — це git-коміт у
`bbeellkk-ua/goit-mlops`, гілка `final`.

---

## 1. Розгортання нової версії моделі (Blue-Green)

Припустимо, у production зараз **blue** (v3), і ми щойно натренували **v4**
(вона зараз у `Staging`).

```
        активний   кандидат
──────  blue=v3    green=idle
крок 1  blue=v3    green=v4 (масштабовано, трафіку не отримує)
крок 2  blue=v3    green=v4 (canary smoke-test через inference-green.mlops-production)
крок 3  green=v4   blue=v3  (перемкнено селектор Service)
крок 4  green=v4   blue=idle (blue масштабовано в 0)
```

### 1.1  Промоція v4 у registry

Промоушен виконується **у самому кластері** через
`WorkflowTemplate iris-promote` (namespace `mlops-system`). GitHub Actions
runners не мають мережевого доступу до приватного k3s API, тому промоушен
не запускається з GH. Два еквівалентні способи:

```bash
# 1) Через Makefile (рекомендовано):
make promote VERSION=4 ACTOR=<ти> REASON="feature X"

# 2) Напряму через argo CLI:
argo -n mlops-system submit --from workflowtemplate/iris-promote \
     -p version=4 \
     -p actor="<ти>" \
     -p reason="feature X" \
     --serviceaccount training-sa \
     --wait

# 3) Через Argo UI:
#   https://argo.squirell.pp.ua → Workflow Templates → iris-promote → SUBMIT
```

Workflow:

1. переводить v4 у `Production`, архівує поточну Production
   (`archive_existing_versions=true`);
2. пише подію аудиту `model.promoted` у stdout → Promtail → Loki.

### 1.2  Масштабуємо green

Правимо `deploy/inference-production/deployment-green.yaml`,
виставляємо `spec.replicas: 2`. Коміт, push. ArgoCD (з
`selfHeal: false` для production) покаже diff — натисни **Sync**
у UI, або:

```bash
argocd app sync mlops-inference-production
```

Чекаємо, доки green-поди будуть готові:

```bash
kubectl -n mlops-production rollout status deploy/inference-green --timeout=5m
kubectl -n mlops-production get pods -l color=green
```

### 1.3  Smoke-test green

Green доступний через Service `inference-green` (лише ClusterIP).
Використовуємо port-forward:

```bash
kubectl -n mlops-production port-forward svc/inference-green 8080:80
curl -sf http://localhost:8080/readyz | jq
curl -sf http://localhost:8080/model  | jq '.model_version, .checksum_sha256'
curl -sf http://localhost:8080/predict \
   -H 'content-type: application/json' \
   -d '{"instances":[{"sepal_length":5.1,"sepal_width":3.5,"petal_length":1.4,"petal_width":0.2}]}' | jq
```

### 1.4  Перемикання Service

Правимо `deploy/inference-production/service.yaml`:

- `spec.selector.color`: `blue` → `green`
- `metadata.annotations."mlops.goit/blue-green-active"`: `blue` → `green`

Коміт з повідомленням `feat(prod): switch inference to green v4`.
`argocd app sync mlops-inference-production` → Service оновлюється
атомарно.

### 1.5  Верифікація + масштабування старого blue до 0

Дивимось на дашборди 10 хв:

- `sum by (status) (rate(inference_requests_total[1m]))` — без сплеску 4xx/5xx
- `histogram_quantile(0.95, ...)` — тримається під 500 ms
- `inference_model_ready` у green-неймспейсі = 1
- Loki: `{namespace="mlops-production"} | json | model_version="4"` — трафік іде туди

Далі правимо `deployment-blue.yaml` → `replicas: 0`, коміт
`chore(prod): scale down blue after switch`.

---

## 2. Відкат (швидкий)

Два варіанти залежно від того, що зламалось.

### 2.1  Просто повернути трафік назад (green падає, blue ще активний)

Валідний **лише до** кроку 1.5 вище. `git revert` на flip-коміті:

```bash
git revert <commit_hash_of_flip>
git push
argocd app sync mlops-inference-production
```

Service повертається до `color: blue`. Трафік відновлюється за < 5 с.

### 2.2  Повний відкат до попередньої версії

Blue вже масштабований у 0 або погана версія працює на blue.
Запускаємо `WorkflowTemplate iris-rollback` у кластері (симетрично до
`iris-promote` — GH-runners доступу до k8s API не мають, тому все виконується
in-cluster):

```bash
# Через Makefile — на конкретну версію:
make rollback TO_VERSION=3 ACTOR=<ти> REASON="v4 SLA breach — accuracy < 0.85"

# Або без TO_VERSION — rollback на найновішу Archived:
make rollback ACTOR=<ти> REASON="v4 SLA breach"

# Через argo CLI напряму:
argo -n mlops-system submit --from workflowtemplate/iris-rollback \
     -p to-version=3 \
     -p actor="<ти>" \
     -p reason="v4 SLA breach — accuracy < 0.85" \
     --serviceaccount training-sa \
     --wait

# Через Argo UI:
#   https://argo.squirell.pp.ua → Workflow Templates → iris-rollback → SUBMIT
```

Це:

1. архівує поточну Production;
2. переводить v3 назад у `Production`;
3. пише подію аудиту `model.rolled_back`.

Далі повторюємо §1.2–§1.4, щоб підняти `inference-green` (або свіжий
blue) з v3.

---

## 3. Тренування через Argo Workflows

Тренування виконується `WorkflowTemplate iris-train` у неймспейсі
`mlops-system` (див. ADR-002). Оскільки GH-runners не мають доступу до
приватного k8s API, тренування завжди сабмітиться **у самому кластері** —
через `make train` з bastion-host або напряму `argo` CLI/UI. Нижче — ручне
керування для day-2 операцій.

### 3.1  Ручний запуск тренування

Найпростіший шлях — Makefile-таргет (обгортка над `argo submit`):

```bash
make train
# → argo submit --from workflowtemplate/iris-train --serviceaccount training-sa --wait
```

Або напряму через `argo` CLI, з переданням додаткових параметрів:

```bash
argo -n mlops-system submit --from workflowtemplate/iris-train \
     --generate-name iris-train-manual- \
     -p job-id="manual-$(date +%s)" \
     -p git-commit="$(git rev-parse HEAD)" \
     -p git-branch="$(git rev-parse --abbrev-ref HEAD)" \
     -p dataset-version="v1" \
     -p lr-c="1.0" \
     -p lr-max-iter="200" \
     --serviceaccount training-sa \
     --wait

# Логи одразу після завершення:
argo -n mlops-system logs @latest
```

Через Argo UI (з Cloudflare Zero Trust SSO):
`https://argo.squirell.pp.ua` → **Workflow Templates** → `iris-train` →
**SUBMIT** → заповнити параметри.

### 3.2  Перегляд поточних та історичних запусків

```bash
# Список останніх Workflow-запусків
argo -n mlops-system list

# Логи найсвіжішого запуску
argo -n mlops-system logs @latest

# Деталі DAG (крок за кроком)
argo -n mlops-system get @latest

# UI Argo — публічний хост за Cloudflare Zero Trust (email/SSO):
#   https://argo.squirell.pp.ua
# Локальна альтернатива (обхід Zero Trust):
kubectl -n argo port-forward svc/argo-workflows-server 2746:2746
# → http://localhost:2746
```

### 3.3  Скасувати запущений Workflow

```bash
argo -n mlops-system terminate <workflow-name>
# Або примусово (без graceful shutdown крокових подів):
argo -n mlops-system stop <workflow-name>
```

### 3.4  Retry невдалого запуску

```bash
argo -n mlops-system retry <workflow-name>
```

Argo перезапустить лише невдалі кроки DAG (train / verify / audit),
пропустивши успішні.

### 3.5  Прибирання старих Workflow

TTL встановлено в `WorkflowTemplate` (`ttlStrategy.secondsAfterCompletion:
86400`) — старі за замовчуванням чистяться через 24 години.
Ручне прибирання:

```bash
# Видалити всі успішні старіші за 7 днів
argo -n mlops-system delete --older 7d --status Succeeded

# Видалити конкретний Workflow
argo -n mlops-system delete <workflow-name>
```

---

## 4. Тріаж алертів

### 4.1  `MLOpsInferenceModelNotReady` (critical)

Под завантажився, але `inference_model_ready == 0` → неспівпадіння
SHA256 або MLflow недоступний.

```bash
NS=mlops-production   # або mlops-staging
kubectl -n $NS logs -l app=inference --tail=200 | jq -Rr 'fromjson? // .'
```

Шукаємо:

- `ModelIntegrityError: checksum mismatch` → хтось перезалив артефакт
  поза тренуванням. **Не** промотуй цю версію; перевір audit-логи MinIO;
  видали цю версію моделі.
- `Connection refused …:5000` → MLflow / mlflow-postgres под лежить.
  Перевір: `kubectl -n mlops-system get pods`.

### 4.2  `MLOpsInferenceHighLatency` (warning, p95 > 500 ms 5 хв поспіль)

```bash
kubectl -n mlops-production top pods -l app=inference
kubectl -n mlops-production describe hpa
```

- CPU throttling → HPA уперся у maxReplicas; підніми ліміт у
  `deploy/inference-production/hpa.yaml`.
- Стабільно висока пам'ять → шукай патерн зловживання пакетним запитом:
  `sum by (client_ip) (rate(inference_requests_total[5m]))` у Loki.

### 4.3  `MLOpsInferenceHighErrorRate` (warning, > 1 %)

Розділяємо 4xx проти 5xx:

```
sum by (status) (rate(inference_requests_total[5m]))
```

- Сплеск 400 → помилки валідації, перевір
  `{namespace="mlops-production"} |= "validation.failed" | json`.
  Найчастіше клієнт змінив схему.
- Сплеск 5xx → помилка застосунку; шукай stack trace у Loki.
- Сплеск 429 → див. 4.4.

### 4.4  `MLOpsInferenceRateLimited` (info, спрацьовують 429)

Перевіряємо розподіл `client_ip` в ingress. Якщо домінує одна IP —
додай NetworkPolicy або ingress-nginx `deny` для цього CIDR. Якщо
розподілено — підніми `REQUESTS_PER_MINUTE` у env деплойменту,
попередньо перевіривши capacity.

### 4.5  `MLOpsDriftDetected` (warning, PSI > 0.3 15 хв поспіль)

Реальні дані відхилились від тренувального референсу. Панель Grafana
*Drift score* показує розбивку по кожній фічі.

Дії:

1. Взяти семпл останніх передбачень:
   `kubectl -n mlops-system exec deploy/mlflow -- aws --endpoint-url http://minio:9000 s3 cp s3://mlflow-artifacts/predictions/production/ /tmp --recursive`
2. Прийняти рішення: перетренувати зараз чи чекати планового
   ретренінгу. Якщо перетренувати — запусти `iris-train` WorkflowTemplate
   (`make train` або `argo submit --from workflowtemplate/iris-train
   -p dataset-version=<нова>`).
3. Просунь свіжу версію через §1.

### 4.6  `MLOpsDriftJobStale`

Drift-джоб (Argo CronWorkflow `mlops-drift`) не звітує > 6 год.
Перевіряємо (див. §7 нижче для повного набору команд):

```bash
kubectl -n mlops-system get cronworkflow mlops-drift
argo -n mlops-system list --prefix mlops-drift
argo -n mlops-system get @latest
```

Типове: MinIO недоступний (падає `fetch-predictions`) або Pushgateway
недосяжний (падає `publish`). Argo Workflow-controller автоматично
робить retry (limit=2, backoff exp), але якщо всі спроби вичерпані —
розбираємось по логах конкретного кроку:

```bash
argo -n mlops-system logs @latest -c fetch    # або compute / publish
```

Полагодь кореневу причину і запусти вручну:

```bash
argo -n mlops-system submit --from workflowtemplate/iris-drift \
     --generate-name mlops-drift-manual-
```

### 4.7  Argo Workflow не завершується / зависає

```bash
# Подивись, на якому кроці застряг
argo -n mlops-system get <workflow-name>

# Логи конкретного кроку
argo -n mlops-system logs <workflow-name> --node <step-node-id>

# Стан контролера
kubectl -n argo logs deploy/argo-workflows-workflow-controller --tail=100
```

Найчастіші причини: MinIO недоступний (крок `train` не може вивантажити
артефакт), MLflow лежить (`verify-registration` не бачить нову версію).

---

## 5. Аварійна зупинка

Якщо ArgoCD поводиться дивно:

```bash
# Заморозити auto-sync на production
argocd app set mlops-inference-production --sync-policy none
```

Якщо модель активно віддає неправильні відповіді і швидкого шляху
відкату немає:

```bash
kubectl -n mlops-production scale deploy/inference-blue --replicas=0
kubectl -n mlops-production scale deploy/inference-green --replicas=0
```

Користувачі отримають 502 з ingress до відновлення. Якщо можливо —
краще §2.

---

## 6. Слід аудиту

Кожна зміна стану пише JSON-рядок у stdout, який Promtail збирає у
Loki. Приклади запитів:

- Усі промоції за останню добу
  `{namespace="mlops-system"} |= "event_action=\"model.transition\"" | json`
- Усі rate-limited запити, згруповані по IP
  `{namespace="mlops-production"} |= "ratelimit.exceeded" | json | client_ip != ""`
- Помилки цілісності моделі
  `{namespace=~"mlops-.*"} |= "startup_integrity_failure"`
- Завершені тренування
  `{namespace="mlops-system"} |= "event_action=\"training.completed\"" | json`
- Завершені drift-обчислення
  `{namespace="mlops-system"} |= "event_action=\"drift.computed\"" | json`

---

## 7. Drift-джоб (Argo CronWorkflow)

Drift-детекція виконується як `CronWorkflow mlops-drift`, який
запускає `WorkflowTemplate iris-drift` (DAG з 3 кроків: `fetch-predictions`
→ `compute-drift` → `publish`) кожні 6 годин у namespace `mlops-system`.
Артефакти (parquet із поточними предиктами + JSON зі scores) передаються між кроками через
MinIO-бакет `argo-workflows`.

### 7.1  Огляд стану

```bash
# Розклад та наступний запуск
kubectl -n mlops-system get cronworkflow mlops-drift

# Останні запуски (список Workflow, які створив CronWorkflow)
argo -n mlops-system list --prefix mlops-drift

# Стан DAG останнього запуску (наочно, по кроках)
argo -n mlops-system get @latest
```

### 7.2  Логи per-step

```bash
# Всі кроки останнього запуску
argo -n mlops-system logs @latest

# Тільки один крок (name = fetch / compute / publish, як у DAG)
argo -n mlops-system logs @latest -c fetch
argo -n mlops-system logs @latest -c compute
argo -n mlops-system logs @latest -c publish

# По конкретному workflow-run
argo -n mlops-system logs mlops-drift-1737024000 -c publish
```

### 7.3  Ручний запуск (позаплановий)

```bash
argo -n mlops-system submit --from workflowtemplate/iris-drift \
     --generate-name mlops-drift-manual-

# Дочекатись завершення
argo -n mlops-system wait @latest --timeout 10m
argo -n mlops-system logs @latest
```

### 7.4  Retry невдалого запуску

Argo автоматично робить retry на рівні кроку (`retryStrategy.limit=2`,
експоненціальний backoff — див. `deploy/evidently/drift-cronworkflow.yaml`).
Ручний retry після виправлення першопричини:

```bash
argo -n mlops-system retry <workflow-name>
```

Перезапустяться лише failed-кроки; успішні (наприклад, `fetch`) не
будуть виконуватись повторно — вони віддадуть готовий артефакт.

### 7.5  Витягти артефакти з MinIO

Кожен запуск заливає `current.parquet` (з `fetch`) та `scores.json`
(з `compute`) у бакет `argo-workflows`. Прямий доступ:

```bash
kubectl -n mlops-system port-forward svc/minio 9000:9000 &
export AWS_ACCESS_KEY_ID=minio AWS_SECRET_ACCESS_KEY=minio123
aws --endpoint-url http://localhost:9000 s3 ls \
    s3://argo-workflows/mlops-drift/<workflow-name>/ --recursive

# Скопіювати JSON-scores для конкретного запуску
aws --endpoint-url http://localhost:9000 s3 cp \
    s3://argo-workflows/mlops-drift/<workflow-name>/compute-drift/scores.json \
    /tmp/scores.json
jq . /tmp/scores.json
```

### 7.6  Заморозити / розморозити розклад

Планове обслуговування або гарячий інцидент — коли не хочемо, щоб
CronWorkflow смітив у логи ще одним падінням:

```bash
# Suspend (перестане створювати нові Workflow-run-и)
kubectl -n mlops-system patch cronworkflow mlops-drift \
        --type merge -p '{"spec":{"suspend":true}}'

# Resume
kubectl -n mlops-system patch cronworkflow mlops-drift \
        --type merge -p '{"spec":{"suspend":false}}'
```

### 7.7  Зафіксувати версію Evidently-образу

За замовчуванням `iris-drift` тягне `ghcr.io/bbeellkk-ua/goit-mlops-evidently:latest`.
Для reproducibility (аудит incident-а на конкретний SHA) можна
закріпити версію у CronWorkflow arguments:

```bash
kubectl -n mlops-system edit cronworkflow mlops-drift
# spec.workflowSpec.arguments.parameters:
#   - name: evidently-image
#     value: "ghcr.io/bbeellkk-ua/goit-mlops-evidently:<git-sha>"
```

