import os
from celery import Celery

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_DB = os.getenv("REDIS_DB", "0")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

_auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
REDIS_URL = f"redis://{_auth}{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"

celery_app = Celery(
    "ml_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    imports=["tasks"]  # Explicitly loads tasks.py
)

celery_app.conf.update(
    task_track_started=True,
    result_expires=int(os.getenv("RESULT_EXPIRES", "3600")),
)