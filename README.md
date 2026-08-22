# Домашнє завдання №9 — Experiment tracking з MLflow + Prometheus PushGateway

Уся інфраструктура (MinIO, PostgreSQL, MLflow Tracking Server, PushGateway)
розгортається декларативно через **ArgoCD**. Усі поди розміщуються на
infra-нодах кластера (`nodeSelector: node=infra` + toleration на taint
`node=infra:NoSchedule`).

> ℹ️ **Bitnami legacy images.** Bitnami перенесли безкоштовні образи в реєстр
> `docker.io/bitnamilegacy/*`. У values для MinIO та PostgreSQL явно вказано
> `image.repository: bitnamilegacy/...` і `global.security.allowInsecureImages: true`,
> щоб чарти працювали без Bitnami Premium.

---

## 🧭 Архітектура

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              Kubernetes cluster                          │
│                                                                          │
│   ns: application                             ns: monitoring             │
│   ┌────────────────────────────┐              ┌──────────────────────┐   │
│   │  minio (S3, :9000)         │◄──artifacts──│                      │   │
│   │  bucket: mlflow-artifacts  │              │  kube-prometheus-    │   │
│   └────────────────────────────┘              │  stack (Prometheus,  │   │
│                                               │  Grafana)            │   │
│   ┌────────────────────────────┐              │                      │   │
│   │  mlflow-postgres (:5432)   │              │        ▲             │   │
│   │  db=mlflow / user=mlflow   │◄──metadata───┤        │ scrape      │   │
│   └────────────────────────────┘              │        │ (ServiceMon)│   │
│                                               │        │             │   │
│   ┌────────────────────────────┐              │  ┌───────────────┐   │   │
│   │  mlflow Tracking (:5000)   │              │  │ pushgateway   │   │   │
│   │  UI / REST API             │              │  │  (:9091)      │   │   │
│   └────────────────────────────┘              │  └───────────────┘   │   │
│              ▲                                └──────────────────────┘   │
│              │ log_param / log_metric / log_model                        │
└──────────────┼───────────────────────────────────────────────────────────┘
               │              ▲                          ▲
               │              │ port-forward             │ port-forward
        ┌──────┴──────────────┴──────────────────────────┴─────────┐
        │           experiments/train_and_push.py                  │
        │  - grid search LogisticRegression (C × max_iter)         │
        │  - log params/metrics/model у MLflow                     │
        │  - push accuracy/loss у PushGateway (labels: run_id)     │
        │  - завантаження best model у ../best_model/              │
        └──────────────────────────────────────────────────────────┘
```

---

## 📁 Структура проєкту

```
goit-mlops/
├── argocd/
│   └── applications/
│       ├── minio.yaml            # MinIO (S3, bucket mlflow-artifacts)
│       ├── mlflow-postgres.yaml  # PostgreSQL для MLflow metadata
│       ├── mlflow.yaml           # MLflow Tracking Server (ClusterIP:5000)
│       └── pushgateway.yaml      # Prometheus PushGateway (ClusterIP:9091)
├── experiments/
│   ├── train_and_push.py         # тренує моделі, логує в MLflow, пушить у PushGateway
│   ├── requirements.txt
│   └── .env.example              # шаблон змінних середовища
├── best_model/                   # сюди завантажується найкраща модель
├── screenshots/                  # скріншоти MLflow UI + Grafana
└── README.md
```

---

## 🚀 Крок 1. Розгортання інфраструктури через ArgoCD

Усі `Application`-и налаштовані на **project: `default`** і `syncPolicy.automated`,
тому після `kubectl apply` синхронізація відбувається автоматично.

```bash
kubectl apply -f argocd/applications/minio.yaml
kubectl apply -f argocd/applications/mlflow-postgres.yaml
kubectl apply -f argocd/applications/mlflow.yaml
kubectl apply -f argocd/applications/pushgateway.yaml
```

Перевірити стан у ArgoCD UI або через CLI:

```bash
argocd app list
argocd app get mlflow
```

### Перевірка подів

```bash
kubectl get pods -n application  | grep -E 'mlflow|minio|postgres'
kubectl get pods -n monitoring   | grep pushgateway
```

Очікуваний вигляд:

```
NAME                                            READY   STATUS
minio-xxxxxxxxxx-xxxxx                          1/1     Running
mlflow-postgres-postgresql-0                    1/1     Running
mlflow-xxxxxxxxxx-xxxxx                         1/1     Running
pushgateway-prometheus-pushgateway-xxxxx        1/1     Running
```

### Перевірка ServiceMonitor (Prometheus)

PushGateway публікує `ServiceMonitor` з міткою `release: kube-prometheus-stack`
— саме її очікує наш Prometheus.

```bash
kubectl get servicemonitor -n monitoring pushgateway-prometheus-pushgateway \
  -o jsonpath='{.metadata.labels}'
