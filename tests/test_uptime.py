"""Аптайм работающей реплики в `GET /api/services` (#6).

Ядро отдаёт `started_at`/`uptime_seconds` по первой реплике сервиса — ЛК
зеркалит это в строку приложения (счётчик аптайма контейнера). Источник —
`Instance.deployed_at` (onupdate): у стабильно работающей реплики это момент
перехода в `online`, поэтому «сейчас − deployed_at» ≈ аптайм контейнера.

Граничные случаи по заданию: свежий старт (маленький аптайм — честный сигнал
недавнего перезапуска), оффлайн/упавший сервис (None — там deployed_at это
момент СБОЯ, а не работы), отсутствие данных (None), рассинхрон часов (не
уходит в минус).
"""
from datetime import datetime, timedelta, timezone

from app import models


def _utcnow_naive():
    """Naive-UTC «сейчас» — как SQLite хранит CURRENT_TIMESTAMP (без tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_service(Session, *, status="online", deployed_at="auto",
                  with_instance=True, port=9301, name="up-app"):
    """Deployment (+ опционально Instance) напрямую в БД — как _make_deployment
    в test_api.py, но с управляемой репликой для сценариев аптайма."""
    s = Session()
    bp = models.AppBlueprint(name=name)
    s.add(bp)
    s.commit()
    art = models.Artifact(version_tag="v1", zip_hash=f"h-{name}",
                          stored_zip_path="uploads/h.zip", blueprint_id=bp.id)
    s.add(art)
    s.commit()
    dep = models.Deployment(blueprint_id=bp.id, artifact_id=art.id,
                            target_replicas=1, group_name="g")
    s.add(dep)
    s.commit()
    dep_id = dep.id
    if with_instance:
        inst = models.Instance(deployment_id=dep_id, assigned_port=port, status=status)
        s.add(inst)
        s.commit()
        if deployed_at != "auto":
            # Явное присваивание побеждает onupdate=func.now() — фиксируем момент.
            inst.deployed_at = deployed_at
            s.commit()
    s.close()
    return dep_id


def _svc(client, dep_id):
    return next(x for x in client.get("/api/services").json() if x["id"] == dep_id)


def test_uptime_fresh_online_instance(auth_client):
    """Свежий старт: маленький аптайм (≥0) — это ок, сигналит о недавнем запуске."""
    client, Session = auth_client
    dep_id = _make_service(Session, status="online", name="fresh")
    svc = _svc(client, dep_id)
    assert isinstance(svc["uptime_seconds"], int)
    assert 0 <= svc["uptime_seconds"] < 300
    # started_at — валидный ISO-момент в UTC (naive-UTC из SQLite получает +00:00).
    started = datetime.fromisoformat(svc["started_at"])
    assert started.tzinfo is not None


def test_uptime_hour_old_instance(auth_client):
    """Час работы → ~3600 c (человекочитаемый формат рисует уже клиент ЛК)."""
    client, Session = auth_client
    dep_id = _make_service(Session, status="online",
                           deployed_at=_utcnow_naive() - timedelta(hours=1), name="hour")
    svc = _svc(client, dep_id)
    assert 3500 <= svc["uptime_seconds"] <= 3700


def test_uptime_none_for_failed_and_offline(auth_client):
    """Не-online реплика: deployed_at — момент сбоя, не работы → честный None."""
    client, Session = auth_client
    failed_id = _make_service(Session, status="failed", name="dead", port=9302)
    off_id = _make_service(Session, status="offline", name="stopped", port=9303)
    for dep_id in (failed_id, off_id):
        svc = _svc(client, dep_id)
        assert svc["uptime_seconds"] is None
        assert svc["started_at"] is None


def test_uptime_none_without_instance_or_timestamp(auth_client):
    """Нет реплики вовсе / нет метки времени → None, эндпоинт не падает."""
    client, Session = auth_client
    no_inst = _make_service(Session, with_instance=False, name="bare")
    no_ts = _make_service(Session, status="online", deployed_at=None,
                          name="no-ts", port=9304)
    assert _svc(client, no_inst)["uptime_seconds"] is None
    assert _svc(client, no_ts)["uptime_seconds"] is None
    assert _svc(client, no_ts)["started_at"] is None


def test_uptime_multi_replica_deterministic_online_pick(auth_client):
    """Дофикс по ревью: у relationship нет order_by — «первая» реплика формально
    не гарантирована. Аптайм берётся с МЛАДШЕЙ по id online-реплики (стабильна
    между поллами — чип не прыгает), а не-online instances[0] не гасит чип,
    когда рядом есть живая реплика."""
    client, Session = auth_client
    dep_id = _make_service(Session, status="starting", name="multi", port=9306)
    s = Session()
    # Вторая реплика — online, час аптайма; первая (младший id) — starting.
    s.add(models.Instance(deployment_id=dep_id, assigned_port=9307, status="online"))
    s.commit()
    inst2 = s.query(models.Instance).filter_by(assigned_port=9307).one()
    inst2.deployed_at = _utcnow_naive() - timedelta(hours=1)
    s.commit()
    s.close()

    svc = _svc(client, dep_id)
    # instances[0] (starting) не прячет аптайм — взят с online-реплики.
    assert svc["uptime_seconds"] is not None
    assert 3500 <= svc["uptime_seconds"] <= 3700

    # Обе online: выбирается младшая по id (детерминизм) — свежая реплика.
    s = Session()
    inst1 = s.query(models.Instance).filter_by(assigned_port=9306).one()
    inst1.status = "online"
    s.commit()
    inst1.deployed_at = _utcnow_naive() - timedelta(minutes=5)
    s.commit()
    s.close()
    svc = _svc(client, dep_id)
    assert 250 <= svc["uptime_seconds"] <= 400


def test_uptime_clock_skew_clamped_to_zero(auth_client):
    """deployed_at «в будущем» (рассинхрон часов) → 0, а не отрицательный аптайм."""
    client, Session = auth_client
    dep_id = _make_service(Session, status="online",
                           deployed_at=_utcnow_naive() + timedelta(hours=1),
                           name="skew", port=9305)
    assert _svc(client, dep_id)["uptime_seconds"] == 0
