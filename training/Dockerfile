# syntax=docker/dockerfile:1.7
# Training image — виконується як Kubernetes Job у ns mlops-system.
# ARG BASE_IMAGE — рівно один у нас; multi-stage не потрібен.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Створюємо непривілейованого користувача.
RUN groupadd --system --gid 1000 mlops \
 && useradd --system --uid 1000 --gid mlops --home /home/mlops --shell /bin/bash mlops \
 && mkdir -p /app /home/mlops && chown -R mlops:mlops /app /home/mlops

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=mlops:mlops . /app

USER mlops

# За замовчуванням запускаємо training run. У K8s Job переозначується command.
ENTRYPOINT ["python"]
CMD ["train.py"]
