"""Здоровье хоста — `app/services/host_health.py` + `GET /api/host/health` (Ночь 13).

Чистые функции читают /proc-фикстуры из tmp_path (реального /proc на Windows-dev
нет), эндпоинт проверяется через TestClient с подменённым сбором. Пороги warnings
завязаны на живые инциденты (ADR-078: диск 100% + RAM без swap → thrash хоста).
"""
from app.services import host_health

MEMINFO_HEALTHY = """MemTotal:        2003248 kB
MemFree:          164760 kB
MemAvailable:    1436268 kB
Buffers:           93524 kB
Cached:          1122880 kB
SwapCached:            0 kB
SwapTotal:       2097148 kB
SwapFree:        2097148 kB
"""

MEMINFO_NO_SWAP_TIGHT = """MemTotal:        2003248 kB
MemFree:           50000 kB
MemAvailable:      80000 kB
SwapTotal:             0 kB
SwapFree:              0 kB
"""


def test_read_meminfo_parses_memory_and_swap(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text(MEMINFO_HEALTHY, encoding="utf-8")
    parsed = host_health.read_meminfo(str(p))
    assert parsed["memory"]["total_mb"] == 1956
    assert parsed["memory"]["available_mb"] == 1403
    assert 25 < parsed["memory"]["used_pct"] < 30
    assert parsed["swap"]["total_mb"] == 2048
    assert parsed["swap"]["used_pct"] == 0.0


def test_read_meminfo_missing_file_returns_none(tmp_path):
    assert host_health.read_meminfo(str(tmp_path / "nope")) is None


def test_read_loadavg_and_uptime(tmp_path):
    (tmp_path / "loadavg").write_text("0.52 1.10 2.30 2/345 6789\n", encoding="utf-8")
    (tmp_path / "uptime").write_text("123456.78 654321.00\n", encoding="utf-8")
    assert host_health.read_loadavg(str(tmp_path / "loadavg")) == [0.52, 1.10, 2.30]
    assert host_health.read_uptime(str(tmp_path / "uptime")) == 123456
    assert host_health.read_loadavg(str(tmp_path / "no")) is None
    assert host_health.read_uptime(str(tmp_path / "no")) is None


def test_disk_usage_reports_current_fs():
    du = host_health.disk_usage(".")
    assert du["total_gb"] > 0
    assert 0 <= du["used_pct"] <= 100


def test_assess_healthy_snapshot_is_ok():
    status, warnings = host_health.assess({
        "disk": {"used_pct": 45.0},
        "memory": {"used_pct": 30.0},
        "swap": {"total_mb": 2048, "used_pct": 0.0},
        "load": [0.1, 0.2, 0.1],
        "cpu_count": 2,
    })
    assert status == "ok"
    assert warnings == []


def test_assess_flags_crit_disk_and_missing_swap():
    """Профиль инцидента ADR-078: диск под завязку, swap нет — crit + оба warning'а."""
    status, warnings = host_health.assess({
        "disk": {"used_pct": 97.3},
        "memory": {"used_pct": 85.0},
        "swap": {"total_mb": 0, "used_pct": None},
        "load": [5.0, 6.1, 4.0],
        "cpu_count": 1,
    })
    assert status == "crit"
    joined = " | ".join(warnings)
    assert "Диск заполнен на 97%" in joined
    assert "Память занята на 85%" in joined
    assert "Swap отсутствует" in joined
    assert "Высокая нагрузка" in joined


def test_assess_tolerates_empty_snapshot():
    """Windows-dev/сломанный сбор: все источники None — ok без ложных тревог."""
    status, warnings = host_health.assess({
        "disk": None, "memory": None, "swap": None,
        "load": None, "cpu_count": None,
    })
    assert status == "ok"
    assert warnings == []


def test_collect_always_builds_snapshot():
    """Снимок собирается даже без /proc (dev) и без Docker-клиента."""
    snap = host_health.collect(docker_client=None)
    assert set(snap) >= {"disk", "memory", "swap", "load", "cpu_count",
                         "uptime_sec", "docker", "status", "warnings"}
    assert snap["docker"] is None
    assert snap["status"] in ("ok", "warn", "crit")


class _FakeInfoClient:
    def info(self):
        return {"ContainersRunning": 3, "Images": 7, "ServerVersion": "26.0.1"}


def test_docker_summary_from_client_info():
    assert host_health.docker_summary(_FakeInfoClient()) == {
        "containers_running": 3, "images": 7, "server_version": "26.0.1"}


def test_endpoint_requires_auth(api_env):
    _app, _Session, client = api_env
    assert client.get("/api/host/health").status_code == 401


def test_endpoint_returns_snapshot(auth_client, monkeypatch):
    client, _Session = auth_client
    import main
    monkeypatch.setattr(main, "get_docker_client", lambda: _FakeInfoClient())
    r = client.get("/api/host/health")
    assert r.status_code == 200
    body = r.json()
    assert body["docker"] == {"containers_running": 3, "images": 7,
                              "server_version": "26.0.1"}
    assert body["status"] in ("ok", "warn", "crit")
    assert isinstance(body["warnings"], list)


def test_version_announces_host_health_capability(api_env):
    """ЛК гейтит зеркало здоровья по capability (инвариант №8)."""
    _app, _Session, client = api_env
    caps = client.get("/api/version").json()["capabilities"]
    assert "host_health" in caps
