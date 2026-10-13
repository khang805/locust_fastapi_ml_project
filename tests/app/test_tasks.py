import tasks


def test_task_lazy_loads_model_once_and_returns_first_result(monkeypatch):
    created = []

    def fake_pipeline(kind, model):
        created.append((kind, model))
        return lambda text: [{"label": "NEGATIVE", "score": 0.91}, {"label": "POSITIVE", "score": 0.09}]

    monkeypatch.setattr(tasks, "pipeline", fake_pipeline)
    monkeypatch.setattr(tasks, "classifier", None)

    first = tasks.run_sentiment_analysis("bad")
    second = tasks.run_sentiment_analysis("worse")

    assert first == {"label": "NEGATIVE", "score": 0.91}
    assert second == first
    assert len(created) == 1                       # model loaded only once per worker
    assert created[0][0] == "sentiment-analysis"


def test_task_is_registered_under_stable_name():
    assert tasks.run_sentiment_analysis.name == "tasks.run_sentiment_analysis"
