"""
InferQueue load tests (Locust).

Target API (app/main.py):
    GET  /health            liveness
    POST /predict           enqueue a Celery job  -> 202 {"task_id": ...}
    GET  /results/{id}      poll the job          -> {"status": PENDING|STARTED|SUCCESS|FAILURE, ...}
    GET  /metrics           Prometheus metrics

Locust features demonstrated in this file (see README.md for the full matrix):
    HttpUser / FastHttpUser, abstract base users, @task weights, @tag, user weight, fixed_count,
    wait_time (between / constant / constant_pacing / constant_throughput / custom method),
    on_start / on_stop, catch_response, name= grouping, context=, TaskSet nesting + interrupt(),
    SequentialTaskSet, StopUser, custom metrics via events.request.fire, custom CLI args,
    events (init, test_start, test_stop, request, quitting, report_to_master, worker_report),
    web UI extension route, SLA based exit code.
Load shapes live in shapes/*.py and are combined with:  locust -f locustfile.py,shapes/stages.py
"""
import logging
import os
import random
import time
from collections import Counter

from locust import (
    FastHttpUser,
    HttpUser,
    SequentialTaskSet,
    TaskSet,
    between,
    constant,
    constant_pacing,
    constant_throughput,
    events,
    tag,
    task,
)
from locust.exception import StopUser

log = logging.getLogger("inferqueue")

DEFAULT_HOST = os.getenv("TARGET_HOST", "http://localhost:8000")

# --------------------------------------------------------------------------------------
# Custom command line arguments  (locust --poll-timeout 60 ...; also editable in the web UI)
# --------------------------------------------------------------------------------------
OPTION_DEFAULTS = {
    "poll_timeout": 30.0,
    "poll_interval": 0.5,
    "p95_limit_ms": 2000,
    "e2e_p95_limit_ms": 30000,
    "max_fail_ratio": 0.01,
    "texts_file": "",
}


@events.init_command_line_parser.add_listener
def add_custom_arguments(parser):
    parser.add_argument("--poll-timeout", type=float, default=OPTION_DEFAULTS["poll_timeout"],
                        env_var="LT_POLL_TIMEOUT", include_in_web_ui=True,
                        help="Max seconds to wait for a task to reach SUCCESS before it counts as a failure")
    parser.add_argument("--poll-interval", type=float, default=OPTION_DEFAULTS["poll_interval"],
                        env_var="LT_POLL_INTERVAL", include_in_web_ui=True,
                        help="Seconds between /results polls")
    parser.add_argument("--p95-limit-ms", type=int, default=OPTION_DEFAULTS["p95_limit_ms"],
                        env_var="LT_P95_LIMIT_MS", include_in_web_ui=False,
                        help="SLA: POST /predict p95 must stay below this (ms), else exit code 1")
    parser.add_argument("--e2e-p95-limit-ms", type=int, default=OPTION_DEFAULTS["e2e_p95_limit_ms"],
                        env_var="LT_E2E_P95_LIMIT_MS", include_in_web_ui=False,
                        help="SLA: end-to-end (submit -> SUCCESS) p95 limit in ms")
    parser.add_argument("--max-fail-ratio", type=float, default=OPTION_DEFAULTS["max_fail_ratio"],
                        env_var="LT_MAX_FAIL_RATIO", include_in_web_ui=False,
                        help="SLA: maximum tolerated overall failure ratio (0.01 = 1%%)")
    parser.add_argument("--texts-file", type=str, default=OPTION_DEFAULTS["texts_file"],
                        env_var="LT_TEXTS_FILE", include_in_web_ui=False,
                        help="Optional file with one input sentence per line (default: built-in samples)")


def opt(environment, name):
    """Read a custom option; falls back to a default when running without parsed CLI options."""
    parsed = getattr(environment, "parsed_options", None)
    return getattr(parsed, name, OPTION_DEFAULTS[name]) if parsed is not None else OPTION_DEFAULTS[name]


