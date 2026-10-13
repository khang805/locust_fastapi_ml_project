# Shortcuts for testing and load-testing InferQueue.  `make help` lists everything.
PY        ?= python
HOST      ?= http://localhost:8000
MOCK_PORT ?= 8001
LT        = cd loadtests &&

help:  ## show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'

install:  ## install app + load-test + dev dependencies
	$(PY) -m pip install -r requirements-dev.txt

# ---------------------------------------------------------------- tests
test:  ## fast unit tests (API, Celery task/config, mock server) - ~2 s
	$(PY) -m pytest -m "not e2e" -q

test-loadtests:  ## full Locust test-suite: real Locust processes vs mock API (+ real stack if redis-server exists) - ~4 min
	$(PY) -m pytest tests/loadtests -m e2e -q

test-all:  ## everything
	$(PY) -m pytest -q

# ---------------------------------------------------------------- target to test against
mock-api:  ## start the offline mock API on port 8001 (no Redis / model needed)
	$(LT) $(PY) -m uvicorn mock_server.fake_api:app --port $(MOCK_PORT)

mock-api-flaky:  ## mock API that injects 30% HTTP 500s (proves failures are detected)
	$(LT) MOCK_ERROR_RATE=0.3 $(PY) -m uvicorn mock_server.fake_api:app --port $(MOCK_PORT)

# ---------------------------------------------------------------- run Locust (HOST=http://localhost:8000 by default)
locust-ui:  ## web UI on http://localhost:8089 (start/stop, charts, live custom args)
	$(LT) locust --host $(HOST)

locust-smoke:  ## one-off functional check of the whole API (tag: smoke)
	$(LT) locust --config conf/smoke.conf --host $(HOST)

locust-ci:  ## headless pass/fail run with SLA thresholds -> reports/ci.html + CSV
	$(LT) locust --config conf/ci.conf --host $(HOST)

locust-e2e:  ## only submit->poll->result journeys
	$(LT) locust --headless --tags e2e -u 20 -r 5 -t 1m --host $(HOST) --html reports/e2e.html

locust-throughput:  ## only POST /predict ingestion speed (SubmitUser + FastHttpUser)
	$(LT) locust --headless --tags throughput -u 50 -r 10 -t 1m --host $(HOST) --html reports/throughput.html

locust-stages:  ## staged ramp shape (shapes/stages.py)
	$(LT) locust -f locustfile.py,shapes/stages.py --headless --host $(HOST) --html reports/stages.html

locust-spike:  ## sudden spike shape - watch the HPAs / queue drain (shapes/spike.py)
	$(LT) locust -f locustfile.py,shapes/spike.py --headless --host $(HOST) --html reports/spike.html

locust-wave:  ## two smooth traffic waves (shapes/double_wave.py)
	$(LT) locust -f locustfile.py,shapes/double_wave.py --headless --host $(HOST) --html reports/wave.html

locust-distributed:  ## 1 master + 4 local workers, headless
	$(LT) (for i in 1 2 3 4; do locust --worker & done; \
	  locust --master --headless --expect-workers 4 -u 100 -r 20 -t 1m --host $(HOST) --html reports/distributed.html; wait)

# ---------------------------------------------------------------- docker
up-real:  ## real stack (redis + api + celery worker)
	docker compose --profile real up --build

up-loadtest:  ## real stack + distributed Locust (3 workers) -> http://localhost:8089
	docker compose --profile real --profile loadtest up --build --scale locust-worker=3

up-mock-loadtest:  ## mock API + distributed Locust (3 workers), no model download
	TARGET_HOST=http://mock-api:8000 docker compose --profile mock --profile loadtest up --build --scale locust-worker=3

down:  ## stop and remove containers
	docker compose --profile real --profile mock --profile loadtest down -v

.PHONY: help install test test-loadtests test-all mock-api mock-api-flaky locust-ui locust-smoke locust-ci locust-e2e \
        locust-throughput locust-stages locust-spike locust-wave locust-distributed up-real up-loadtest up-mock-loadtest down

# ---------------------------------------------------------------- kubernetes: HPA autoscaling demo (k8s-local overlay)
IMG ?= inferqueue:latest
K8S_NS = inferqueue

k8s-build:  ## build the app image used by api + worker (then load it into minikube/kind yourself, see README)
	docker build -t $(IMG) ./app

k8s-up:  ## deploy redis + api + worker + both HPAs (local overlay) and wait for them
	kubectl apply -k k8s-local
	kubectl -n $(K8S_NS) rollout status statefulset/redis --timeout=180s
	kubectl -n $(K8S_NS) wait --for=condition=complete job/model-download --timeout=900s
	kubectl -n $(K8S_NS) rollout status deploy/api deploy/worker --timeout=300s

k8s-locust:  ## deploy Locust master+workers inside the cluster (target http://api)
	kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/loadtest | kubectl apply -f -
	kubectl -n $(K8S_NS) rollout status deploy/locust-master deploy/locust-worker --timeout=180s

k8s-ports:  ## Locust UI -> http://localhost:8089 and API -> http://localhost:8000 (Ctrl+C stops both)
	(kubectl -n $(K8S_NS) port-forward svc/api 8000:80 &) ; kubectl -n $(K8S_NS) port-forward svc/locust-master 8089:8089

k8s-watch:  ## live HPA view (run in its own terminal)
	kubectl -n $(K8S_NS) get hpa -w

k8s-top:  ## pods + CPU every 5 s, plus Celery queue length in Redis
	while true; do clear; kubectl -n $(K8S_NS) get pods -l 'app in (api,worker)' ; echo; kubectl -n $(K8S_NS) top pods -l 'app in (api,worker)' ; echo; \
	  echo -n "celery queue length: "; kubectl -n $(K8S_NS) exec redis-0 -- sh -c 'redis-cli -a "$$REDIS_PASSWORD" --no-auth-warning llen celery'; sleep 5; done

k8s-down:  ## remove everything (Locust + app + PVCs)
	-kubectl kustomize --load-restrictor LoadRestrictionsNone k8s/loadtest | kubectl delete -f -
	-kubectl delete -k k8s-local
	-kubectl -n $(K8S_NS) delete pvc --all

.PHONY: k8s-build k8s-up k8s-locust k8s-ports k8s-watch k8s-top k8s-down

k8s-argo-off:  ## stop ArgoCD from resetting replicas/HPA values to the git version (it has selfHeal: true)
	-kubectl -n argocd patch application inferqueue-app --type merge -p '{"spec":{"syncPolicy":null}}'

.PHONY: k8s-argo-off
