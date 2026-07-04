"""Прогресс и замеры операций (Ночь 14, ADR-082).

Три слоя:
  • парсер docker-событий сборки/пулла (`build_progress.BuildProgressParser`) —
    стадии pull/build, проценты по байтам/шагам, троттлинг pull-строк;
  • реестр активных сборок + замер `OperationMetric` (build) на финише;
  • crud-статистика/ETA (`operation_stats`, `avg_seconds`) и API: стадия+ETA в
    `/api/pending-actions`, живой прогресс в `/api/services`,
    `GET /api/operation-metrics`; замеры dns_wait/ssl_issue/self_update в
    обработчиках фоновых задач.
Внешний мир (Docker/DNS/certbot) — на моках.
"""
import json
import time

import pytest

from app import crud, models
from app.services import build_progress, op_metrics
from app.services import pending_actions as pa
from app.services.build_progress import BuildProgressParser


# --------------------------------------------------------------------------- #
#  Парсер docker-событий
# --------------------------------------------------------------------------- #

def test_parser_build_steps_progress():
    p = BuildProgressParser()
    line = p.feed({"stream": "Step 2/8 : RUN pip install -r requirements.txt\n"})
    assert line.startswith("Step 2/8")
    st = p.state()
    assert st["stage"] == "build"
    assert st["percent"] == 25  # 2/8, потолок 99 — выполняющийся шаг не завершён
    assert "шаг 2/8" in st["detail"]
    assert "pip install" in st["detail"]


def test_parser_build_percent_capped_at_99():
    p = BuildProgressParser()
    p.feed({"stream": "Step 8/8 : CMD [\"uvicorn\"]\n"})
    assert p.state()["percent"] == 99  # 100% только по факту финиша сборки


def test_parser_pull_bytes_and_throttle():
    p = BuildProgressParser()
    assert "Скачивание базового образа" in p.feed(
        {"status": "Pulling from library/python", "id": "3.12-slim"})
    mb = 1024 * 1024
    # Пока известен один слой 100 МБ, скачано 50 → 50%, строка эмитится.
    line = p.feed({"status": "Downloading", "id": "l1",
                   "progressDetail": {"current": 50 * mb, "total": 100 * mb}})
    assert line and "50%" in line
    # Появился второй слой 100 МБ → общий прогресс пересчитан: (50+0)/200 = 25%.
    p.feed({"status": "Downloading", "id": "l2",
            "progressDetail": {"current": 0, "total": 100 * mb}})
    st = p.state()
    assert st["stage"] == "pull" and st["percent"] == 25
    assert "50/200 МБ" in st["detail"]
    # Микрошаг +1 п.п. — строка НЕ эмитится (троттлинг), но состояние живое.
    assert p.feed({"status": "Downloading", "id": "l1",
                   "progressDetail": {"current": 52 * mb, "total": 100 * mb}}) is None
    assert p.state()["percent"] == 26
    # Скачок ≥10 п.п. от последней эмиссии (50%) → новая строка: (52+90)/200 = 71%.
    line = p.feed({"status": "Downloading", "id": "l2",
                   "progressDetail": {"current": 90 * mb, "total": 100 * mb}})
    assert line and "71%" in line


def test_parser_pull_then_build_switches_stage():
    p = BuildProgressParser()
    p.feed({"status": "Downloading", "id": "l1",
            "progressDetail": {"current": 1, "total": 2}})
    assert p.state()["stage"] == "pull"
    p.feed({"stream": "Step 3/5 : COPY . .\n"})
    assert p.state()["stage"] == "build"
    assert p.pull_seconds() is not None  # фаза пулла замерена


def test_parser_error_chunk_returned_as_line():
    p = BuildProgressParser()
    assert p.feed({"error": "boom failed\n"}) == "boom failed"


def test_parser_layer_done_counts_full_bytes():
    p = BuildProgressParser()
    mb = 1024 * 1024
    p.feed({"status": "Downloading", "id": "l1",
            "progressDetail": {"current": 10 * mb, "total": 100 * mb}})
    p.feed({"status": "Pull complete", "id": "l1"})
    assert p.pull_percent() == 100


# --------------------------------------------------------------------------- #
#  Реестр активных сборок + замер build
# --------------------------------------------------------------------------- #

