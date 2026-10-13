"""
Unit-test setup for the real FastAPI app. No Redis, no model download, no torch needed:
`transformers` is stubbed when missing and Celery calls are replaced with fakes.
"""
import os
import sys
import types

import pytest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

try:  # pragma: no cover
    import transformers  # noqa: F401
except Exception:  # not installed in CI / on a laptop -> provide a stub module
    stub = types.ModuleType("transformers")
    stub.pipeline = lambda *a, **k: (lambda text: [{"label": "POSITIVE", "score": 0.99}])
    sys.modules["transformers"] = stub


class FakeAsyncResult:
    """Replacement for celery.result.AsyncResult; behaviour is set per test through class attributes."""
    status = "PENDING"
    result = None
    info = None

    def __init__(self, task_id, app=None):
        self.id = task_id


@pytest.fixture
def api(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    calls = []

    class FakeTask:
        id = "task-123"

    class FakeRunner:
        @staticmethod
        def delay(text):
            calls.append(text)
            return FakeTask()

    monkeypatch.setattr(main, "run_sentiment_analysis", FakeRunner)
    monkeypatch.setattr(main, "AsyncResult", FakeAsyncResult)
    FakeAsyncResult.status, FakeAsyncResult.result, FakeAsyncResult.info = "PENDING", None, None
    client = TestClient(main.app)
    client.enqueued = calls
    client.fake_result = FakeAsyncResult
    return client
