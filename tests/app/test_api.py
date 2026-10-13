"""Contract tests for app/main.py - the same contract the Locust load test relies on."""


def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_predict_enqueues_task(api):
    r = api.post("/predict", json={"text": "I love it"})
    assert r.status_code == 202
    assert r.json() == {"task_id": "task-123", "status": "Queued"}
    assert api.enqueued == ["I love it"]


def test_predict_rejects_blank_text(api):
    r = api.post("/predict", json={"text": "   "})
    assert r.status_code == 400
    assert api.enqueued == []


def test_predict_rejects_missing_field(api):
    assert api.post("/predict", json={"wrong": "x"}).status_code == 422


def test_result_pending(api):
    r = api.get("/results/abc")
    assert r.status_code == 200
    assert r.json() == {"status": "PENDING"}


def test_result_success(api):
    api.fake_result.status = "SUCCESS"
    api.fake_result.result = {"label": "POSITIVE", "score": 0.98}
    r = api.get("/results/abc")
    assert r.json() == {"status": "SUCCESS", "prediction": {"label": "POSITIVE", "score": 0.98}}


def test_result_failure(api):
    api.fake_result.status = "FAILURE"
    api.fake_result.info = RuntimeError("model exploded")
    r = api.get("/results/abc")
    assert r.json() == {"status": "FAILURE", "error": "model exploded"}


def test_metrics_endpoint_exposes_prometheus_text(api):
    api.get("/health")
    r = api.get("/metrics")
    assert r.status_code == 200
    assert "http_request" in r.text   # the locustfile checks for exactly this substring


def test_openapi_contains_routes_checked_by_smoke_user(api):
    paths = api.get("/openapi.json").json()["paths"]
    assert {"/predict", "/health", "/results/{task_id}"} <= set(paths)


def test_result_status_is_read_only_once_even_if_task_finishes_midway(api, monkeypatch):
    """
    Regression test (found by the Locust load test against the real stack): AsyncResult.status hits Redis on every
    access while a task is unfinished. A task finishing between two reads produced {"status": "SUCCESS"} with no
    "prediction" key.
    """
    import main

    reads = iter(["STARTED", "STARTED", "SUCCESS", "SUCCESS", "SUCCESS"])

    class RacyResult:
        result = {"label": "POSITIVE", "score": 0.9}
        info = None

        def __init__(self, task_id, app=None):
            pass

        @property
        def status(self):
            return next(reads)

    monkeypatch.setattr(main, "AsyncResult", RacyResult)
    body = api.get("/results/abc").json()
    assert body == {"status": "STARTED"}              # one consistent snapshot, never SUCCESS-without-prediction
    assert not (body["status"] == "SUCCESS" and "prediction" not in body)