# → {"release":"kube-prometheus-stack", ...}
```

У Prometheus UI (`Status → Targets`) має з'явитися job
`monitoring/pushgateway-prometheus-pushgateway` у стані `UP`.

---

## 🔌 Крок 2. Port-forward до сервісів

Скрипт запускається локально, тож пробрасуємо порти сервісів у localhost:

```bash
kubectl port-forward -n application svc/mlflow 5000:5000 &
kubectl port-forward -n application svc/minio 9000:9000 &
kubectl port-forward -n monitoring  svc/pushgateway-prometheus-pushgateway 9091:9091 &
```

Перевірка доступності:

- MLflow UI:    <http://localhost:5000>
- MinIO API:    <http://localhost:9000> (S3 endpoint, у браузері віддає 403 — це нормально)
- PushGateway:  <http://localhost:9091>

---

## 🐍 Крок 3. Локальне середовище та `.env`

```bash
cd experiments

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
```

За замовчуванням значення у `.env` вже відповідають конфігурації ArgoCD
Applications (creds MinIO та PostgreSQL з values). Змінюйте лише за
необхідності.

---

## 🧪 Крок 4. Запуск експериментів

```bash
python train_and_push.py
```

Що відбувається:

1. Створюється (або підхоплюється) експеримент **`Iris Classification HW9`**.
2. Виконуються **8 запусків** LogisticRegression по grid:
   - `C ∈ {0.01, 0.1, 1.0, 10.0}`
   - `max_iter ∈ {100, 500}`
3. Для кожного run:
   - `mlflow.log_param("C", …)`, `log_param("max_iter", …)`;
   - `mlflow.log_metric("accuracy", …)`, `log_metric("loss", …)`;
   - `mlflow.sklearn.log_model(model, "model")` → artifact у MinIO (`s3://mlflow-artifacts/…`);
   - `push_to_gateway(...)` → метрики `mlflow_accuracy` та `mlflow_loss`
     з labels `run_id`, `run_name` у PushGateway (job=`mlflow_experiments`).
4. Після завершення скрипт знаходить run з максимальною accuracy й
   завантажує його `model/` artifact у **`../best_model/`** разом із
   summary-файлом `BEST_RUN.txt`.

Приклад підсумкового виводу:

```
📊 Підсумок запусків:
   run_name                    accuracy       loss
   C=10.0_iter=500               1.0000     0.0489
   C=10.0_iter=100               1.0000     0.0489
   C=1.0_iter=500                1.0000     0.0619
   ...
🏆 Найкращий run: run_id=abc123... accuracy=1.0000 loss=0.0489
📦 Модель завантажено у: /…/best_model/model
```

---

## 🖥️ Крок 5. Перегляд у MLflow UI

Відкрийте <http://localhost:5000> → експеримент **`Iris Classification HW9`**.

Для кожного run доступні:

- **Parameters**: `C`, `max_iter`;
- **Metrics**: `accuracy`, `loss`;
- **Artifacts**: `model/` (Pickle + `MLmodel` + `conda.yaml` / `python_env.yaml`);
- **Tags**: `hw=lesson-9`, `model_family=LogisticRegression`;
- службова інформація: host, user, start/end time.

Можна вибрати кілька runs і натиснути **Compare** — MLflow побудує
таблицю параметрів і графік метрик між ними.

Скріншоти — див. `screenshots/`.

---

## 📈 Крок 6. Метрики у Grafana

У Grafana → **Explore → Prometheus**:

```promql
# Пари accuracy/loss по run_name
mlflow_accuracy
mlflow_loss
```

Корисні запити:

```promql
# Найкраща accuracy серед усіх запусків
max(mlflow_accuracy)

# Топ-5 запусків за accuracy
topk(5, mlflow_accuracy)
```

Метрики мають labels `run_id`, `run_name`, `exported_job`, `instance`,
що дає можливість фільтрувати по конкретному запуску.

Скріншоти — див. `screenshots/`.

---

## 🏆 Крок 7. Найкраща модель

Після виконання `train_and_push.py` у `best_model/` з'являється:

```
best_model/
├── BEST_RUN.txt        # run_id, accuracy, loss найкращого запуску
└── model/
    ├── MLmodel
    ├── conda.yaml
    ├── python_env.yaml
    ├── requirements.txt
    └── model.pkl
```

Модель можна одразу підвантажити:

```python
import mlflow.sklearn
model = mlflow.sklearn.load_model("best_model/model")
```

---

## 🧹 Очистка

```bash
kubectl delete -f argocd/applications/
```

ArgoCD видалить Applications, а разом з ними — release-и Helm.
