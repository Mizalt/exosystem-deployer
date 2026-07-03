"""Фоновые задачи панели (Ночь 10, ADR-069).

Проверяем и сервис-исполнитель (`app/services/pending_actions.py` — стейт-машина
«ждать DNS → опубликовать → SSL», бэкофф, таймаут), и enqueue/список/retry/dismiss
роуты (`/api/pending-actions/*`). Внешний мир (DNS/certbot/nginx) — на моках: тесты
не ходят в сеть и не трогают Docker.
"""
import json
from datetime import datetime, timedelta

import pytest

from app import crud, models
from app.services import pending_actions as pa


# --------------------------------------------------------------------------- #
#  Юниты исполнителя (стейт-машина) — на голой in-memory сессии (`db`).
# --------------------------------------------------------------------------- #
@pytest.fixture
def patch_side_effects(monkeypatch):
    """Глушим побочные эффекты (nginx/панель), чтобы юниты не трогали Docker/диск."""
    monkeypatch.setattr(pa.nginx_manager, "update_application_nginx_config", lambda *a, **k: None)
    monkeypatch.setattr(pa.nginx_manager, "reload_nginx", lambda *a, **k: None)
    monkeypatch.setattr(pa, "_save_panel", lambda *a, **k: None)


def _make_action(db, type, params):
    return crud.create_pending_action(db, type=type, title="t",
                                      params=json.dumps(params))


def _publish_params(dep, **over):
    p = {"domain": "app.example.com", "name": "app", "service_id": dep.id,
         "ssl_mode": "issue", "existing_cert": None}
    p.update(over)
    return p


def test_publish_none_publishes_http_immediately(db, deployment, patch_side_effects):
    action = _make_action(db, "publish_on_dns", _publish_params(deployment, ssl_mode="none"))
    pa._handle_publish(db, action)
    assert action.status == "done"
    assert action.result == "http://app.example.com"
    app_row = crud.get_application_by_domain(db, "app.example.com")
    assert app_row is not None and app_row.ssl_cert_name is None


def test_publish_existing_cert_binds_and_done(db, deployment, patch_side_effects):
    action = _make_action(db, "publish_on_dns",
                          _publish_params(deployment, ssl_mode="existing", existing_cert="mycert"))
    pa._handle_publish(db, action)
    assert action.status == "done"
    assert crud.get_application_by_domain(db, "app.example.com").ssl_cert_name == "mycert"


def test_publish_issue_waits_dns_then_issues(db, deployment, patch_side_effects, monkeypatch):
    """DoD: публикация с SSL при неготовом DNS не блокирует, задача сама доходит до HTTPS."""
    action = _make_action(db, "publish_on_dns", _publish_params(deployment))

    # 1) DNS ещё не указывает сюда — приложение публикуется по HTTP, задача ждёт.
    monkeypatch.setattr(pa, "dns_matches", lambda d: (False, "нет A-записи"))
    issued = {"n": 0}
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (issued.__setitem__("n", issued["n"] + 1), (True, "log"))[1])
    pa._handle_publish(db, action)
    assert action.status == "pending"
    assert action.next_check_at is not None
    app_row = crud.get_application_by_domain(db, "app.example.com")
    assert app_row is not None and app_row.ssl_cert_name is None  # опубликовано по HTTP
    assert issued["n"] == 0  # SSL ещё не пробовали

    # 2) DNS распространился — тот же чекер выпускает SSL и привязывает.
    monkeypatch.setattr(pa, "dns_matches", lambda d: (True, "домен указывает на 1.2.3.4"))
    pa._handle_publish(db, action)
    assert action.status == "done"
    assert action.result == "https://app.example.com"
    assert issued["n"] == 1
    assert crud.get_application_by_domain(db, "app.example.com").ssl_cert_name == "app.example.com"


def test_publish_conflicting_domain_errors(db, deployment, patch_side_effects):
    db.add(models.Application(name="taken", domain="app.example.com", deployment_id=deployment.id))
    db.commit()
    action = _make_action(db, "publish_on_dns", _publish_params(deployment))
    pa._handle_publish(db, action)
    assert action.status == "error"
    assert "уже используется" in action.result