def test_registry_lifecycle_and_metric(monkeypatch):
    recorded = []
    monkeypatch.setattr(op_metrics, "record",
                        lambda *a, **k: recorded.append((a, k)))
    tag = "deployer-cache:testtag"
    assert build_progress.get(tag) is None
    build_progress.begin(tag)
    build_progress.feed(tag, {"stream": "Step 1/4 : FROM python:3.12-slim\n"})
    snap = build_progress.get(tag)
    assert snap["stage"] == "build" and snap["percent"] == 25
    assert "elapsed_seconds" in snap
    build_progress.finish(tag, ok=True)
    assert build_progress.get(tag) is None
    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args[0] == "build" and kwargs["outcome"] == "done"
    assert kwargs["meta"]["steps"] == 4


def test_registry_feed_unknown_tag_is_noop():
    assert build_progress.feed("deployer-cache:nope", {"stream": "Step 1/2 : X"}) is None
    build_progress.finish("deployer-cache:nope", ok=True)  # не падает


def test_streaming_build_reports_pull_lines(monkeypatch, tmp_path):
    """Pull-события базового образа доходят до WS-лога (раньше терялись —
    долгий pull выглядел зависанием)."""
    import zipfile
    import docker as docker_lib
    from app.services import docker_manager

    zip_path = tmp_path / "a.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("main.py", "x = 1")

    mb = 1024 * 1024

    class _Api:
        def build(self, **kwargs):
            yield {"status": "Pulling from library/python", "id": "3.12-slim"}
            yield {"status": "Downloading", "id": "l1",
                   "progressDetail": {"current": 60 * mb, "total": 100 * mb}}
            yield {"stream": "Step 1/2 : FROM python:3.12-slim\n"}
            yield {"stream": "Step 2/2 : COPY . .\n"}

    class _NoImage:
        def get(self, tag):
            raise docker_lib.errors.ImageNotFound("no")

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    recorded = []
    monkeypatch.setattr(op_metrics, "record", lambda *a, **k: recorded.append(k))
    lines = []
    tag = docker_manager.build_image_if_needed(zip_path, image_cache_key="hp",
                                               on_line=lines.append)
    assert any("Скачивание базового образа" in ln for ln in lines)
    assert any("60%" in ln for ln in lines)
    assert any(ln.startswith("Step 2/2") for ln in lines)
    assert build_progress.get(tag) is None      # сборка снята с учёта
    assert recorded and recorded[0]["outcome"] == "done"
    assert recorded[0]["meta"]["pull_seconds"] is not None


# --------------------------------------------------------------------------- #
#  crud: запись, статистика, ретенция; ETA
# --------------------------------------------------------------------------- #

def test_operation_stats_only_done_rows(db):
    crud.record_operation_metric(db, kind="build", duration_seconds=100.0)
    crud.record_operation_metric(db, kind="build", duration_seconds=200.0)
    crud.record_operation_metric(db, kind="build", duration_seconds=999.0, outcome="error")
    crud.record_operation_metric(db, kind="ssl_issue", duration_seconds=30.0)
    stats = crud.operation_stats(db)
    assert stats["build"] == {"avg": 150.0, "samples": 2}  # error не в среднем
    assert stats["ssl_issue"]["samples"] == 1


def test_operation_metrics_retention(db, monkeypatch):
    monkeypatch.setattr(crud, "OPERATION_METRICS_KEEP", 5)
    for i in range(8):
        crud.record_operation_metric(db, kind="build", duration_seconds=float(i))
    rows = db.query(models.OperationMetric).filter_by(kind="build").all()
    assert len(rows) == 5
    assert min(r.duration_seconds for r in rows) == 3.0  # старые подрезаны


def test_avg_seconds_default_until_enough_samples(db):
    # Нет замеров → дефолтный ориентир; dns_wait без ориентира → None (вилка).
    assert op_metrics.avg_seconds(db, "build") == op_metrics.DEFAULT_AVG["build"]
    assert op_metrics.avg_seconds(db, "dns_wait") is None
    crud.record_operation_metric(db, kind="build", duration_seconds=300.0)
    assert op_metrics.avg_seconds(db, "build") == op_metrics.DEFAULT_AVG["build"]
    crud.record_operation_metric(db, kind="build", duration_seconds=100.0)
    assert op_metrics.avg_seconds(db, "build") == 200  # ≥2 замеров → своё среднее


# --------------------------------------------------------------------------- #
#  Замеры в обработчиках фоновых задач
# --------------------------------------------------------------------------- #

@pytest.fixture
def patch_side_effects(monkeypatch):
    monkeypatch.setattr(pa.nginx_manager, "update_application_nginx_config",
                        lambda *a, **k: None)
    monkeypatch.setattr(pa.nginx_manager, "reload_nginx", lambda *a, **k: None)
    monkeypatch.setattr(pa, "_save_panel", lambda *a, **k: None)


