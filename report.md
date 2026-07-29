# Звіт: порівняння Fat vs Slim Docker-образів для inference MobileNet V2

**Обладнання:** macOS Sequoia, Apple Silicon (arm64), Docker 29.6.2 (BuildKit).
**Модель:** `torchvision.models.mobilenet_v2` (ImageNet, TorchScript через `torch.jit.trace`).
**Тестове зображення:** `example.jpg` (Yellow Labrador, Wikimedia Commons).

---

## 1. Підсумкова таблиця

| Метрика                                     | **Fat image** (`ml-infer-fat:1.0`) | **Slim image** (`ml-infer-slim:1.0`) |
|---------------------------------------------|------------------------------------|--------------------------------------|
| Базовий образ                               | `python:3.13`                      | `python:3.13-slim` (multi-stage)     |
| Розмір image                                | **1.83 GB**                        | **795 MB** (≈ **2.3× менше**)        |
| Кількість шарів (`docker history`)          | **20**                             | **16**                               |
| Час збірки, `--no-cache` (base image cached)| **30.67 s**                        | **40.64 s**                          |
| Час rebuild (усе закешовано)                | ~1.35 s                            | ~1–2 s                               |
| Найважчий шар (додатковий, наш)             | `pip install torch/torchvision/pillow` — **644 MB** | `COPY --from=builder /install` — **636 MB** |
| Найважчі шари базового образу               | `python:3.13` build-deps (**641 MB**) + Python (**72.5 MB**) + перший Debian layer (**142 MB**) | `python:3.13-slim`: перший Debian layer (**100 MB**) + Python (**38.8 MB**) |
| Системні пакети апт-шар (наш `RUN apt-get`) | 46.8 MB (`build-essential curl wget git vim libglib2.0-0 libsm6 libxrender1 libxext6`) | 1.53 MB (лише `libgomp1`)            |
| Inference (top-3)                           | Labrador retriever / Chesapeake Bay retriever / dingo | Labrador retriever / Chesapeake Bay retriever / dingo |
| Числова похибка (confidence)                | `0.1320 / 0.0274 / 0.0228`         | `0.1320 / 0.0274 / 0.0228` — **збіг**|
| Зайві інструменти в runtime                 | `git`, `curl`, `wget`, `vim`, `build-essential`, повний Debian toolchain | Немає (лише `libgomp1` для torch)   |

> Сирі дані — див. `comparison.txt` (`docker images` + `docker history --no-trunc`).

---

## 2. Що робить fat-образ важким

`docker history ml-infer-fat:1.0` показує кілька справді великих шарів:

1. **~641 MB** — базовий шар `python:3.13`, у якому встановлений повний набір
   build-tools (`autoconf`, `automake`, `g++`, `gcc`, `imagemagick`,
   `libxml2-dev`, `libssl-dev`, `libpq-dev`, `libjpeg-dev` …). Ці залежності
   потрібні для збірки С-розширень з source, але для запуску вже готових
   `torch`/`torchvision` wheel-ів вони не потрібні.
2. **~644 MB** — власний `pip install -r requirements.txt`
   (`torch==2.7.0`, `torchvision==0.22.0`, `pillow==11.2.1` + транзитивні
   `numpy`, `sympy`, `networkx`, `fsspec`, `jinja2` …). Це в основному сам PyTorch.
3. **~191 MB** — `git`, `mercurial`, `openssh-client`, `subversion`, `procps`
   у базовому образі. Для inference — зайве.
4. **~142 MB** — базовий Debian rootfs `python:3.13`.
5. **~72.5 MB** — сам Python-інтерпретатор.
6. **~46.8 MB** — наш власний `apt-get install build-essential curl wget git vim …`,
   доданий у Dockerfile.fat навмисно, щоб реалістично змоделювати
   «development-friendly» образ.
7. **~58.9 MB** — `ca-certificates`, `curl`, `gnupg`, `netbase`, `sq`, `wget`
   у базовому образі.
8. Ще менші, але зайві шари з `libbluetooth-dev`, `tk-dev`, `uuid-dev` тощо.

Разом «фонові» пакети базового `python:3.13` (build-deps + git-tooling + curl-stack)
дають ~**0.9 GB** одразу. І це ще до нашого `pip install`.

---

## 3. Що змінилось у slim-образі

`Dockerfile.slim` — це чіткий multi-stage build:

```
builder  →  installs torch/torchvision/pillow  →  /install
runtime  →  copies only /install + app/ + model/
```

Ключові рішення:

- **`python:3.13-slim`** як обидва базові образи — тільки runtime-Python
  без build-tools, git, curl, imagemagick тощо. Це прибрало ~**0.9 GB** «на
  автоматі».
- **Builder-stage встановлює пакети в окремий префікс** через
  `pip install --prefix=/install`. У runtime ми потім
  `COPY --from=builder /install /usr/local` — і забираємо **тільки готові
  `site-packages`**, без pip-кешів, wheels, `build-essential`, git.
- **Runtime отримує лише `libgomp1`** (1.53 MB) — той єдиний системний
  runtime, який реально потрібний CPU-версії PyTorch. Ніяких
  `libglib2.0-0/libsm6/libxrender1/libxext6` тут не потрібно, бо ми не
  використовуємо OpenCV.
- **`.dockerignore`** прибирає `.git`, `__pycache__`, `.venv`, кеші, архіви,
  сам `README.md`/`report.md`/`Dockerfile.*` — щоб не роздувати build context.
