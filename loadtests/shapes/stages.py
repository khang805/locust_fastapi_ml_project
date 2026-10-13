"""
Stages load shape: warm-up -> ramp -> steady -> peak -> ramp-down.
Each stage may also restrict which user classes run (3-tuple return of tick()).

Run:  locust -f locustfile.py,shapes/stages.py --headless --host http://localhost:8000
Speed up for demos / tests:  SHAPE_TIME_SCALE=0.1  (every duration is multiplied by it)
"""
import os

from locust import LoadTestShape

SCALE = float(os.getenv("SHAPE_TIME_SCALE", "1"))

# duration = END time (seconds since start) of the stage
STAGES = [
    {"duration": 30, "users": 5, "spawn_rate": 1},                                      # warm-up
    {"duration": 90, "users": 20, "spawn_rate": 2},                                     # ramp
    {"duration": 150, "users": 20, "spawn_rate": 2, "user_classes": ["JourneyUser"]},   # realistic users only
    {"duration": 180, "users": 50, "spawn_rate": 5},                                    # peak
    {"duration": 210, "users": 5, "spawn_rate": 5},                                     # ramp-down
]


class StagesShape(LoadTestShape):
    def tick(self):
        run_time = self.get_run_time()
        for stage in STAGES:
            if run_time < stage["duration"] * SCALE:
                classes = self.resolve(stage.get("user_classes"))
                if classes:
                    return stage["users"], stage["spawn_rate"], classes
                return stage["users"], stage["spawn_rate"]
        return None  # returning None stops the test

    def resolve(self, names):
        if not names:
            return None
        return [c for c in self.runner.user_classes if c.__name__ in names] if self.runner else None
