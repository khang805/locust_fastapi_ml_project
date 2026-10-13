"""The mock API must honour the same contract as the real API (otherwise load tests prove nothing)."""
import json
import time
import urllib.error
import urllib.request


def call(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_health(mock_api):
    status, body = call(mock_api.url, "/health")
    assert status == 200 and json.loads(body) == {"status": "ok"}


def test_predict_then_result(mock_api):
    status, body = call(mock_api.url, "/predict", {"text": "I love this wonderful thing"})
    assert status == 202
    payload = json.loads(body)
    assert payload["status"] == "Queued"
    deadline = time.time() + 5
    while time.time() < deadline:
        _, res = call(mock_api.url, f"/results/{payload['task_id']}")
        res = json.loads(res)
        if res["status"] == "SUCCESS":
            assert res["prediction"]["label"] == "POSITIVE"
            assert 0 <= res["prediction"]["score"] <= 1
            return
        time.sleep(0.1)
    raise AssertionError("task never finished")


def test_negative_text_is_classified_negative(mock_api):
    _, body = call(mock_api.url, "/predict", {"text": "terrible, I want a refund"})
    task_id = json.loads(body)["task_id"]
    time.sleep(0.5)
    _, res = call(mock_api.url, f"/results/{task_id}")
    assert json.loads(res)["prediction"]["label"] == "NEGATIVE"


def test_validation_errors(mock_api):
    assert call(mock_api.url, "/predict", {"text": "  "})[0] == 400
    assert call(mock_api.url, "/predict", {"nope": 1})[0] == 422


def test_unknown_task_is_pending(mock_api):
    status, body = call(mock_api.url, "/results/unknown")
    assert status == 200 and json.loads(body) == {"status": "PENDING"}


def test_metrics_and_openapi(mock_api):
    assert "http_request" in call(mock_api.url, "/metrics")[1]
    assert {"/predict", "/health", "/results/{task_id}"} <= set(json.loads(call(mock_api.url, "/openapi.json")[1])["paths"])
