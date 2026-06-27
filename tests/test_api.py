"""API-тесты через FastAPI TestClient.

Фикстуры `api_env`/`auth_client` — общие, в `tests/conftest.py`.
"""
from app import models, security


# --------------------------------------------------------------------------- #
def test_protected_endpoint_requires_auth(api_env):
    _, _, client = api_env
    r = client.get("/api/blueprints")
    assert r.status_code == 401


def test_blueprints_crud(auth_client):
    client, _ = auth_client
    assert client.get("/api/blueprints").json() == []

    r = client.post("/api/blueprints", json={"name": "myapp", "description": "demo"})
    assert r.status_code == 201
    assert r.json()["name"] == "myapp"

    names = [b["name"] for b in client.get("/api/blueprints").json()]
    assert "myapp" in names


def test_blueprint_duplicate_rejected(auth_client):
    client, _ = auth_client
    client.post("/api/blueprints", json={"name": "dup"})
    r = client.post("/api/blueprints", json={"name": "dup"})
    assert r.status_code == 400


def test_blueprint_patch_and_delete(auth_client):
    client, _ = auth_client
    bp_id = client.post("/api/blueprints", json={"name": "edit-me", "description": "old"}).json()["id"]

    r = client.patch(f"/api/blueprints/{bp_id}", json={"description": "new"})
    assert r.status_code == 200
    assert r.json()["description"] == "new"
    assert r.json()["name"] == "edit-me"

    assert client.delete(f"/api/blueprints/{bp_id}").status_code == 204
    assert client.patch(f"/api/blueprints/{bp_id}", json={"name": "x"}).status_code == 404


def test_blueprint_delete_blocked_by_deployment(auth_client):
    client, Session = auth_client
    bp_id = client.post("/api/blueprints", json={"name": "busy"}).json()["id"]
    s = Session()
    art = models.Artifact(version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp_id)
    s.add(art); s.commit()
    s.add(models.Deployment(blueprint_id=bp_id, artifact_id=art.id, target_replicas=1, group_name="g"))
    s.commit()

    r = client.delete(f"/api/blueprints/{bp_id}")
    assert r.status_code == 409


def test_artifact_delete_blocked_when_used(auth_client):
    client, Session = auth_client
    bp_id = client.post("/api/blueprints", json={"name": "art-busy"}).json()["id"]
    s = Session()
    art = models.Artifact(version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp_id)
    s.add(art); s.commit()
    art_id = art.id
    s.add(models.Deployment(blueprint_id=bp_id, artifact_id=art_id, target_replicas=1, group_name="g"))
    s.commit()

    r = client.delete(f"/api/blueprints/{bp_id}/artifacts/{art_id}")
    assert r.status_code == 409


def test_service_name_autogen_and_collision(auth_client):
    client, _ = auth_client
    bp_id = client.post("/api/blueprints", json={"name": "svc-name"}).json()["id"]
    # загрузим версию (минимальный валидный zip не нужен — inspect молча игнорит)
    import io
    art = client.post(f"/api/blueprints/{bp_id}/artifacts",
                      data={"version_tag": "v1"},
                      files={"zip_file": ("a.zip", io.BytesIO(b"PK\x03\x04bad"), "application/zip")})
    assert art.status_code == 200
    art_id = art.json()["id"]
    client.post("/api/groups", json={"name": "gx", "start_port": 9101, "end_port": 9110})

    # имя пусто → автоген из имени приложения
    s1 = client.post("/api/services", json={"artifact_id": art_id, "group_name": "gx"})
    assert s1.status_code == 200 and s1.json()["name"] == "svc-name"
    # повтор без имени → суффикс
    s2 = client.post("/api/services", json={"artifact_id": art_id, "group_name": "gx"})
    assert s2.json()["name"] == "svc-name-2"
    # явный дубль → 409
    dup = client.post("/api/services", json={"artifact_id": art_id, "group_name": "gx", "name": "svc-name"})
    assert dup.status_code == 409


def test_create_service_with_advanced_config(auth_client):
    """Расширенный режим (Идея 2а): база/команда/порт/env принимаются при создании
    и видны в GET /api/services."""
    client, _ = auth_client
    bp_id = client.post("/api/blueprints", json={"name": "adv-app"}).json()["id"]
    import io
    art_id = client.post(f"/api/blueprints/{bp_id}/artifacts",
                         data={"version_tag": "v1"},
                         files={"zip_file": ("a.zip", io.BytesIO(b"PK\x03\x04bad"), "application/zip")}).json()["id"]
    client.post("/api/groups", json={"name": "ga", "start_port": 9201, "end_port": 9210})
    r = client.post("/api/services", json={
        "artifact_id": art_id, "group_name": "ga",
        "base_image": "node:20-alpine", "run_command": "node index.js",
        "internal_port": 3000, "env_vars": "FOO=bar\nB=2",
    })
    assert r.status_code == 200
    sid = r.json()["id"]
    svc = next(s for s in client.get("/api/services").json() if s["id"] == sid)
    assert svc["internal_port"] == 3000
    assert svc["run_command"] == "node index.js"
    assert svc["base_image"] == "node:20-alpine"
    assert svc["env_vars"] == {"FOO": "bar", "B": "2"}


