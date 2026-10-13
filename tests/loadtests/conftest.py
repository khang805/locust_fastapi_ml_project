"""
Fixtures for the Locust test-suite.

Every test starts REAL processes: a mock InferQueue API (uvicorn) and the real `locust` CLI.
Locust is always run in a subprocess because it monkey-patches the standard library (gevent),
which must not leak into the normal pytest process.
"""
import csv
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOADTESTS = os.path.join(ROOT, "loadtests")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


class MockApi:
    def __init__(self, **env):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        full_env = {**os.environ, **{k: str(v) for k, v in env.items()}}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mock_server.fake_api:app", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=LOADTESTS, env=full_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_http(self.url + "/health") if not env.get("MOCK_ERROR_RATE") else self._wait_port()

    def _wait_port(self):
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("mock api did not start")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="session")
def mock_api():
    api = MockApi()
    yield api
    api.stop()


@pytest.fixture
def make_mock_api():
    """Factory for mock servers with injected latency / failures; cleaned up automatically."""
    created = []

    def factory(**env):
        api = MockApi(**env)
        created.append(api)
        return api

    yield factory
    for api in created:
        api.stop()


class LocustResult:
    def __init__(self, proc, prefix):
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.log = proc.stdout + proc.stderr
        self.prefix = str(prefix)

    def stats(self):
        """Rows of the *_stats.csv file as {(Type, Name): row}."""
        with open(self.prefix + "_stats.csv", newline="") as fh:
            return {(r["Type"], r["Name"]): r for r in csv.DictReader(fh)}

    def names(self):
        return {name for (_type, name) in self.stats()}

    def count(self, name, method=""):
        for (typ, nm), row in self.stats().items():
            if nm == name and (not method or typ == method):
                return int(row["Request Count"])
        return 0

    def failures(self, name):
        for (_typ, nm), row in self.stats().items():
            if nm == name:
                return int(row["Failure Count"])
        return 0

    def history(self):
        with open(self.prefix + "_stats_history.csv", newline="") as fh:
            return list(csv.DictReader(fh))


@pytest.fixture
def run_locust(tmp_path):
    """run_locust("--headless", "-u", "5", ..., env={...}) -> LocustResult (CSV files are written to tmp_path)."""

    def _run(*args, host=None, env=None, timeout=120, csv_prefix=True, files=("locustfile.py",)):
        prefix = tmp_path / "run"
        cmd = [sys.executable, "-m", "locust", "-f", ",".join(files)]
        if host:
            cmd += ["--host", host]
        if csv_prefix:
            cmd += ["--csv", str(prefix), "--csv-full-history"]
        cmd += list(args)
        proc = subprocess.run(cmd, cwd=LOADTESTS, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})
        return LocustResult(proc, prefix)

    return _run
