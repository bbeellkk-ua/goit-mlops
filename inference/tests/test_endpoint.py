"""Integration-тести inference-endpoint — Блок F1."""

from __future__ import annotations


def _sample():
    return {"sepal_length": 5.1, "sepal_width": 3.5, "petal_length": 1.4, "petal_width": 0.2}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"


def test_readyz(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["model_ready"] is True


def test_model_info(client):
    r = client.get("/model")
    assert r.status_code == 200
    body = r.json()
    assert body["model_name"] == "iris-classifier-it"
    assert body["checksum_sha256"]  # non-empty
    assert body["stage"] == "Production"


def test_predict_ok(client):
    r = client.post("/predict", json={"instances": [_sample()]})
    assert r.status_code == 200
    body = r.json()
    assert body["model_version"]
    assert len(body["predictions"]) == 1
    p = body["predictions"][0]
    assert p["predicted_class"] in {0, 1, 2}
    assert p["predicted_label"] in {"setosa", "versicolor", "virginica"}
    assert set(p["probabilities"].keys()) == {"setosa", "versicolor", "virginica"}


def test_predict_bad_input_returns_400(client):
    r = client.post("/predict", json={"instances": [{"sepal_length": "foo"}]})
    assert r.status_code == 400
    assert "detail" in r.json()


def test_predict_extra_field_rejected(client):
    bad = _sample() | {"malicious": 1}
    r = client.post("/predict", json={"instances": [bad]})
    assert r.status_code == 400


def test_metrics_endpoint_prometheus_format(client):
    # Спочатку викликаємо predict щоб було що показати
    client.post("/predict", json={"instances": [_sample()]})
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "inference_requests_total" in body
    assert "inference_predictions_total" in body


def test_rate_limit(client):
    # ліміт 5/min встановлено у conftest.
    # Попередні тести уже витратили частину квоти на цю ж IP (testclient),
    # тому обнуляємо in-memory storage лімітера перед перевіркою.
    from app.main import app as _app

    _app.state.limiter.reset()

    for _ in range(5):
        r = client.post("/predict", json={"instances": [_sample()]})
        assert r.status_code == 200
    r = client.post("/predict", json={"instances": [_sample()]})
    assert r.status_code == 429