def test_dns_wait_times_out(db, deployment, patch_side_effects, monkeypatch):
    action = _make_action(db, "issue_ssl", {"domain": "app.example.com", "app_id": None})
    monkeypatch.setattr(pa, "dns_matches", lambda d: (False, "нет"))
    # Окно ожидания «истекло» — задача проваливается, а не ждёт вечно.
    params = {"domain": "app.example.com", "app_id": None,
              "wait_since": 0.0}  # эпоха 1970 → старше лимита
    action.params = json.dumps(params)
    pa._handle_issue_ssl(db, action)
    assert action.status == "error"
    assert "36 час" in action.result


def test_issue_ssl_retry_backoff_then_fail(db, deployment, patch_side_effects, monkeypatch):
    action = _make_action(db, "issue_ssl", {"domain": "app.example.com", "app_id": None})
    monkeypatch.setattr(pa, "dns_matches", lambda d: (True, "ок"))
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (False, "не вышло"))
    for _ in range(pa.SSL_MAX_ATTEMPTS - 1):
        pa._handle_issue_ssl(db, action)
        assert action.status == "pending"
        assert action.next_check_at is not None
    pa._handle_issue_ssl(db, action)
    assert action.status == "error"


def test_issue_ssl_binds_to_app(db, deployment, patch_side_effects, monkeypatch):
    app_row = models.Application(name="mine", domain="app.example.com", deployment_id=deployment.id)
    db.add(app_row)
    db.commit()
    action = _make_action(db, "issue_ssl", {"domain": "app.example.com", "app_id": app_row.id})
    monkeypatch.setattr(pa, "dns_matches", lambda d: (True, "ок"))
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (True, "выпущен"))
    pa._handle_issue_ssl(db, action)
    assert action.status == "done"
    db.refresh(app_row)
    assert app_row.ssl_cert_name == "app.example.com"


def test_panel_ssl_saves_http_then_binds(db, deployment, monkeypatch):
    calls = []
    monkeypatch.setattr(pa, "_save_panel", lambda domain, cert: calls.append((domain, cert)))
    monkeypatch.setattr(pa, "dns_matches", lambda d: (True, "ок"))
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (True, "выпущен"))
    action = _make_action(db, "panel_ssl", {"domain": "panel.example.com"})
    pa._handle_panel_ssl(db, action)
    assert action.status == "done"
    # Сначала сохранён по HTTP (cert=None), затем привязан сертификат (cert=domain).
    assert calls == [("panel.example.com", None), ("panel.example.com", "panel.example.com")]


def test_unknown_type_fails_in_pass(db, monkeypatch):
    action = _make_action(db, "bogus", {})
    # process_due_actions открывает свою сессию → направим её на тестовую.
    monkeypatch.setattr(pa, "SessionLocal", lambda: _NoCloseSession(db))
    pa.process_due_actions()
    db.refresh(action)
    assert action.status == "error"
    assert "Неизвестный тип" in action.result


class _NoCloseSession:
    """Обёртка над тестовой сессией: .close() не закрывает реальную (её закроет фикстура)."""
    def __init__(self, real):
        self._real = real

    def __getattr__(self, item):
        return getattr(self._real, item)

    def close(self):
        pass


def test_list_due_respects_next_check_at(db, deployment):
    ready = _make_action(db, "issue_ssl", {"domain": "a.example.com"})
    later = _make_action(db, "issue_ssl", {"domain": "b.example.com"})
    later.next_check_at = datetime.utcnow() + timedelta(hours=1)
    done = _make_action(db, "issue_ssl", {"domain": "c.example.com"})
    done.status = "done"
    db.commit()
    due_ids = [a.id for a in crud.list_due_pending_actions(db, datetime.utcnow())]
    assert ready.id in due_ids
    assert later.id not in due_ids   # запланирована на будущее
    assert done.id not in due_ids    # уже завершена


