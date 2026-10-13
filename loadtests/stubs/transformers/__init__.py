"""
Minimal stand-in for Hugging Face `transformers`, used ONLY to run the REAL FastAPI + Celery + Redis stack
without torch and without downloading the model (CI, laptops, tests/loadtests/test_real_stack.py).

    PYTHONPATH=loadtests/stubs  celery -A celery_app.celery_app worker ...

It mimics `pipeline("sentiment-analysis", model=...)` -> callable(text) -> [{"label", "score"}]
with a small random delay so the queue behaves like a (very fast) model.
"""
import random
import time

_NEGATIVE = ("terrible", "awful", "worst", "disappointed", "crash", "refund", "broke", "bad")


def pipeline(task=None, model=None, **kwargs):
    def classify(text):
        time.sleep(random.uniform(0.02, 0.08))
        label = "NEGATIVE" if any(w in str(text).lower() for w in _NEGATIVE) else "POSITIVE"
        return [{"label": label, "score": round(random.uniform(0.9, 0.999), 4)}]

    return classify
