"""
Spike load shape: calm -> sudden 10x burst -> recovery. Ideal for watching the Kubernetes HPAs
(k8s/api-hpa.yaml, k8s/worker-hpa.yaml) react and for checking the Celery queue drains afterwards.

Run:  locust -f locustfile.py,shapes/spike.py --headless --host http://localhost:8000
"""
import os

from locust import LoadTestShape

SCALE = float(os.getenv("SHAPE_TIME_SCALE", "1"))


class SpikeShape(LoadTestShape):
    base_users = 5
    spike_users = 50
    warmup = 60 * SCALE
    spike = 60 * SCALE
    recovery = 90 * SCALE

    def tick(self):
        t = self.get_run_time()
        if t < self.warmup:
            return self.base_users, self.base_users
        if t < self.warmup + self.spike:
            return self.spike_users, self.spike_users          # spawn instantly
        if t < self.warmup + self.spike + self.recovery:
            return self.base_users, self.spike_users
        return None