def _make_action(db, type, params):
    return crud.create_pending_action(db, type=type, title="t",
                                      params=json.dumps(params))


def test_advance_ssl_records_dns_wait_and_ssl_issue(db, deployment,
                                                    patch_side_effects, monkeypatch):
    monkeypatch.setattr(pa, "dns_matches", lambda d: (True, "ок"))
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (True, "выпущен"))
    action = _make_action(db, "issue_ssl", {"domain": "app.example.com", "app_id": None,
                                            "wait_since": time.time() - 120})
    pa._handle_issue_ssl(db, action)
    assert action.status == "done"
    kinds = {r.kind: r for r in db.query(models.OperationMetric).all()}
    assert kinds["dns_wait"].outcome == "done"
    assert 100 <= kinds["dns_wait"].duration_seconds <= 200
    assert kinds["ssl_issue"].outcome == "done"


def test_dns_confirm_recorded_once_across_ssl_retries(db, deployment,
                                                      patch_side_effects, monkeypatch):
    """dns_wait пишется ОДИН раз (на подтверждении), а не на каждой SSL-попытке."""
    monkeypatch.setattr(pa, "dns_matches", lambda d: (True, "ок"))
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (False, "не вышло"))
    action = _make_action(db, "issue_ssl", {"domain": "a.example.com", "app_id": None})
    pa._handle_issue_ssl(db, action)
    pa._handle_issue_ssl(db, action)
    dns_rows = db.query(models.OperationMetric).filter_by(kind="dns_wait").all()
    ssl_rows = db.query(models.OperationMetric).filter_by(kind="ssl_issue").all()
    assert len(dns_rows) == 1
    assert len(ssl_rows) == 2 and all(r.outcome == "error" for r in ssl_rows)


def test_dns_timeout_records_error_wait(db, deployment, patch_side_effects, monkeypatch):
    monkeypatch.setattr(pa, "dns_matches", lambda d: (False, "нет"))
    action = _make_action(db, "issue_ssl",
                          {"domain": "a.example.com", "app_id": None, "wait_since": 0.0})
    pa._handle_issue_ssl(db, action)
    assert action.status == "error"
    row = db.query(models.OperationMetric).filter_by(kind="dns_wait").one()
    assert row.outcome == "error"


def test_self_update_finish_records_metric(db, monkeypatch):
    from app.services import self_update
    monkeypatch.setattr(self_update, "updater_status", lambda: ("exited", 0, "ok logs"))
    monkeypatch.setattr(self_update, "cleanup_updater", lambda: None)
    monkeypatch.setattr(self_update, "read_update_state", lambda: {"current_ref": "abc123"})
    action = _make_action(db, "self_update",
                          {"started": True, "started_ts": time.time() - 90, "ref": None})
    pa._handle_self_update(db, action)
    assert action.status == "done"
    row = db.query(models.OperationMetric).filter_by(kind="self_update").one()
    assert row.outcome == "done" and 60 <= row.duration_seconds <= 150


def test_self_update_already_up_to_date_not_measured(db, monkeypatch):
    """«Уже актуально» — не обновление: замер испортил бы среднее для ETA."""
    from app.services import self_update
    monkeypatch.setattr(self_update, "updater_status",
                        lambda: ("exited", 0, "ALREADY_UP_TO_DATE"))
    monkeypatch.setattr(self_update, "cleanup_updater", lambda: None)
    action = _make_action(db, "self_update",
                          {"started": True, "started_ts": time.time() - 30, "ref": None})
    pa._handle_self_update(db, action)
    assert action.status == "done"
    assert db.query(models.OperationMetric).count() == 0


# --------------------------------------------------------------------------- #
#  Стадия задачи для UI (describe_stage)
# --------------------------------------------------------------------------- #

def test_describe_stage_publish_then_dns_then_ssl(db, deployment):
    action = _make_action(db, "publish_on_dns", {"domain": "a.example.com"})
    st = pa.describe_stage(db, action)
    assert st["stage"] == "publish"

    action.params = json.dumps({"domain": "a.example.com", "app_id": 1,
                                "wait_since": time.time()})
    st = pa.describe_stage(db, action)
    assert st["stage"] == "dns_wait"
    assert st["unpredictable"] is True and st["eta_seconds"] is None
    assert "суток" in st["hint"]  # честная вилка вместо ложного ETA

    action.params = json.dumps({"domain": "a.example.com", "app_id": 1,
                                "dns_confirmed": True})
    st = pa.describe_stage(db, action)
    assert st["stage"] == "ssl_issue"
    assert st["eta_seconds"] == op_metrics.DEFAULT_AVG["ssl_issue"]


