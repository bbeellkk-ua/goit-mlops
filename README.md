# Домашнє завдання №10 — Automated training pipeline: GitLab CI + AWS Step Functions

- **GitLab CI** реагує на `push` у гілку `main` і запускає AWS Step Functions
  execution, передаючи Git-контекст (`commit`, `branch`, `pipeline_id`).
- **AWS Step Functions** оркеструють послідовність кроків тренування:
  для цього завдання — спрощений workflow із двох Lambda-функцій
  `ValidateData → LogMetrics`.
- Інфраструктура повністю описана в **Terraform** (IAM-ролі, дві Lambda-функції,
  state machine, S3 remote state).

---

## 🧭 Архітектура

```
┌──────────────┐   push to main    ┌──────────────────────────┐
│  Developer   ├──────────────────►│         GitLab           │
└──────────────┘                   │  .gitlab-ci.yml → job    │
                                   │  train-model             │
                                   └───────────┬──────────────┘
                                               │  aws stepfunctions
                                               │  start-execution
                                               │  --input '{commit,branch,...}'
                                               ▼
                                   ┌──────────────────────────┐
                                   │   AWS Step Functions     │
                                   │   MLOpsPipeline (ASL)    │
                                   │                          │
                                   │   ┌──────────────────┐   │
                                   │   │  ValidateData    │   │  Lambda: validateData
                                   │   │  (Task, Retry×2) │   │
                                   │   └────────┬─────────┘   │
                                   │            ▼             │
                                   │   ┌──────────────────┐   │
                                   │   │   LogMetrics     │   │  Lambda: logMetrics
                                   │   │  (Task, End)     │   │
                                   │   └──────────────────┘   │
                                   └──────────────────────────┘
```

---

## 📁 Структура проєкту

```
goit-mlops/
├── terraform/
│   ├── main.tf              # IAM roles, Lambda funcs, Step Functions state machine
│   ├── data.tf              # trust policy для Step Functions + archive_file для Lambda ZIP
│   ├── terraform.tf         # required_version, aws/archive providers, S3 backend, default_tags
│   ├── variables.tf         # регіон, profile, назви ZIP-архівів
│   ├── outputs.tf           # ARN Lambda × 2, ARN state machine
│   └── lambda/
│       ├── validate.py      # mock валідації вхідних даних
│       ├── log_metrics.py   # mock логування метрик в MLflow
│       ├── validate.zip     # готовий пакет для aws_lambda_function
│       └── log_metrics.zip  # готовий пакет для aws_lambda_function
├── .gitlab-ci.yml           # stage train → job train-model → start-execution
├── .gitignore
└── README.md
```

---

## 🚀 Крок 1. Створення .zip-архівів Lambda

Файли `terraform/lambda/validate.zip` і `terraform/lambda/log_metrics.zip`
вже присутні в репозиторії. Пересобрати їх можна вручну:

```bash
cd terraform/lambda
zip -j validate.zip    validate.py
zip -j log_metrics.zip log_metrics.py
cd -
```

---

## 🏗 Крок 2. Розгортання інфраструктури (Terraform)

```bash
cd terraform
terraform init
terraform apply
```

Що створиться:

| Ресурс                         | Опис                                                            |
|--------------------------------|-----------------------------------------------------------------|
| `aws_iam_role.lambda_exec`     | Роль, яку приймають обидві Lambda-функції                       |
| `aws_iam_role_policy_attachment` | Прив'язка `AWSLambdaBasicExecutionRole` (CloudWatch Logs)     |
| `aws_lambda_function.validate` | Функція `validateData` (Python 3.11, handler `validate.handler`)|
| `aws_lambda_function.log_metrics` | Функція `logMetrics` (Python 3.11)                          |
| `aws_iam_role.stepfunction_exec` | Роль, яку приймає сервіс Step Functions                       |
| `aws_iam_role_policy.stepfunction_invoke_lambda` | Дозвіл `lambda:InvokeFunction`                |
| `aws_sfn_state_machine.mlops_pipeline` | Step Function `MLOpsPipeline`: `ValidateData → LogMetrics` |

Після успішного `apply` Terraform поверне значення `outputs`:

```text
lambda_validate_arn    = "arn:aws:lambda:eu-west-1:637512824156:function:validateData"
lambda_log_metrics_arn = "arn:aws:lambda:eu-west-1:637512824156:function:logMetrics"
stepfunction_arn       = "arn:aws:states:eu-west-1:637512824156:stateMachine:MLOpsPipeline"
```

---

## 🖱 Крок 3. Ручний запуск Step Function через AWS Console

1. Відкрийте **AWS Console → Step Functions → State machines → `MLOpsPipeline`**.
2. Натисніть **Start execution**.
3. Вставте у поле **Input** приклад JSON:

    ```json
    {
      "source": "manual",
      "commit": "test-abc123",
      "branch": "main",
      "pipeline_id": "manual-run",
      "triggered_by": "username"
    }
    ```

4. Натисніть **Start execution**.
5. У Graph inspector послідовно з'являться зелені `ValidateData` та `LogMetrics`.
6. У кроці **ExecutionSucceeded** перегляньте output — там буде відповідь від
   `logMetrics` (`{"status":"logged", ...}`).