def test_patch_service_config(auth_client, monkeypatch):
    client, Session = auth_client
    import main
    monkeypatch.setattr(main.docker_manager, "build_image_if_needed", lambda *a, **k: "img:ok")
    dep_id = _make_deployment(Session, name="cfg")
    r = client.patch(f"/api/services/{dep_id}/config", json={"internal_port": 8080, "env_vars": {"X": "y"}})
    assert r.status_code == 200
    assert r.json()["internal_port"] == 8080
    svc = next(x for x in client.get("/api/services").json() if x["id"] == dep_id)
    assert svc["internal_port"] == 8080
    assert svc["env_vars"] == {"X": "y"}


def test_patch_service_config_build_failure_keeps_old(auth_client, monkeypatch):
    """Build-first (ADR-022): провал сборки с новым конфигом откатывает изменения —
    сервис остаётся на прежнем порту."""
    client, Session = auth_client
    import main

    def boom(*a, **k):
        raise RuntimeError("Ошибка сборки образа:\nboom")

    monkeypatch.setattr(main.docker_manager, "build_image_if_needed", boom)
    dep_id = _make_deployment(Session, name="cfg-fail")
    r = client.patch(f"/api/services/{dep_id}/config", json={"internal_port": 8080})
    assert r.status_code == 400
    # порт НЕ изменился (откат через db.rollback)
    svc = next(x for x in client.get("/api/services").json() if x["id"] == dep_id)
    assert svc["internal_port"] == 80


def test_patch_service_config_rejects_bad_port(auth_client):
    client, Session = auth_client
    dep_id = _make_deployment(Session, name="cfg-bad")
    assert client.patch(f"/api/services/{dep_id}/config", json={"internal_port": 99999}).status_code == 400
    assert client.patch(f"/api/services/{dep_id}/config", json={"internal_port": "abc"}).status_code == 400


def _make_deployment_two_versions(Session, name):
    """Деплой на версии v1 + вторая версия v2 для тестов redeploy."""
    s = Session()
    bp = models.AppBlueprint(name=name); s.add(bp); s.commit()
    a1 = models.Artifact(version_tag="v1", zip_hash="h1", stored_zip_path="uploads/h1.zip", blueprint_id=bp.id)
    a2 = models.Artifact(version_tag="v2", zip_hash="h2", stored_zip_path="uploads/h2.zip", blueprint_id=bp.id)
    s.add_all([a1, a2]); s.commit()
    dep = models.Deployment(blueprint_id=bp.id, artifact_id=a1.id, target_replicas=1, group_name="g")
    s.add(dep); s.commit()
    ids = (dep.id, a2.id)
    s.close()
    return ids


def test_redeploy_build_failure_keeps_old_version(auth_client, monkeypatch):
    """Build-first (ADR-022): если сборка новой версии падает — сервис остаётся на
    старой версии (DoD «неудачные деплои не ломают работающий сервис»)."""
    client, Session = auth_client
    import main
    dep_id, a2_id = _make_deployment_two_versions(Session, "redeploy-fail")

    def boom(*a, **k):
        raise RuntimeError("Ошибка сборки образа:\nboom")

    monkeypatch.setattr(main.docker_manager, "build_image_if_needed", boom)
    r = client.post(f"/api/services/{dep_id}/redeploy", json={"artifact_id": a2_id})
    assert r.status_code == 400
    svc = next(x for x in client.get("/api/services").json() if x["id"] == dep_id)
    assert svc["artifact"]["version_tag"] == "v1"  # откат — версия не сменилась


def test_redeploy_success_swaps_version(auth_client, monkeypatch):
    client, Session = auth_client
    import main
    dep_id, a2_id = _make_deployment_two_versions(Session, "redeploy-ok")
    monkeypatch.setattr(main.docker_manager, "build_image_if_needed", lambda *a, **k: "img:ok")
    r = client.post(f"/api/services/{dep_id}/redeploy", json={"artifact_id": a2_id})
    assert r.status_code == 200
    svc = next(x for x in client.get("/api/services").json() if x["id"] == dep_id)
    assert svc["artifact"]["version_tag"] == "v2"


def test_groups_crud_and_validation(auth_client):
    client, _ = auth_client

    ok = client.post("/api/groups", json={"name": "g1", "start_port": 9001, "end_port": 9010})
    assert ok.status_code == 200

    bad = client.post("/api/groups", json={"name": "g2", "start_port": 9010, "end_port": 9001})
    assert bad.status_code == 422  # start_port >= end_port


