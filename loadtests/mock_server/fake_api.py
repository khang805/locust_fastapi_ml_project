"""
A tiny stand-in for the InferQueue API (same HTTP contract, no Redis / Celery / torch).

Why it exists
-------------
* lets you try every Locust feature in seconds, without downloading a 250 MB model
* makes the load-test suite runnable in CI and in unit tests
* lets you inject latency / errors to prove that Locust really detects failures

Contract (identical to app/main.py)
    GET  /health            -> 200 {"status": "ok"}
    POST /predict           -> 202 {"task_id": str, "status": "Queued"}   | 400 empty text | 422 bad body
    GET  /results/{task_id} -> 200 {"status": "PENDING|STARTED|SUCCESS|FAILURE", ...}
    GET  /metrics           -> Prometheus text

Tunables (environment variables)
    MOCK_LATENCY_MIN / MOCK_LATENCY_MAX   seconds a "task" takes to finish (default 0.05 / 0.25)
    MOCK_ERROR_RATE                       fraction (0-1) of requests answered with HTTP 500
    MOCK_TASK_FAILURE_RATE                fraction (0-1) of tasks that end in FAILURE
"""
import os
import random
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="InferQueue MOCK API")

_tasks = {}
_lock = threading.Lock()
_counters = {"requests": 0}


def _f(name, default):
    return float(os.getenv(name, default))


class TextRequest(BaseModel):
    text: str


def _maybe_fail():
    if random.random() < _f("MOCK_ERROR_RATE", 0):
        raise HTTPException(status_code=500, detail="injected failure")


def _classify(text):
    negative_words = ("terrible", "awful", "worst", "disappointed", "crash", "refund", "broke")
    label = "NEGATIVE" if any(w in text.lower() for w in negative_words) else "POSITIVE"
    return {"label": label, "score": round(random.uniform(0.80, 0.999), 4)}


@app.get("/health")
def health():
    _counters["requests"] += 1
    _maybe_fail()
    return {"status": "ok"}


@app.post("/predict", status_code=202)
def predict(payload: TextRequest):
    _counters["requests"] += 1
    _maybe_fail()
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Text payload cannot be empty.")
    task_id = str(uuid.uuid4())
    duration = random.uniform(_f("MOCK_LATENCY_MIN", 0.05), _f("MOCK_LATENCY_MAX", 0.25))
    fails = random.random() < _f("MOCK_TASK_FAILURE_RATE", 0)
    with _lock:
        _tasks[task_id] = {"ready_at": time.monotonic() + duration, "text": payload.text, "fails": fails}
    return {"task_id": task_id, "status": "Queued"}


@app.get("/results/{task_id}")
def get_result(task_id: str):
    _counters["requests"] += 1
    _maybe_fail()
    task = _tasks.get(task_id)
    if task is None:
        # Celery reports unknown ids as PENDING – mirror that behaviour
        return {"status": "PENDING"}
    remaining = task["ready_at"] - time.monotonic()
    if remaining > 0:
        return {"status": "STARTED" if remaining < 0.1 else "PENDING"}
    if task["fails"]:
        return {"status": "FAILURE", "error": "injected task failure"}
    return {"status": "SUCCESS", "prediction": _classify(task["text"])}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return (
        "# HELP http_requests_total Total number of requests\n"
        "# TYPE http_requests_total counter\n"
        f"http_requests_total {_counters['requests']}\n"
        "# HELP http_request_duration_seconds Request latency\n"
        "# TYPE http_request_duration_seconds histogram\n"
    )
