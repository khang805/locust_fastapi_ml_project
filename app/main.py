from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from celery.result import AsyncResult
from celery_app import celery_app
from tasks import run_sentiment_analysis
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="InferQueue API")

# Add Instrumentator to expose /metrics
Instrumentator().instrument(app).expose(app)

class TextRequest(BaseModel):
    text: str

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/predict", status_code=202)
def predict(payload: TextRequest):
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Text payload cannot be empty.")
    task = run_sentiment_analysis.delay(payload.text)
    return {"task_id": task.id, "status": "Queued"}

@app.get("/results/{task_id}")
def get_result(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    # Read the status ONCE: every `result.status` access of an unfinished task queries Redis again, so a task that
    # finishes between two reads used to be answered as {"status": "SUCCESS"} *without* a prediction.
    status = result.status
    if status == "SUCCESS":
        return {"status": "SUCCESS", "prediction": result.result}
    elif status == "FAILURE":
        return {"status": "FAILURE", "error": str(result.info)}
    return {"status": status}