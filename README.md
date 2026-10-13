# fastapi_ml_locust

A sentiment-analysis API built with FastAPI, Celery and Redis. A DistilBERT model runs in background workers,
and on Kubernetes both the API and the workers scale automatically with CPU load (HPA).

```
client ──POST /predict──▶ FastAPI ──▶ Redis queue ──▶ Celery worker (DistilBERT)
   ▲                                      │
   └─────────GET /results/{task_id}◀──────┘
```

`POST /predict` returns `202` immediately with a `task_id`. The client then polls `GET /results/{task_id}`
until the status goes from `PENDING` → `STARTED` → `SUCCESS` (or `FAILURE`).

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/predict` | Body `{"text": "..."}`. Queues a job and returns `{"task_id": "..."}` (202) |
| GET | `/results/{task_id}` | Returns the job status and, when done, the prediction |
| GET | `/metrics` | Prometheus metrics |

```bash
curl -s -X POST localhost:8000/predict -H 'Content-Type: application/json' -d '{"text":"I love this"}'
curl -s localhost:8000/results/<task_id>
```

## Requirements

- Docker
- For autoscaling: minikube (or kind / Docker Desktop Kubernetes) and `kubectl`
- About 4 CPUs and 8–10 GB of free RAM for the Kubernetes setup. Each worker requests 1 Gi of memory.

## 1. Run with Docker Compose (functional check, no autoscaling)

```bash
docker compose --profile real up --build
```

- API: http://localhost:8000
- One API container, one worker (`-c 1`) and Redis
- The first prediction is slow, because the worker downloads and loads the model

Compose cannot autoscale. To see pods scale up and down, use Kubernetes (section 2).

Stop with `docker compose --profile real down`.

## 2. Run on Kubernetes with autoscaling (HPA)

The base manifests are in `k8s/`. The `k8s-local/` overlay adapts them to a local cluster:
it uses one image (`inferqueue:latest`) for the API and worker, drops the ServiceMonitor,
creates the `inferqueue-sa` ServiceAccount, sets HPA limits to **min 1 / max 8** for both,
and shortens the scale-down window to 60 seconds.

### Autoscaling settings (local overlay)

| | Metric | Target | Min | Max |
|---|---|---|---|---|
| `api-hpa` | CPU | 70% of request | 1 | 8 |
| `worker-hpa` | CPU | 75% of request | 1 | 8 |

Scale-up is immediate (API: 2 pods per 15 s, worker: 1 pod per 30 s). Scale-down waits 60 s and then removes
1 pod every 30 s. The settings live in `k8s-local/kustomization.yaml`.

### Setup

```bash
minikube start --cpus=4 --memory=10000
minikube addons enable metrics-server           # required, the HPA needs CPU metrics

make k8s-build                                   # docker build -t inferqueue:latest ./app
minikube image load inferqueue:latest redis:7-alpine busybox:1.36

make k8s-up                                      # redis, model download job, api, worker, both HPAs
kubectl -n inferqueue get hpa                    # TARGETS should show real percentages
```

For kind use `kind load docker-image inferqueue:latest`. On kind or Docker Desktop, metrics-server needs
the `--kubelet-insecure-tls` argument.

The first `make k8s-up` downloads the model, which can take several minutes.

If ArgoCD is installed and watches `k8s/` (`k8s/argocd-app.yaml` has `selfHeal: true`), it resets replica counts
and HPA limits back to the git version. Run `make k8s-argo-off` before testing.

### Try the API

```bash
kubectl -n inferqueue port-forward svc/api 8000:80
```

## 3. Load test and watch autoscaling

Locust runs inside the cluster and calls the API at `http://api`.

```bash
make k8s-locust                # Locust master (web UI) + 3 workers
make k8s-ports                 # Locust UI: http://localhost:8089, API: http://localhost:8000
```

In two more terminals:

```bash
make k8s-watch                 # live view of both HPAs
make k8s-top                   # pods, CPU and Celery queue length
```

To make the load follow the user count (each user waits for its result before sending the next request),
run only the `JourneyUser` class:

```bash
kubectl -n inferqueue patch deploy locust-master --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"JourneyUser"}]'
```

In the Locust UI (http://localhost:8089):

1. **Ramp up:** 20 users, spawn rate 2, for 2–3 minutes. Pod counts start to rise.
2. **Peak:** use **Edit** in the top bar to raise to 100 users. Pods climb towards 8.
3. **Ramp down:** use **Edit** to go to 20, then 5, or press **Stop**. About a minute after CPU falls, pods
   step down one at a time.

### What to expect

- Workers are single-task processes and a busy worker uses its full CPU limit (about 400% of its request).
  A few busy workers therefore keep the average high, and 8 workers is reached quickly.
- Workers only scale down once the Celery queue is empty. If a large backlog built up, they stay busy
  until it drains. To clear it:
```bash
  kubectl -n inferqueue exec redis-0 -- sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning del celery'
```
- Going from 8 pods back to 1 takes about 4–5 minutes because of the one-pod-per-30-seconds step.
- The first request on a new worker is slow while it loads the model.
- The model cache volume is `ReadWriteOnce`, so this setup is for a single-node cluster.

### Load test with Docker Compose (no autoscaling)

```bash
docker compose --profile real --profile loadtest up --build --scale locust-worker=3
```

Locust UI: http://localhost:8089

## 4. Clean up

```bash
make k8s-down            # remove Locust, the app and its volumes
minikube delete          # remove the whole local cluster
docker compose --profile real --profile loadtest down
```

## Project layout

```
app/                     FastAPI app (main.py), Celery app (celery_app.py), task (tasks.py), Dockerfile
k8s/                     Base Kubernetes manifests (redis, api, worker, HPAs, model download job, cronjob)
k8s-local/               Local overlay: single image, min 1 / max 8 HPAs, ServiceAccount, faster scale-down
k8s/loadtest/            Locust master and workers for the cluster
loadtests/               locustfile.py and load shapes
docker-compose.yml       Profiles: real, loadtest
Makefile                 k8s-build, k8s-up, k8s-locust, k8s-ports, k8s-watch, k8s-top, k8s-down, k8s-argo-off
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| HPA shows `<unknown>` | metrics-server not running or still starting: `kubectl -n kube-system get pods \| grep metrics-server` |
| `api` / `worker` pods never appear, `serviceaccount "inferqueue-sa" not found` | Use `kubectl apply -k k8s-local` (it creates the ServiceAccount) |
| `ErrImageNeverPull` / `ImagePullBackOff` | Image is not in the cluster: `minikube image load inferqueue:latest` |
| Pod counts jump back to 3 | ArgoCD self-heal is resetting them: `make k8s-argo-off` |
| Pods `Pending` with `Insufficient memory` | Give minikube more memory or lower `maxReplicas` in `k8s-local/kustomization.yaml` |
| Workers don't scale down | Queue still has a backlog, or the 60 s window and 30 s steps haven't passed yet |
