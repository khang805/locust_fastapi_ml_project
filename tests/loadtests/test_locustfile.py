"""
Checks that the Locust setup itself is correct and RUNNING correctly:
structure, tags, user selection, shapes, SLA gate, config files, web UI, distributed mode.
All tests are marked `e2e`: they spawn real Locust processes against a mock API.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

from .conftest import LOADTESTS, free_port

pytestmark = pytest.mark.e2e

FAST = ("--poll-interval", "0.1")   # poll quickly so mock tasks (50-250 ms) finish fast


# ----------------------------------------------------------------------------- structure
def test_all_user_classes_are_discovered(run_locust):
    r = run_locust("--list", csv_prefix=False)
    assert r.returncode == 0, r.log
    for name in ("HealthProbeUser", "SubmitUser", "JourneyUser", "MixedUser", "FastSubmitUser", "SmokeUser"):
        assert name in r.stdout
    assert "InferQueueUser" not in r.stdout          # abstract base class is never spawned


def test_task_ratio_json_has_weights_and_nested_tasksets(run_locust):
    r = run_locust("--show-task-ratio-json", csv_prefix=False)
    assert r.returncode == 0, r.log
    data = json.loads(r.stdout[r.stdout.index("{"):])
    per_user = data["per_class"]
    assert set(per_user) >= {"SubmitUser", "JourneyUser", "MixedUser"}
    assert set(per_user["JourneyUser"]["tasks"]["InferenceFlow"]["tasks"]) == {
        "submit_job", "wait_for_result", "read_again"}
    assert set(per_user["MixedUser"]["tasks"]) == {"ProbeSet", "BurstSet"}
    submit_tasks = per_user["SubmitUser"]["tasks"]
    assert submit_tasks["predict"]["ratio"] > submit_tasks["empty_text_is_rejected"]["ratio"]   # @task(10) vs @task(1)


# ----------------------------------------------------------------------------- normal run
def test_headless_run_is_green_and_produces_reports(run_locust, mock_api, tmp_path):
    html = tmp_path / "report.html"
    r = run_locust("--headless", "-u", "12", "-r", "12", "-t", "12s", *FAST, "--html", str(html),
                   host=mock_api.url)
    assert r.returncode == 0, r.log[-3000:]

    # every endpoint is exercised ...
    for name in ("/health", "/predict", "/results/[task_id]", "/metrics", "/predict [empty]", "/predict [bad body]"):
        assert r.count(name) > 0, f"{name} was never requested\n{sorted(r.names())}"
    # ... and nothing failed
    assert r.failures("Aggregated") == 0
    assert r.count("/predict") > 20

    # custom metric fired through events.request.fire
    assert r.count("predict -> SUCCESS", "E2E") > 0
    assert r.failures("predict -> SUCCESS") == 0

    # polls of many different task ids are grouped into ONE row (name= grouping)
    assert not any(n.startswith("/results/") and "[" not in n for n in r.names())

    # reports
    assert html.exists() and html.stat().st_size > 10_000
    assert os.path.exists(r.prefix + "_failures.csv")
    assert len(r.history()) > 3                      # --csv-full-history time series

    # events: init/test_start/test_stop listeners ran, users were started
    assert "Custom counters" in r.log
    assert "'users_started'" in r.log and "'submitted'" in r.log


def test_nested_tasksets_and_interrupt_run(run_locust, mock_api):
    r = run_locust("--headless", "-u", "10", "-r", "10", "-t", "10s", *FAST, host=mock_api.url)
    assert r.returncode == 0, r.log[-2000:]
    assert r.count("/health (taskset)") > 0 and r.count("/metrics (taskset)") >= 0
    assert "'bursts'" in r.log                        # BurstSet.on_start ran


# ----------------------------------------------------------------------------- selecting what runs
def test_tags_smoke_runs_only_smoke_tasks_and_stopuser_ends_it(run_locust, mock_api):
    r = run_locust("--headless", "-u", "5", "-r", "5", "-t", "8s", *FAST, "--tags", "smoke", host=mock_api.url)
    assert r.returncode == 0, r.log[-2000:]
    assert r.count("/openapi.json [smoke]") == 1      # fixed_count=1 and StopUser -> ran exactly once
    assert r.count("/health [smoke]") == 1
    assert r.count("/predict [smoke empty]") == 1
    assert r.count("/predict [smoke bad body]") == 1
    assert r.count("/results/[task_id] [smoke unknown]") == 1
    assert r.count("predict -> SUCCESS", "E2E") == 1  # full flow in the smoke test succeeded
    assert r.count("/predict [empty]") == 0           # SubmitUser tasks are tagged "negative", not "smoke"
    assert r.count("/results/[task_id] (again)") == 0
    assert r.failures("Aggregated") == 0


def test_exclude_tags(run_locust, mock_api):
    r = run_locust("--headless", "-u", "10", "-r", "10", "-t", "8s", *FAST, "--exclude-tags", "e2e", "negative",
                   host=mock_api.url)
    assert r.returncode == 0, r.log[-2000:]
    assert r.count("/predict [empty]") == 0 and r.count("/predict [bad body]") == 0
    assert r.count("/results/[task_id] (again)") == 0
    assert r.count("/predict") > 0


def test_user_class_selection_and_fasthttpuser(run_locust, mock_api):
    r = run_locust("--headless", "-u", "5", "-r", "5", "-t", "8s", "FastSubmitUser", host=mock_api.url)
    assert r.returncode == 0, r.log[-2000:]
    assert r.count("/health (fast)") > 0 and r.count("/predict") > 0
    assert r.count("/metrics") == 0 and r.count("/predict [empty]") == 0     # other users did not run
    assert r.failures("Aggregated") == 0


# ----------------------------------------------------------------------------- failure detection
def test_sla_gate_fails_when_api_returns_errors(run_locust, make_mock_api):
    bad = make_mock_api(MOCK_ERROR_RATE=0.6)
    r = run_locust("--headless", "-u", "8", "-r", "8", "-t", "8s", *FAST, "--exit-code-on-error", "0",
                   "--max-fail-ratio", "0.05", host=bad.url)
    assert r.failures("Aggregated") > 0
    assert "SLA FAILED: failure ratio" in r.log
    assert r.returncode == 1                          # exit code set by our quitting listener, not by Locust


def test_default_exit_code_on_error(run_locust, make_mock_api):
    bad = make_mock_api(MOCK_ERROR_RATE=1.0)
    r = run_locust("--headless", "-u", "3", "-r", "3", "-t", "5s", *FAST, host=bad.url)
    assert r.returncode == 1


def test_custom_args_poll_timeout_makes_e2e_fail(run_locust, make_mock_api):
    slow = make_mock_api(MOCK_LATENCY_MIN=1.5, MOCK_LATENCY_MAX=1.8)
    r = run_locust("--headless", "-u", "4", "-r", "4", "-t", "10s", "--poll-timeout", "0.3", "--poll-interval", "0.1",
                   "--exit-code-on-error", "0", "--max-fail-ratio", "0.0", host=slow.url)
    assert r.failures("predict -> SUCCESS") > 0       # --poll-timeout is honoured
    assert r.returncode == 1


def test_task_failures_are_reported_as_e2e_failures(run_locust, make_mock_api):
    flaky = make_mock_api(MOCK_TASK_FAILURE_RATE=1.0)
    r = run_locust("--headless", "-u", "4", "-r", "4", "-t", "8s", *FAST, "--exit-code-on-error", "0",
                   host=flaky.url)
    assert r.failures("predict -> SUCCESS") > 0
    assert "task ended with FAILURE" in r.log


def test_p95_sla_limit(run_locust, mock_api):
    r = run_locust("--headless", "-u", "5", "-r", "5", "-t", "6s", *FAST, "--exit-code-on-error", "0",
                   "--p95-limit-ms", "0", host=mock_api.url)          # impossible limit -> must fail
    assert "SLA FAILED: POST /predict p95" in r.log
    assert r.returncode == 1


def test_custom_texts_file(run_locust, mock_api, tmp_path):
    texts = tmp_path / "t.txt"
    texts.write_text("only this sentence\n")
    r = run_locust("--headless", "-u", "3", "-r", "3", "-t", "5s", *FAST, "--texts-file", str(texts), host=mock_api.url)
    assert r.returncode == 0, r.log[-2000:]
    assert "Loaded 1 sample texts" in r.log


# ----------------------------------------------------------------------------- config files
def test_ci_config_file(run_locust, mock_api, tmp_path):
    r = run_locust("--config", "conf/ci.conf", "--run-time", "10s", "-u", "8", "-r", "8", *FAST,
                   "--html", str(tmp_path / "ci.html"), host=mock_api.url, csv_prefix=True)
    assert r.returncode == 0, r.log[-2000:]
    assert (tmp_path / "ci.html").exists()
    rows = json.loads(r.stdout)                       # `json = true` in ci.conf prints the stats as JSON
    predict = next(row for row in rows if row["name"] == "/predict" and row["method"] == "POST")
    assert predict["num_requests"] > 0 and predict["num_failures"] == 0


def test_smoke_config_file(run_locust, mock_api, tmp_path):
    r = run_locust("--config", "conf/smoke.conf", "--html", str(tmp_path / "s.html"), "--run-time", "6s",
                   *FAST, host=mock_api.url)
    assert r.returncode == 0, r.log[-2000:]
    assert r.count("/openapi.json [smoke]") == 1


# ----------------------------------------------------------------------------- load shapes
@pytest.mark.parametrize("shape_file,scale,min_peak", [
    ("shapes/stages.py", "0.04", 10),
    ("shapes/spike.py", "0.06", 45),
    ("shapes/double_wave.py", "0.04", 20),
])
def test_load_shapes_drive_user_count_and_stop_by_themselves(run_locust, mock_api, shape_file, scale, min_peak):
    started = time.time()
    r = run_locust("--headless", *FAST, host=mock_api.url, env={"SHAPE_TIME_SCALE": scale},
                   files=("locustfile.py", shape_file), timeout=120)
    assert r.returncode == 0, r.log[-2500:]
    assert time.time() - started < 90                 # the shape (not -t) ended the test
    users = [int(float(row["User Count"])) for row in r.history() if row["User Count"]]
    assert max(users) >= min_peak, f"peak users {max(users)} < {min_peak}"
    assert len(set(users)) > 2                        # the user count really changed over time (not a flat line)
    assert r.failures("Aggregated") == 0


# ----------------------------------------------------------------------------- web UI
def test_web_ui_and_custom_route(mock_api):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "locust", "-f", "locustfile.py", "--host", mock_api.url, "--web-port", str(port),
         "--web-host", "127.0.0.1", "--autostart", "-u", "5", "-r", "5", "-t", "12s", "--autoquit", "1",
         "--poll-interval", "0.1"],
        cwd=LOADTESTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(base + "/", timeout=1).read()
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise AssertionError("web UI did not start")
        time.sleep(5)
        stats = json.loads(urllib.request.urlopen(base + "/stats/requests", timeout=5).read())
        assert stats["total_rps"] > 0 and stats["state"] in ("running", "spawning", "stopped")
        assert any(row["name"] == "/predict" for row in stats["stats"])
        summary = json.loads(urllib.request.urlopen(base + "/inferqueue/summary", timeout=5).read())   # our route
        assert summary.get("submitted", 0) > 0
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "poll_timeout" in html or "poll-timeout" in html          # custom arg is editable in the UI
        assert proc.wait(timeout=60) == 0                                # --autoquit ended the process
    finally:
        if proc.poll() is None:
            proc.kill()


# ----------------------------------------------------------------------------- distributed
def test_distributed_master_and_workers(mock_api, tmp_path):
    port = free_port()
    prefix = tmp_path / "dist"
    common = [sys.executable, "-m", "locust", "-f", "locustfile.py"]
    master = subprocess.Popen(
        common + ["--master", "--headless", "--expect-workers", "2", "--master-bind-host", "127.0.0.1",
                  "--master-bind-port", str(port), "--host", mock_api.url, "-u", "12", "-r", "12", "-t", "12s",
                  "--poll-interval", "0.1", "--csv", str(prefix)],
        cwd=LOADTESTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    workers = [subprocess.Popen(common + ["--worker", "--master-host", "127.0.0.1", "--master-port", str(port)],
                                cwd=LOADTESTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
               for _ in range(2)]
    try:
        out, _ = master.communicate(timeout=120)
        assert master.returncode == 0, out[-3000:]
        assert "2 workers" in out or "worker" in out.lower()
        import csv
        with open(str(prefix) + "_stats.csv", newline="") as fh:
            rows = {r["Name"]: r for r in csv.DictReader(fh)}
        assert int(rows["/predict"]["Request Count"]) > 20
        assert int(rows["Aggregated"]["Failure Count"]) == 0
        # custom data travelled worker -> master through report_to_master / worker_report
        assert "'submitted'" in out
        for w in workers:
            w.wait(timeout=30)
    finally:
        for p in [master, *workers]:
            if p.poll() is None:
                p.kill()
