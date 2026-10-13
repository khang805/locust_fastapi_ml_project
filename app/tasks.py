import os
from celery_app import celery_app
from transformers import pipeline

MODEL_NAME = os.getenv("MODEL_NAME", "distilbert-base-uncased-finetuned-sst-2-english")
classifier = None

@celery_app.task(name="tasks.run_sentiment_analysis")
def run_sentiment_analysis(text: str):
    global classifier
    if classifier is None:
        classifier = pipeline("sentiment-analysis", model=MODEL_NAME)
    results = classifier(text)
    return results[0]