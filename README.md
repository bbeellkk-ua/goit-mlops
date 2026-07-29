# Домашнє завдання №3 — Bash + Docker для PyTorch inference

Домашнє завдання поєднує теми **«Основи Linux/Bash-скриптування»** і
**«Контейнеризація ML-моделей»**. У проєкті:

- готуємо середовище через bash-скрипт (`scripts/install_dev_tools.sh`);
- експортуємо претреновану модель `MobileNet V2` у TorchScript (`model/model.pt`);
- пишемо CLI inference-скрипт з top-3 передбаченнями (`app/inference.py`);
- будуємо два Docker-образи для inference — «важкий» `Dockerfile.fat`
  і оптимізований multi-stage `Dockerfile.slim`;
- порівнюємо їх (розмір, шари, час збірки, inference-результат) — див.
  [`report.md`](./report.md).

---

## Структура проєкту

```
lesson-3/
├── app/
│   └── inference.py           # CLI-скрипт inference (top-3)
├── model/
│   └── model.pt               # TorchScript-модель (створюється export_model.py)
├── scripts/
│   └── install_dev_tools.sh   # ідемпотентний bash-скрипт підготовки середовища
├── export_model.py            # експорт mobilenet_v2 → TorchScript
├── requirements.txt           # torch / torchvision / pillow (фіксовані версії)
├── Dockerfile.fat             # «важкий» образ на python:3.13
├── Dockerfile.slim            # оптимізований multi-stage на python:3.13-slim
├── .dockerignore              # виключення для docker build context
├── example.jpg                # тестове зображення (Wikimedia Commons)
├── install.log                # лог bash-скрипта
├── comparison.txt             # сирі дані порівняння образів (docker history)
├── report.md                  # звіт з порівнянням fat vs slim
└── README.md                  # цей файл
```

---

## Вимоги

- macOS / Linux
- Python **≥ 3.13** (у прикладі — `python3.13` з Homebrew)
- Docker Desktop **≥ 4.x** (містить Docker Compose V2)
- ~2 ГБ вільного місця для fat-образу

---

## Крок 1. Підготовка середовища (bash-скрипт)

Скрипт `scripts/install_dev_tools.sh`:

- перевіряє наявність `docker`, `docker compose version`, Python ≥ 3.13, `pip3`;
- створює локальний `.venv/` через знайдений `python3.13`;
- встановлює у venv `torch`, `torchvision`, `pillow` (з `requirements.txt`);
- пише все у `install.log`;
- **ідемпотентний**: повторний запуск не перевстановлює наявні компоненти.

Запуск:

```bash
bash scripts/install_dev_tools.sh
source .venv/bin/activate
```

Після цього:

```bash
docker --version
docker compose version
python3 --version
pip3 --version
python3 -c "import torch, torchvision, PIL; print(torch.__version__, torchvision.__version__, PIL.__version__)"
```

---

## Крок 2. Експорт моделі у TorchScript

`export_model.py` завантажує `mobilenet_v2` через сучасний `weights=` API
(`MobileNet_V2_Weights.DEFAULT`), переводить у `eval()`, робить
`torch.jit.trace(...)` і зберігає у `model/model.pt`.

```bash
python3 export_model.py
```

Очікуваний вивід:

```
[OK] TorchScript-модель збережено: model/model.pt
[OK] Тестовий forward-pass: output.shape = (1, 1000)
```

---

## Крок 3. Локальний inference

```bash
python3 app/inference.py example.jpg
```

Приклад результату (тестове зображення — жовтий лабрадор):

```
Image: example.jpg
Top-3 predictions (MobileNet V2, ImageNet):
  #1: class_id= 208  confidence=0.1320  name=Labrador retriever
  #2: class_id= 209  confidence=0.0274  name=Chesapeake Bay retriever
  #3: class_id= 273  confidence=0.0228  name=dingo
```

`app/inference.py`:

- приймає шлях до зображення як CLI-аргумент;
- шукає `model/model.pt` у локальному репо та у `/app/model/model.pt` (для Docker);
- використовує офіційний `weights.transforms()` (правильний resize/normalize);
- інференс під `torch.no_grad()`;
- друкує top-3 `(class_id, name, confidence)`.

---

## Крок 4. Збірка Docker-образів

**Fat (важкий):**

```bash
docker build -f Dockerfile.fat -t ml-infer-fat:1.0 .
```

**Slim (оптимізований, multi-stage):**

```bash
docker build -f Dockerfile.slim -t ml-infer-slim:1.0 .
```

---

## Крок 5. Запуск inference у контейнерах

Локальний файл `example.jpg` монтуємо в контейнер через bind mount:

```bash
# Fat
docker run --rm \
  -v "$(pwd)/example.jpg:/app/example.jpg:ro" \
  ml-infer-fat:1.0 /app/example.jpg

# Slim
docker run --rm \
  -v "$(pwd)/example.jpg:/app/example.jpg:ro" \
  ml-infer-slim:1.0 /app/example.jpg
```

Обидва контейнери повертають **однаковий** top-3 результат:

```
Image: /app/example.jpg
Top-3 predictions (MobileNet V2, ImageNet):
  #1: class_id= 208  confidence=0.1320  name=Labrador retriever
  #2: class_id= 209  confidence=0.0274  name=Chesapeake Bay retriever
  #3: class_id= 273  confidence=0.0228  name=dingo
```

---

## Крок 6. Порівняння образів

```bash
docker images | grep ml-infer
docker history --no-trunc ml-infer-fat:1.0
docker history --no-trunc ml-infer-slim:1.0
```

Реальні виміри (macOS, Apple Silicon, docker 29):

| Метрика                | Fat                     | Slim                   |
|------------------------|-------------------------|------------------------|
| Розмір image           | **1.83 GB**             | **795 MB**             |
| Кількість шарів        | 20                      | 16                     |
| Час збірки (`--no-cache`, базовий Python-image закешовано) | 30.67 s | 40.64 s |
| Найважчий власний шар  | ~644 MB (pip install)   | ~636 MB (COPY /install)|
| Найважчий базовий шар  | ~641 MB (build-deps)    | ~100 MB (debian slim)  |
| Inference top-3        | Labrador retriever ✅    | Labrador retriever ✅   |

Детальний аналіз — у [`report.md`](./report.md).
Сирі дані — у [`comparison.txt`](./comparison.txt).

---

## Тестове зображення

`example.jpg` — фото «Yellow Labrador looking» з Wikimedia Commons:
<https://upload.wikimedia.org/wikipedia/commons/2/26/YellowLabradorLooking_new.jpg>

Ліцензія: [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).

Завантажене командою:

```bash
curl -sSL -o example.jpg "https://upload.wikimedia.org/wikipedia/commons/2/26/YellowLabradorLooking_new.jpg"
```