# --------------------------------------------------------------------------------------
# Test data
# --------------------------------------------------------------------------------------
BUILTIN_TEXTS = [
    "I absolutely love this product, it works perfectly!",
    "This was a terrible experience, I want a refund.",
    "The service was fine, nothing special.",
    "Fantastic quality and very fast delivery.",
    "The app keeps crashing and support never replies.",
]
_texts = list(BUILTIN_TEXTS)


def next_text():
    return random.choice(_texts)


# --------------------------------------------------------------------------------------
# Per-process counters, shipped from workers to the master (report_to_master / worker_report)
# --------------------------------------------------------------------------------------
class Counters:
    def __init__(self):
        self.reset()

    def reset(self):
        self.data = Counter()

    def inc(self, key, n=1):
        self.data[key] += n


LOCAL = Counters()      # what this process has produced since the last report
CLUSTER = Counter()     # master / standalone: totals across all workers


@events.report_to_master.add_listener
def on_report_to_master(client_id, data):
    data["inferqueue"] = dict(LOCAL.data)
    LOCAL.reset()


@events.worker_report.add_listener
def on_worker_report(client_id, data):
    CLUSTER.update(data.get("inferqueue", {}))


def cluster_totals():
    totals = Counter(CLUSTER)
    totals.update(LOCAL.data)  # standalone mode never receives worker reports
    return dict(totals)


# --------------------------------------------------------------------------------------
# Events: init / test_start / test_stop / request / quitting
# --------------------------------------------------------------------------------------
def prune_user_classes_without_tasks(environment):
    """
    `--tags smoke` / `--exclude-tags e2e` strip tasks from user classes, but Locust still spawns a user class whose
    tasks are ALL gone and it then crashes with "No tasks defined" (and the run exits with code 1).
    Apply the tag filter early and drop such classes so tag-based runs work out of the box.
    """
    parsed = getattr(environment, "parsed_options", None)
    if not (getattr(parsed, "tags", None) or getattr(parsed, "exclude_tags", None)):
        return
    environment._filter_tasks_by_tags()          # idempotent: Locust only filters once
    keep = [u for u in environment.user_classes if u.tasks]
    if keep and len(keep) != len(environment.user_classes):
        dropped = [u.__name__ for u in environment.user_classes if not u.tasks]
        environment.user_classes[:] = keep       # in place: the runner holds the same list
        log.info("Tag filter active - not spawning user classes without matching tasks: %s", dropped)


