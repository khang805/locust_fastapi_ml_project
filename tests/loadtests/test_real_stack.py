"""
Runs Locust against the REAL application: redis-server + Celery worker + FastAPI (uvicorn).
The Hugging Face model is replaced by loadtests/stubs/transformers (no torch / no download needed).
Skipped automatically when `redis-server` is not installed.
"""
import os
import shutil
import subprocess
import sys

import pytest

from .conftest import LOADTESTS, ROOT, free_port, wait_http

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server is not installed"),
]

APP_DIR = os.path.join(ROOT, "app")


@pytest.fixture(scope="module")
def real_stack():
    redis_port, api_port = free_port(), free_port()
    env = {
        **os.environ,
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": str(redis_port),
        "PYTHONPATH": os.path.join(LOADTESTS, "stubs"),
    }
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs = [
        subprocess.Popen(["redis-server", "--port", str(redis_port), "--save", "", "--appendonly", "no"], **quiet),
    ]
    try:
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "celery", "-A", "celery_app.celery_app", "worker", "--loglevel=warning", "-c", "2"],
            cwd=APP_DIR, env=env, **quiet))
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(api_port), "--log-level", "warning"],
            cwd=APP_DIR, env=env, **quiet))
        wait_http(f"http://127.0.0.1:{api_port}/health", timeout=40)
        yield f"http://127.0.0.1:{api_port}"
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def test_smoke_suite_passes_against_real_api(run_locust, real_stack, tmp_path):
    r = run_locust("--config", "conf/smoke.conf", "--poll-interval", "0.1", "--run-time", "10s",
                   "--html", str(tmp_path / "smoke.html"), host=real_stack)
    assert r.returncode == 0, r.log[-3000:]
    assert r.count("/openapi.json [smoke]") == 1
    assert r.count("predict -> SUCCESS", "E2E") == 1 and r.failures("predict -> SUCCESS") == 0
    assert r.failures("Aggregated") == 0


def test_load_run_passes_sla_against_real_api(run_locust, real_stack):
    r = run_locust("--headless", "-u", "25", "-r", "25", "-t", "20s", "--poll-interval", "0.2",
                   "--max-fail-ratio", "0", "--p95-limit-ms", "3000", host=real_stack)
    assert r.returncode == 0, r.log[-3000:]
    assert r.count("/predict") > 50
    assert r.count("predict -> SUCCESS", "E2E") > 20       # tasks really travelled API -> Redis -> worker -> result
    assert r.failures("Aggregated") == 0                   # also guards the /results race fixed in app/main.py