def test_system_metrics_shape(auth_client, monkeypatch):
    client, _ = auth_client
    import main
    sample = {
        "host": {"ncpu": 4, "mem_total_mb": 8000, "containers_running": 3, "containers_stopped": 1, "images": 5},
        "disk": {"images_mb": 1200.0, "volumes_mb": 64.0, "build_cache_mb": 0.0},
        "load": {"cpu_percent": 12.0, "memory_usage_mb": 480.0, "net_rx_mb": 3.1, "net_tx_mb": 0.8, "managed_running": 3},
    }
    monkeypatch.setattr(main.docker_manager, "get_system_metrics", lambda: sample)
    r = client.get("/api/system/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["host"]["ncpu"] == 4
    assert body["load"]["net_rx_mb"] == 3.1
    assert set(body.keys()) == {"host", "disk", "load"}


def test_system_metrics_resilient_to_docker_failure(auth_client, monkeypatch):
    client, _ = auth_client
    import main

    def boom():
        raise RuntimeError("docker down")

    monkeypatch.setattr(main.docker_manager, "get_system_metrics", boom)
    r = client.get("/api/system/metrics")
    assert r.status_code == 200
    assert r.json() == {"host": {}, "disk": {}, "load": {}}


def test_system_metrics_requires_auth(api_env):
    _, _, client = api_env
    assert client.get("/api/system/metrics").status_code == 401


def test_edition_endpoint_requires_auth(api_env):
    _, _, client = api_env
    assert client.get("/api/edition").status_code == 401


def test_edition_endpoint_shape(auth_client, monkeypatch):
    client, _ = auth_client
    monkeypatch.setenv("DEPLOYER_EDITION", "pro")
    r = client.get("/api/edition")
    assert r.status_code == 200
    body = r.json()
    assert body["edition"] == "pro"
    assert "features" in body and body["features"]["roles"] is True


def _make_deployment(Session, *, name="diag", build_log=None):
    s = Session()
    bp = models.AppBlueprint(name=name)
    s.add(bp); s.commit()
    art = models.Artifact(version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id)
    s.add(art); s.commit()
    dep = models.Deployment(blueprint_id=bp.id, artifact_id=art.id, target_replicas=1,
                            group_name="g", last_build_log=build_log)
    s.add(dep); s.commit()
    dep_id = dep.id
    s.close()
    return dep_id


def test_service_logs_build_failed_branch(auth_client):
    client, Session = auth_client
    dep_id = _make_deployment(Session, name="build-fail", build_log="ERROR: pip install failed")
    r = client.get(f"/api/services/{dep_id}/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "build_failed"
    assert "pip install failed" in body["logs"]


def test_service_scale_sets_target_replicas(auth_client):
    client, Session = auth_client
    dep_id = _make_deployment(Session, name="scale-me")
    r = client.post(f"/api/services/{dep_id}/scale", json={"target_replicas": 3})
    assert r.status_code == 200
    assert r.json()["target_replicas"] == 3
    # GET /api/services отражает новую цель + online_count (реплик ещё нет — оркестратор
    # в тестах не крутится).
    svc = next(s for s in client.get("/api/services").json() if s["id"] == dep_id)
    assert svc["target_replicas"] == 3
    assert svc["online_count"] == 0
    assert svc["instances_count"] == 0


def test_service_scale_rejects_out_of_range(auth_client):
    client, Session = auth_client
    dep_id = _make_deployment(Session, name="scale-bad")
    assert client.post(f"/api/services/{dep_id}/scale", json={"target_replicas": 99}).status_code == 400
    assert client.post(f"/api/services/{dep_id}/scale", json={"target_replicas": -1}).status_code == 400


def test_service_logs_falls_back_to_saved_crash_logs(auth_client):
    client, Session = auth_client
    dep_id = _make_deployment(Session, name="crash")
    s = Session()
    s.add(models.Instance(deployment_id=dep_id, container_name="missing-ctr", assigned_port=9555,
                          status="failed", exit_code=1, last_logs="ModuleNotFoundError: phonenumbers"))
    s.commit(); s.close()
    # Контейнера нет (docker недоступен в тестах) → отдаём сохранённый снимок логов.
    r = client.get(f"/api/services/{dep_id}/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["exit_code"] == 1
    assert "phonenumbers" in body["logs"]


def test_login_flow(api_env):
    _, Session, client = api_env
    s = Session()
    s.add(models.User(username="admin", hashed_password=security.get_password_hash("pw12345")))
    s.commit()
    s.close()

    ok = client.post("/api/auth/token", data={"username": "admin", "password": "pw12345"})
    assert ok.status_code == 200
    assert "access_token" in ok.json()

    wrong = client.post("/api/auth/token", data={"username": "admin", "password": "nope"})
    assert wrong.status_code == 401
