"""
Double-wave load shape: two smooth traffic peaks (e.g. morning + evening) computed with a cosine.

Run:  locust -f locustfile.py,shapes/double_wave.py --headless --host http://localhost:8000
"""
import math
import os

from locust import LoadTestShape

SCALE = float(os.getenv("SHAPE_TIME_SCALE", "1"))


class DoubleWaveShape(LoadTestShape):
    min_users = 5
    peak_users = 40
    time_limit = 240 * SCALE   # two waves of 120 s each (scaled)

    def tick(self):
        t = self.get_run_time()
        if t > self.time_limit:
            return None
        wave = (1 - math.cos(2 * math.pi * t / (self.time_limit / 2))) / 2     # 0..1, two periods
        users = round(self.min_users + wave * (self.peak_users - self.min_users))
        return users, 5