Також перевірити виконання можна з CLI:

```bash
aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:eu-west-1:637512824156:stateMachine:MLOpsPipeline \
  --name "manual-$(date +%s)" \
  --input '{"source":"manual","commit":"test-abc123","branch":"main"}'
```

---

## 🤖 Крок 4. Налаштування GitLab CI/CD Variables

Для авторизації GitLab Runner у AWS ми використовуємо статичні ключі окремого
IAM-користувача `gitlab-ci` (див. секцію нижче про best practices та OIDC).

У GitLab: **Settings → CI/CD → Variables → Expand → Add variable**.

| Ключ                     | Значення                                                                                                          | Type      | Options            |
|--------------------------|-------------------------------------------------------------------------------------------------------------------|-----------|--------------------|
| `AWS_ACCESS_KEY_ID`      | Access key IAM-користувача `gitlab-ci`                                                                            | Variable  | ✅ Mask, ✅ Protect |
| `AWS_SECRET_ACCESS_KEY`  | Secret key IAM-користувача `gitlab-ci`                                                                            | Variable  | ✅ Mask, ✅ Protect |
| `AWS_DEFAULT_REGION`     | `eu-west-1`                                                                                                       | Variable  | –                  |
| `STATE_MACHINE_ARN`      | `arn:aws:states:eu-west-1:637512824156:stateMachine:MLOpsPipeline`                                                | Variable  | ✅ Protect         |

Мінімальний IAM policy для `gitlab-ci`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "states:StartExecution",
      "Resource": "arn:aws:states:eu-west-1:637512824156:stateMachine:MLOpsPipeline"
    }
  ]
}
```

---

## 🛠 Крок 5. Як працює `.gitlab-ci.yml`

```yaml
stages:
  - train

train-model:
  stage: train
  image:
    name: amazon/aws-cli:2.15.0
    entrypoint: [""]
  script:
    - aws stepfunctions start-execution \
        --state-machine-arn "${STATE_MACHINE_ARN}" \
        --name "training-${CI_PIPELINE_ID}-$(date +%s)" \
        --input "${STEPFN_INPUT}"
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
```

Ключові деталі (повний файл — див. `.gitlab-ci.yml`):

- **image** — офіційний образ `amazon/aws-cli:2.15.0` з відключеним
  entrypoint, щоб GitLab Runner міг виконувати shell-скрипт.
- **`aws stepfunctions start-execution`** — стартує нове виконання state
  machine.
- **`--name`** — унікальне ім'я `training-<pipeline_id>-<timestamp>`, за яким
  execution легко знайти в AWS Console.
- **`--input`** — JSON із Git-контекстом, який отримає перша Lambda:

    ```json
    {
      "source": "gitlab-ci",
      "commit": "abc1234",
      "branch": "main",
      "pipeline_id": "12345",
      "triggered_by": "username"
    }
    ```

- **`rules`** — job запускається на `push` у `main` та за ручним `web`-тригером
  з GitLab UI.
- **`artifacts.reports.dotenv`** — `executionArn` зберігається як artifact
  (`EXECUTION_ARN`), тож наступні джоби могли б використати його для перевірки
  статусу (`aws stepfunctions describe-execution --execution-arn ...`).

---

## ✅ Крок 6. Перевірка результату

1. **GitLab → CI/CD → Pipelines** — відкрити останній pipeline на `main`,
   переконатись що job `train-model` завершився успішно та у логах видно рядок
   `✅ Started execution: arn:aws:states:...`.
2. **AWS Console → Step Functions → State machines → `MLOpsPipeline` → Executions**
   — знайти нове виконання з іменем `training-<pipeline_id>-<timestamp>`.
3. У виконанні перевірити:
    - обидва стани (`ValidateData`, `LogMetrics`) відпрацювали зеленими;
    - поле **Input** містить переданий Git-контекст (`commit`, `branch`,
      `pipeline_id`);
    - у CloudWatch Logs (лінк з кроку) видно `print(...)` з Lambda.

---

## 🔐 Static keys vs OIDC

Для навчальних цілей ми використовуємо `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` — це найшвидший шлях, але не production-ready:

- ключі довгоживучі, потребують ручної ротації;
- легко потрапляють у логи або в коміти;
- складніше обмежити scope доступу.

Для production рекомендовано **OIDC-федерацію** GitLab з AWS IAM:

1. У AWS створити OIDC provider `https://gitlab.com`.
2. Створити IAM Role з trust policy, що приймає `sts:AssumeRoleWithWebIdentity`
   лише для конкретного репозиторію та branch (умови `aud` і `sub`).
3. У GitLab CI job запросити JWT через `id_tokens:` і викликати
   `aws sts assume-role-with-web-identity`, отримуючи **тимчасові** credentials
   на час виконання job.

Так job отримує саме той доступ, який потрібен, і лише на кілька хвилин.

---

## 🧹 Cleanup

```bash
cd terraform
terraform destroy
```

Terraform видалить обидві Lambda, дві IAM-ролі та state machine `MLOpsPipeline`.
S3 backend і сам бакет tfstate залишаться недоторканими.