- **Порядок COPY**: спочатку `requirements.txt` → `pip install`, лише потім
  `app/` та `model/`. Якщо змінюється тільки код, Docker cache перевикористає
  важкий шар з залежностями.

Результат: **1.83 GB → 795 MB**, приблизно **2.3× менше**.

---

## 4. Чи змінився inference?

Ні. Обидва контейнери повертають **бітово однаковий** top-3:

```
#1: class_id=208  confidence=0.1320  name=Labrador retriever
#2: class_id=209  confidence=0.0274  name=Chesapeake Bay retriever
#3: class_id=273  confidence=0.0228  name=dingo
```

Це очікувано, бо:

- версії `torch==2.7.0`, `torchvision==0.22.0`, `pillow==11.2.1` **зафіксовані**
  у `requirements.txt` і встановлюються в обох контейнерах;
- `model/model.pt` — той самий TorchScript-артефакт (побудований локально,
  просто копіюється у контейнер);
- preprocessing один і той самий — `MobileNet_V2_Weights.DEFAULT.transforms()`;
- CPU-inference — детермінований, без залежності від системних libs (за
  винятком OpenMP runtime, який ідентичний і там, і там: `libgomp1`).

Тобто **оптимізація не змінила ML-поведінку моделі**, лише розмір/структуру
образу — саме те, чого ми хотіли досягти.

---

## 5. Чому час першої збірки slim > fat

На перший погляд контрінтуїтивно: slim займає **40.64 s**, а fat — **30.67 s**.
Причини:

1. **Multi-stage overhead.** Slim спочатку створює builder-stage, встановлює
   туди `torch`/`torchvision`/`pillow`, потім піднімає окремий runtime-stage,
   ставить туди `libgomp1` і робить `COPY --from=builder /install /usr/local`.
   Fat робить те саме, але в один прохід.
2. **Fat використовував кешований `python:3.13`** (уже завантажений з
   Docker Hub у попередніх запусках), тоді як шари між builder і runtime
   у slim довелося серіалізувати окремо.
3. **BuildKit stage-parallelism** тут не допомагає, бо стейджі залежні:
   runtime чекає на builder.

Проте **rebuild** slim (без змін у `requirements.txt`) — так само швидкий,
як і fat, бо шар з залежностями кешується. А в CI/CD типовий сценарій — це
саме rebuild з кешем, тому slim там об'єктивно виграє (менший pull,
менше layers, менший push).

---

## 6. Пропозиції для подальшої оптимізації

Що можна зробити далі, щоб зменшити slim ще:

1. **CPU-only torch build**. Дефолтний `torch` wheel тягне ~пів-гігабайта коду.
   Замість `torch==2.7.0` можна встановити CPU-варіант з окремого індексу:
   ```
   pip install --index-url https://download.pytorch.org/whl/cpu torch==2.7.0+cpu
   ```
   На x86 це знімає ~200–300 MB (менш актуально для arm64).
2. **`--extra-index-url` + `torch-cpu` тільки з потрібними компонентами.**
   Часто не потрібен ані CUDA, ані `torch.distributed`, ані `torch.onnx`.
3. **Прибрати `numpy`, якщо він не використовується у коді напряму.** Ми його
   не імпортуємо, але torch тягне.
4. **`python:3.13-slim` → `distroless/python3-debian12` / `chainguard`
   /`gcr.io/distroless`**. Прибирає shell, apt, coreutils — attack surface
   мінімальний, але дебажити важче.
5. **Кешувати `torch` wheel як окремий шар / build artifact** (`RUN --mount=type=cache,target=/root/.cache/pip`).
6. **Використати `torch.export` / ONNX Runtime** — inference-only рантайм
   (`onnxruntime`) сам по собі < 100 MB, весь Python-стек не потрібен.
7. **Model weights як окремий Docker layer / OCI artifact.** Тоді rebuild
   inference-коду не інвалідатить шар з моделлю (у нас `model/` копіюється
   передостаннім, це вже добре).
8. **strip debug symbols** у Python-shared-libs (як це вже робить `slim`,
   але можна ще агресивніше).
9. **`.pyc` cache prune**. `PYTHONDONTWRITEBYTECODE=1` уже задано, але можна
   ще після встановлення прибрати `__pycache__` з site-packages.
10. **Non-root user** у runtime (безпека, не розмір).

---

## 7. Висновок

- Slim-образ ~**2.3× менший** за fat (795 MB проти 1.83 GB) і має **менше
  шарів** (16 vs 20) при **ідентичному** inference-результаті.
- Основну «вагу» fat-образу дає **не наш код і не модель**, а **базовий
  `python:3.13`** з повним набором build-tools і dev-утиліт (~0.9 GB).
- Multi-stage build (`Dockerfile.slim`) прибирає цю вагу, залишаючи в
  runtime тільки те, що реально виконує inference: Python + `torch` +
  `torchvision` + `pillow` + `libgomp1` + модель + inference-скрипт.
- Оптимізація **не змінила** ML-поведінку моделі — top-3 передбачення
  ідентичні до останнього знаку.
- Для подальшої оптимізації в реальному production я б перевів inference
  на **CPU-only wheel** (`+cpu`) або взагалі на **ONNX Runtime** з окремим
  model-artifact-шаром — це реалістично дало б ще ×2–×3 зменшення розміру.