@events.init.add_listener
def on_init(environment, **kwargs):
    # load custom test data once per process
    path = opt(environment, "texts_file")
    if path:
        with open(path, encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
        if lines:
            _texts[:] = lines
            log.info("Loaded %d sample texts from %s", len(lines), path)

    prune_user_classes_without_tasks(environment)

    # extend the web UI with a custom JSON endpoint:  http://localhost:8089/inferqueue/summary
    if environment.web_ui:
        @environment.web_ui.app.route("/inferqueue/summary")
        def inferqueue_summary():
            from flask import jsonify
            return jsonify(cluster_totals())


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    LOCAL.reset()
    CLUSTER.clear()
    log.info("Test starting against %s (poll_timeout=%ss)", environment.host, opt(environment, "poll_timeout"))


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    log.info("Test finished. Custom counters: %s", cluster_totals())


@events.request.add_listener
def on_request(request_type, name, response_time, response_length, response, context, exception, **kwargs):
    """Called for *every* request (and for synthetic ones fired by us). Great place for custom logging."""
    if exception:
        log.warning("FAILED %s %s: %s", request_type, name, exception)
    elif response_time and response_time > 2000:
        log.warning("SLOW %s %s took %.0f ms (context=%s)", request_type, name, response_time, context)


@events.quitting.add_listener
def enforce_sla(environment, **kwargs):
    """Turn the run into a pass/fail gate for CI by setting the process exit code."""
    stats = environment.stats
    if stats.total.num_requests == 0:
        return
    if stats.total.fail_ratio > opt(environment, "max_fail_ratio"):
        log.error("SLA FAILED: failure ratio %.2f%% > %.2f%%", stats.total.fail_ratio * 100,
                  opt(environment, "max_fail_ratio") * 100)
        environment.process_exit_code = 1
    predict = stats.get("/predict", "POST")
    if predict.num_requests and predict.get_response_time_percentile(0.95) > opt(environment, "p95_limit_ms"):
        log.error("SLA FAILED: POST /predict p95 %s ms > %s ms",
                  predict.get_response_time_percentile(0.95), opt(environment, "p95_limit_ms"))
        environment.process_exit_code = 1
    e2e = stats.get("predict -> SUCCESS", "E2E")
    if e2e.num_requests and e2e.get_response_time_percentile(0.95) > opt(environment, "e2e_p95_limit_ms"):
        log.error("SLA FAILED: E2E p95 %s ms > %s ms",
                  e2e.get_response_time_percentile(0.95), opt(environment, "e2e_p95_limit_ms"))
        environment.process_exit_code = 1


# --------------------------------------------------------------------------------------
# Reusable request helpers (work for both HttpUser and FastHttpUser clients)
# --------------------------------------------------------------------------------------
def check_health(client, name="/health"):
    with client.get("/health", name=name, catch_response=True) as resp:
        if resp.status_code != 200:
            resp.failure(f"health returned {resp.status_code}")
        elif resp.json().get("status") != "ok":
            resp.failure(f"unexpected health body: {resp.text[:100]}")
        else:
            resp.success()


def submit(client, text=None):
    """POST /predict and return the task_id (or None on failure). Expects 202 + task_id."""
    text = text if text is not None else next_text()
    with client.post("/predict", json={"text": text}, name="/predict", catch_response=True,
                     context={"text_len": len(text)}) as resp:
        if resp.status_code != 202:
            resp.failure(f"expected 202, got {resp.status_code}")
            return None
        body = resp.json()
        task_id = body.get("task_id")
        if not task_id or body.get("status") != "Queued":
            resp.failure(f"bad /predict body: {resp.text[:100]}")
            return None
        resp.success()
    LOCAL.inc("submitted")
    return task_id


def poll_result(client, task_id, timeout, interval):
    """
    Poll /results/{task_id} until it is SUCCESS / FAILURE or the timeout expires.
    All polls are grouped under ONE stats row thanks to name="/results/[task_id]".
    Returns (outcome, body, elapsed_seconds).
    """
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        with client.get(f"/results/{task_id}", name="/results/[task_id]", catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"expected 200, got {resp.status_code}")
                return "HTTP_ERROR", None, time.perf_counter() - started
            body = resp.json()
            status = body.get("status")
            if status in ("SUCCESS", "FAILURE"):
                resp.success()
                return status, body, time.perf_counter() - started
            if status not in ("PENDING", "STARTED", "RETRY"):
                resp.failure(f"unknown status {status!r}")
                return "HTTP_ERROR", body, time.perf_counter() - started
            resp.success()  # "not ready yet" is a perfectly valid answer, not a failure
        time.sleep(interval)
    return "TIMEOUT", None, time.perf_counter() - started


def valid_prediction(body):
    pred = (body or {}).get("prediction") or {}
    return pred.get("label") in ("POSITIVE", "NEGATIVE") and 0.0 <= float(pred.get("score", -1)) <= 1.0


def fire_e2e(environment, outcome, body, elapsed_s):
    """Report the full submit -> result latency as a *custom metric* row in the Locust statistics."""
    exception = None
    if outcome == "TIMEOUT":
        exception = TimeoutError("task did not finish before --poll-timeout")
    elif outcome != "SUCCESS":
        exception = RuntimeError(f"task ended with {outcome}")
    elif not valid_prediction(body):
        exception = ValueError(f"invalid prediction payload: {body}")
    environment.events.request.fire(
        request_type="E2E", name="predict -> SUCCESS", response_time=elapsed_s * 1000,
        response_length=0, exception=exception, context={}, response=None,
    )
    LOCAL.inc("e2e_ok" if exception is None else "e2e_failed")


# --------------------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------------------
class InferQueueUser(HttpUser):
    """Abstract base: Locust never spawns it, subclasses inherit host + lifecycle hooks."""
    abstract = True
    host = DEFAULT_HOST

    def on_start(self):          # runs once when a simulated user starts
        self.client.headers.update({"X-Load-Test": "locust", "User-Agent": "inferqueue-locust"})
        LOCAL.inc("users_started")

    def on_stop(self):           # runs once when it is stopped (ramp-down / end of test)
        LOCAL.inc("users_stopped")


class HealthProbeUser(InferQueueUser):
    """Light background traffic like Kubernetes probes and Prometheus scrapes. constant(): fixed 2 s think time."""
    weight = 1
    wait_time = constant(2)

    @tag("health", "smoke")
    @task(5)
    def health(self):
        check_health(self.client)

    @tag("metrics")
    @task(1)
    def metrics(self):
        with self.client.get("/metrics", name="/metrics", catch_response=True) as resp:
            if resp.status_code == 200 and "http_request" in resp.text:
                resp.success()
            else:
                resp.failure(f"metrics endpoint broken: {resp.status_code}")


class SubmitUser(InferQueueUser):
    """
    Hammers POST /predict only (queue ingestion speed, ignores results).
    constant_throughput(2): each user tries to run 2 tasks per second regardless of response time.
    """
    weight = 3
    wait_time = constant_throughput(2)

    @tag("throughput", "predict")
    @task(10)
    def predict(self):
        submit(self.client)

    @tag("negative", "validation")
    @task(1)
    def empty_text_is_rejected(self):
        # 400 is the *expected* answer -> mark success only when the API really rejects it
        with self.client.post("/predict", json={"text": "   "}, name="/predict [empty]",
                              catch_response=True) as resp:
            if resp.status_code == 400:
                resp.success()
            else:
                resp.failure(f"empty text should give 400, got {resp.status_code}")

    @tag("negative", "validation")
    @task(1)
    def missing_field_is_rejected(self):
        with self.client.post("/predict", json={"wrong": "field"}, name="/predict [bad body]",
                              catch_response=True) as resp:
            if resp.status_code == 422:
                resp.success()
            else:
                resp.failure(f"bad body should give 422, got {resp.status_code}")


@tag("e2e")
class InferenceFlow(SequentialTaskSet):
    """A SequentialTaskSet runs its tasks in order: submit -> poll -> re-read."""

    wait_time = constant(0)    # no think time *inside* a flow, so E2E latency is not inflated

    def on_start(self):
        self.task_id = None
        self.outcome = None
        self.body = None
        self.t0 = None

    @task
    def submit_job(self):
        self.t0 = time.perf_counter()
        self.task_id = submit(self.client)
        if self.task_id is None:
            self.interrupt(reschedule=False)   # abort this flow, user starts a new one

    @task
    def wait_for_result(self):
        env = self.user.environment
        self.outcome, self.body, _ = poll_result(
            self.client, self.task_id, opt(env, "poll_timeout"), opt(env, "poll_interval"))
        fire_e2e(env, self.outcome, self.body, time.perf_counter() - self.t0)   # submit -> result

    @task
    def read_again(self):
        # results must stay readable (Celery keeps them for RESULT_EXPIRES seconds)
        if self.outcome == "SUCCESS":
            with self.client.get(f"/results/{self.task_id}", name="/results/[task_id] (again)",
                                 catch_response=True) as resp:
                if resp.status_code == 200 and resp.json().get("status") == "SUCCESS":
                    resp.success()
                else:
                    resp.failure("finished result disappeared")
        # end of the flow: hand control back to the user so ITS wait_time (think time) applies
        self.interrupt(reschedule=False)


class JourneyUser(InferQueueUser):
    """Realistic client: submit text, wait for the answer, think, repeat. between(): random 1-3 s think time."""
    weight = 5
    wait_time = between(1, 3)
    tasks = [InferenceFlow]


class ProbeSet(TaskSet):
    @task(3)
    def health(self):
        check_health(self.client, name="/health (taskset)")

    @task(1)
    def metrics(self):
        self.client.get("/metrics", name="/metrics (taskset)")

    @task(1)
    def leave(self):
        self.interrupt()      # go back to the parent user, which picks the next TaskSet


class BurstSet(TaskSet):
    def on_start(self):
        LOCAL.inc("bursts")

    @task
    def burst(self):
        for _ in range(3):
            submit(self.client)
        self.interrupt()


class MixedUser(InferQueueUser):
    """Nested TaskSets with relative weights {ProbeSet: 1, BurstSet: 2} and a custom wait_time method."""
    weight = 1
    tasks = {ProbeSet: 1, BurstSet: 2}

    def wait_time(self):
        # exponential think time (Poisson-like arrivals), capped at 5 s
        return min(random.expovariate(1 / 1.5), 5)


class FastSubmitUser(FastHttpUser):
    """
    FastHttpUser (geventhttpclient) can push several times more requests per CPU core than HttpUser (requests).
    constant_pacing(1): one task *start* every second per user, whatever the response time is.
    """
    weight = 1
    host = DEFAULT_HOST
    wait_time = constant_pacing(1)
    connection_timeout = 5.0
    network_timeout = 10.0
    concurrency = 10

    @tag("fast", "throughput")
    @task(4)
    def predict(self):
        submit(self.client)

    @tag("fast", "health")
    @task(1)
    def health(self):
        check_health(self.client, name="/health (fast)")


_smoke_done = False     # fixed_count users are re-spawned by Locust after StopUser while ramping up -> run only once


class SmokeUser(InferQueueUser):
    """
    fixed_count=1 -> exactly ONE instance is always spawned, outside the weight distribution.
    Runs a one-off functional check of the whole API and then ends itself with StopUser.
    Run only this:  locust --tags smoke
    """
    fixed_count = 1
    wait_time = constant(0)

    @tag("smoke")
    @task
    def full_api_smoke_test(self):
        global _smoke_done
        if _smoke_done:
            raise StopUser()
        _smoke_done = True
        env = self.environment
        check_health(self.client, name="/health [smoke]")

        with self.client.get("/openapi.json", name="/openapi.json [smoke]", catch_response=True) as resp:
            paths = resp.json().get("paths", {}) if resp.status_code == 200 else {}
            if {"/predict", "/health", "/results/{task_id}"} <= set(paths):
                resp.success()
            else:
                resp.failure("OpenAPI schema is missing expected routes")

        with self.client.get("/metrics", name="/metrics [smoke]", catch_response=True) as resp:
            if resp.status_code == 200 and "http_request" in resp.text:
                resp.success()
            else:
                resp.failure("prometheus metrics missing")

        with self.client.post("/predict", json={"text": ""}, name="/predict [smoke empty]",
                              catch_response=True) as resp:
            resp.success() if resp.status_code == 400 else resp.failure(f"expected 400, got {resp.status_code}")

        with self.client.post("/predict", json={}, name="/predict [smoke bad body]",
                              catch_response=True) as resp:
            resp.success() if resp.status_code == 422 else resp.failure(f"expected 422, got {resp.status_code}")

        with self.client.get("/results/does-not-exist", name="/results/[task_id] [smoke unknown]",
                             catch_response=True) as resp:
            if resp.status_code == 200 and resp.json().get("status") == "PENDING":
                resp.success()
            else:
                resp.failure("unknown task ids should be PENDING")

        t0 = time.perf_counter()
        task_id = submit(self.client, "I really love this, it is wonderful!")
        if task_id:
            outcome, body, _ = poll_result(self.client, task_id, opt(env, "poll_timeout"),
                                           opt(env, "poll_interval"))
            fire_e2e(env, outcome, body, time.perf_counter() - t0)

        raise StopUser()      # this simulated user is done
