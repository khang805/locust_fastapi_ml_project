import importlib


def _reload(monkeypatch, **env):
    for key in ("REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD", "RESULT_EXPIRES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import celery_app
    return importlib.reload(celery_app)


def test_default_url(monkeypatch):
    mod = _reload(monkeypatch)
    assert mod.REDIS_URL == "redis://redis:6379/0"


def test_url_with_password_and_custom_host(monkeypatch):
    mod = _reload(monkeypatch, REDIS_HOST="cache", REDIS_PASSWORD="s3cret", REDIS_DB="2")
    assert mod.REDIS_URL == "redis://:s3cret@cache:6379/2"
    assert mod.celery_app.conf.broker_url == mod.REDIS_URL


def test_result_expiry_is_configurable(monkeypatch):
    mod = _reload(monkeypatch, RESULT_EXPIRES="120")
    assert mod.celery_app.conf.result_expires == 120
    assert mod.celery_app.conf.task_track_started is True
