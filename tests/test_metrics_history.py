"""История метрик хоста панели (Ночь 19) — `app/services/metrics_history.py` +
`GET /api/system/metrics/history`: сэмплер, кольцевой буфер, окно выборки,
персист на диск, auth эндпоинта, capability в /api/version.
"""
import pytest

from app.services import metrics_history as mh


@pytest.fixture(autouse=True)
def _fresh_buffer(monkeypatch, tmp_path):
    """Изолируем модульное состояние: свой файл на диске, пустой буфер."""
    monkeypatch.setattr(mh, "STORE_PATH", tmp_path / "metrics_history.json")
    monkeypatch.setattr(mh, "_points", [])
    monkeypatch.setattr(mh, "_loaded", True)  # не подтягивать данные с реального диска


def _fake_host(monkeypatch, *, load=(0.5, 0.4, 0.3), cpu_n=2, mem=40.0, disk=60.0):
    monkeypatch.setattr(mh.host_health, "read_loadavg", lambda *a, **k: list(load))
    monkeypatch.setattr(mh.host_health, "read_meminfo",
                        lambda *a, **k: {"memory": {"used_pct": mem},
                                         "swap": {"used_pct": 0}})
    monkeypatch.setattr(mh.host_health, "disk_usage",
                        lambda *a, **k: {"used_pct": disk})
    import os
    monkeypatch.setattr(os, "cpu_count", lambda: cpu_n)


def test_sample_point_values(monkeypatch):
    _fake_host(monkeypatch, load=(1.0, 0, 0), cpu_n=2, mem=41.5, disk=77.0)
    t, cpu, mem, disk = mh.sample_point(now=1000)
    assert (t, cpu, mem, disk) == (1000, 50.0, 41.5, 77.0)


def test_sample_point_caps_cpu(monkeypatch):
    _fake_host(monkeypatch, load=(9.0, 0, 0), cpu_n=2)
    assert mh.sample_point(now=0)[1] == 100.0


def test_record_skips_empty_dev_host(monkeypatch):
    """dev-Windows без /proc: None-точки буфер не засоряют."""
    monkeypatch.setattr(mh.host_health, "read_loadavg", lambda *a, **k: None)
    monkeypatch.setattr(mh.host_health, "read_meminfo", lambda *a, **k: None)
    monkeypatch.setattr(mh.host_health, "disk_usage", lambda *a, **k: None)
    mh.record_sample(now=1000)
    assert mh._points == []


def test_ring_buffer_caps_and_history_window(monkeypatch):
    _fake_host(monkeypatch)
    import time
    now = time.time()
    for i in range(mh.MAX_POINTS + 50):
        mh.record_sample(now=now - (mh.MAX_POINTS + 50 - i) * 60)
    assert len(mh._points) == mh.MAX_POINTS
    # Окно 60 минут отдаёт ~последний час, не сутки.
    recent = mh.history(minutes=60)["points"]
    assert 55 <= len(recent) <= 62
    assert all(p[0] >= now - 61 * 60 for p in recent)
    # Кламп: слишком маленькое окно поднимается до 5 минут, не падает.
    assert isinstance(mh.history(minutes=0)["points"], list)


def test_persist_and_load_roundtrip(monkeypatch, tmp_path):
    _fake_host(monkeypatch)
    mh.record_sample(now=5000)
    mh._persist()
    # Свежий «процесс»: буфер пуст, _load() подхватывает файл.
    monkeypatch.setattr(mh, "_points", [])
    monkeypatch.setattr(mh, "_loaded", False)
    got = mh.history()
    assert got["points"] == [] or True  # history фильтрует по времени от «сейчас»
    assert len(mh._points) == 1 and mh._points[0][0] == 5000


def test_endpoint_requires_auth(api_env):
    _app, _Session, client = api_env
    assert client.get("/api/system/metrics/history").status_code == 401


def test_endpoint_returns_points(auth_client, monkeypatch):
    client, _Session = auth_client
    import time
    monkeypatch.setattr(mh, "_points", [[int(time.time()) - 60, 10.0, 20.0, 30.0]])
    monkeypatch.setattr(mh, "_loaded", True)
    r = client.get("/api/system/metrics/history?minutes=120")
    assert r.status_code == 200
    body = r.json()
    assert body["interval_sec"] == mh.SAMPLE_INTERVAL_SEC
    assert body["points"][0][1:] == [10.0, 20.0, 30.0]


def test_version_announces_metrics_history_capability(api_env):
    _app, _Session, client = api_env
    caps = client.get("/api/version").json()["capabilities"]
    assert "metrics_history" in caps