def test_describe_stage_none_for_finished(db, deployment):
    action = _make_action(db, "issue_ssl", {"domain": "a.example.com"})
    action.status = "done"
    assert pa.describe_stage(db, action) is None


def test_describe_stage_self_update_eta_shrinks(db):
    action = _make_action(db, "self_update",
                          {"started": True, "started_ts": time.time() - 100})
    action.status = "running"
    st = pa.describe_stage(db, action)
    assert st["stage"] == "self_update"
    expected = op_metrics.DEFAULT_AVG["self_update"] - 100
    assert abs(st["eta_seconds"] - expected) <= 5


# --------------------------------------------------------------------------- #
#  API: стадии в списке задач, прогресс в /api/services, /api/operation-metrics
# --------------------------------------------------------------------------- #

def test_pending_list_exposes_stage(auth_client):
    client, Session = auth_client
    s = Session()
    crud.create_pending_action(
        s, type="issue_ssl", title="SSL",
        params=json.dumps({"domain": "a.example.com", "app_id": None,
                           "wait_since": time.time()}))
    s.close()
    rows = client.get("/api/pending-actions").json()
    assert rows[0]["stage"] == "dns_wait"
    assert rows[0]["unpredictable"] is True
    assert rows[0]["stage_label"]


def test_retry_resets_dns_confirmed(auth_client):
    client, Session = auth_client
    s = Session()
    action = crud.create_pending_action(
        s, type="issue_ssl", title="SSL",
        params=json.dumps({"domain": "a.example.com", "dns_confirmed": True,
                           "ssl_attempts": 3}))
    action.status = "error"
    s.commit()
    aid = action.id
    s.close()
    r = client.post(f"/api/pending-actions/{aid}/retry")
    assert r.status_code == 200
    s = Session()
    fresh = crud.get_pending_action(s, aid)
    params = json.loads(fresh.params)
    assert "dns_confirmed" not in params and "ssl_attempts" not in params
    s.close()


def _seed_service(Session):
    s = Session()
    group = models.AppGroup(name="g", start_port=9001, end_port=9003)
    bp = models.AppBlueprint(name="app")
    s.add_all([group, bp])
    s.commit()
    art = models.Artifact(version_tag="v1", zip_hash="hash-x",
                          stored_zip_path="uploads/x.zip", blueprint_id=bp.id)
    s.add(art)
    s.commit()
    dep = models.Deployment(blueprint_id=bp.id, artifact_id=art.id,
                            target_replicas=1, group_name="g")
    s.add(dep)
    s.commit()
    dep_id = dep.id
    s.close()
    return dep_id


def test_services_expose_live_build_progress(auth_client, monkeypatch):
    from app import run_config
    from app.services import docker_manager
    client, Session = auth_client
    _seed_service(Session)
    tag = docker_manager.compute_image_tag("hash-x", {
        "base_image": None, "run_command": None,
        "internal_port": run_config.effective_port(None)})
    build_progress.begin(tag)
    try:
        build_progress.feed(tag, {"stream": "Step 1/4 : FROM python:3.12-slim\n"})
        srv = client.get("/api/services").json()[0]
        assert srv["build"]["stage"] == "build"
        assert srv["build"]["percent"] == 25
        assert srv["build"]["eta_seconds"] > 0  # ETA по дефолтному ориентиру
    finally:
        monkeypatch.setattr(op_metrics, "record", lambda *a, **k: None)
        build_progress.finish(tag, ok=True)
    assert client.get("/api/services").json()[0]["build"] is None


def test_operation_metrics_endpoint(auth_client):
    client, Session = auth_client
    s = Session()
    crud.record_operation_metric(s, kind="build", subject="abc", duration_seconds=42.0)
    s.close()
    data = client.get("/api/operation-metrics").json()
    assert data["stats"]["build"]["samples"] == 1
    assert data["rows"][0]["kind"] == "build"
    assert data["rows"][0]["duration_seconds"] == 42.0


def test_operation_metrics_requires_auth(api_env):
    _app, _Session, client = api_env
    assert client.get("/api/operation-metrics").status_code == 401


def test_capability_declared():
    from app import version
    assert version.supports("op_metrics")