# --------------------------------------------------------------------------- #
#  Enqueue/список/retry/dismiss через API (auth_client).
# --------------------------------------------------------------------------- #
def _seed_deployment(Session):
    s = Session()
    try:
        s.add(models.AppGroup(name="backend-services", start_port=9001, end_port=9010))
        bp = models.AppBlueprint(name="qwe")
        s.add(bp)
        s.commit()
        art = models.Artifact(version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip",
                              blueprint_id=bp.id)
        s.add(art)
        s.commit()
        dep = models.Deployment(blueprint_id=bp.id, artifact_id=art.id, target_replicas=1,
                                group_name="backend-services")
        s.add(dep)
        s.commit()
        return dep.id
    finally:
        s.close()


def test_enqueue_publish_creates_pending(auth_client):
    client, Session = auth_client
    dep_id = _seed_deployment(Session)
    r = client.post("/api/pending-actions/publish",
                    json={"service_id": dep_id, "domain": "app.example.com", "ssl_mode": "issue"})
    assert r.status_code == 201
    body = r.json()
    assert body["type"] == "publish_on_dns"
    assert body["status"] == "pending"
    # Виден в списке активных.
    active = client.get("/api/pending-actions", params={"active_only": True}).json()
    assert [a["id"] for a in active] == [body["id"]]


def test_enqueue_publish_validates(auth_client):
    client, Session = auth_client
    dep_id = _seed_deployment(Session)
    # Несуществующий сервис.
    assert client.post("/api/pending-actions/publish",
                       json={"service_id": 999, "domain": "x.example.com"}).status_code == 404
    # existing без сертификата.
    assert client.post("/api/pending-actions/publish",
                       json={"service_id": dep_id, "domain": "x.example.com",
                             "ssl_mode": "existing"}).status_code == 400
    # Занятый домен.
    s = Session()
    s.add(models.Application(name="taken", domain="busy.example.com", deployment_id=dep_id))
    s.commit()
    s.close()
    assert client.post("/api/pending-actions/publish",
                       json={"service_id": dep_id, "domain": "busy.example.com"}).status_code == 400


def test_enqueue_publish_picker_creates_dns_request(auth_client):
    client, Session = auth_client
    dep_id = _seed_deployment(Session)
    # Зона не подключена → 400.
    assert client.post("/api/pending-actions/publish",
                       json={"service_id": dep_id, "domain": "app.example.com",
                             "zone": "example.com", "subdomain": "app"}).status_code == 400
    client.post("/api/integrations/dns", json={"zones": ["example.com"]})
    r = client.post("/api/pending-actions/publish",
                    json={"service_id": dep_id, "domain": "app.example.com",
                          "zone": "example.com", "subdomain": "app"})
    assert r.status_code == 201
    # Заявка на A-запись создана синхронно (её исполнит реконсайлер ЛК).
    reqs = client.get("/api/dns/requests").json()
    assert any(x["fqdn"] == "app.example.com" for x in reqs)


def test_enqueue_issue_ssl_and_panel_ssl(auth_client):
    client, _ = auth_client
    assert client.post("/api/pending-actions/issue-ssl",
                       json={"domain": "a.example.com"}).status_code == 201
    assert client.post("/api/pending-actions/issue-ssl",
                       json={"domain": "a.example.com", "app_id": 999}).status_code == 404
    assert client.post("/api/pending-actions/panel-ssl",
                       json={"domain": "panel.example.com"}).status_code == 201


def test_retry_and_dismiss(auth_client):
    client, Session = auth_client
    dep_id = _seed_deployment(Session)
    aid = client.post("/api/pending-actions/publish",
                      json={"service_id": dep_id, "domain": "app.example.com",
                            "ssl_mode": "none"}).json()["id"]
    # Переводим в error напрямую, затем retry → снова pending.
    s = Session()
    a = s.get(models.PendingAction, aid)
    a.status = "error"
    a.result = "boom"
    s.commit()
    s.close()
    r = client.post(f"/api/pending-actions/{aid}/retry")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    # Dismiss удаляет.
    assert client.delete(f"/api/pending-actions/{aid}").status_code == 204
    assert client.post(f"/api/pending-actions/{aid}/retry").status_code == 404
