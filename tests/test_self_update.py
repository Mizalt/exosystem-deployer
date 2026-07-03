"""Самообновление деплоера (Ночь 11, ADR-071): cpk-роуты update/rollback +
стейт-машина задачи `self_update` + update_state + /api/version.

Docker и updater-джоба — на моках. Токены подписываются ЛОКАЛЬНЫМ Ed25519-парой
(cryptography напрямую, без app.cloud) — файл остаётся публично-срезаемым.
"""
import base64
import json
import secrets
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import crud, version
from app.services import pending_actions


# --- Локальный подписант (совместим по формату с app/services/control_plane.py) ---

def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, base64.b64encode(pub).decode()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign(priv, typ: str) -> str:
    payload = json.dumps({"typ": typ, "sub": "admin", "jti": secrets.token_urlsafe(8),
                          "exp": time.time() + 60}).encode()
    return f"{_b64u(payload)}.{_b64u(priv.sign(payload))}"


@pytest.fixture
def cpk_env(monkeypatch):
    priv, pub_b64 = _keypair()
    monkeypatch.setenv("DEPLOYER_CONTROL_PLANE_KEY", pub_b64)
    return priv


# --- Роуты /api/admin/{update,rollback} ---

def test_capability_declared():
    assert version.supports("self_update")


def test_update_404_without_cpk(api_env, monkeypatch):
    _, _, client = api_env
    monkeypatch.delenv("DEPLOYER_CONTROL_PLANE_KEY", raising=False)
    assert client.post("/api/admin/update", json={"token": "x"}).status_code == 404
    assert client.post("/api/admin/rollback", json={"token": "x"}).status_code == 404


def test_update_enqueues_task(api_env, cpk_env, monkeypatch):
    _, Session, client = api_env
    monkeypatch.setattr("app.services.self_update.precheck", lambda: None)
    r = client.post("/api/admin/update", json={"token": _sign(cpk_env, "update"),
                                               "ref": "v0.12.0"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    s = Session()
    action = crud.get_pending_action(s, task_id)
    assert action.type == "self_update"
    assert json.loads(action.params)["ref"] == "v0.12.0"
    assert "v0.12.0" in (action.title or "")
    s.close()


def test_update_conflict_when_already_running(api_env, cpk_env, monkeypatch):
    _, _, client = api_env
    monkeypatch.setattr("app.services.self_update.precheck", lambda: None)
    assert client.post("/api/admin/update",
                       json={"token": _sign(cpk_env, "update")}).status_code == 200
    r = client.post("/api/admin/update", json={"token": _sign(cpk_env, "update")})
    assert r.status_code == 409


def test_update_precheck_fails_400(api_env, cpk_env, monkeypatch):
    _, _, client = api_env
    monkeypatch.setattr("app.services.self_update.precheck",
                        lambda: "нет контейнерной установки")
    r = client.post("/api/admin/update", json={"token": _sign(cpk_env, "update")})
    assert r.status_code == 400
    assert "контейнерной" in r.json()["detail"]


def test_update_wrong_typ_401(api_env, cpk_env):
    _, _, client = api_env
    r = client.post("/api/admin/update", json={"token": _sign(cpk_env, "sso")})
    assert r.status_code == 401


def test_rollback_409_without_history(api_env, cpk_env, monkeypatch):
    _, _, client = api_env
    monkeypatch.setattr("app.services.self_update.read_update_state", lambda: {})
    r = client.post("/api/admin/rollback", json={"token": _sign(cpk_env, "rollback")})
    assert r.status_code == 409


def test_rollback_enqueues_previous_ref(api_env, cpk_env, monkeypatch):
    _, Session, client = api_env
    monkeypatch.setattr("app.services.self_update.read_update_state",
                        lambda: {"previous_ref": "abc1234def", "status": "updated"})
    monkeypatch.setattr("app.services.self_update.precheck", lambda: None)
    r = client.post("/api/admin/rollback", json={"token": _sign(cpk_env, "rollback")})
    assert r.status_code == 200, r.text
    s = Session()
    action = crud.get_pending_action(s, r.json()["task_id"])
    assert json.loads(action.params)["ref"] == "abc1234def"
    assert "Откат" in action.title
    s.close()


# --- Стейт-машина задачи self_update (updater — на моках) ---

def _make_action(db, ref=None):
    return crud.create_pending_action(db, "self_update", "Обновление деплоера",
                                      json.dumps({"ref": ref}))


def test_handler_starts_updater(db, monkeypatch):
    launched = []
    monkeypatch.setattr("app.services.self_update.launch_updater",
                        lambda ref: launched.append(ref))
    action = _make_action(db, ref="v1")
    pending_actions._handle_self_update(db, action)
    assert launched == ["v1"]
    assert action.status == "running"
    assert json.loads(action.params)["started"] is True
    assert action.next_check_at is not None


def test_handler_launch_error_fails(db, monkeypatch):
    from app.services.self_update import SelfUpdateError
    def boom(ref):
        raise SelfUpdateError("Обновление уже выполняется.")
    monkeypatch.setattr("app.services.self_update.launch_updater", boom)
    action = _make_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "error"
    assert "уже выполняется" in action.result


def _started_action(db):
    action = _make_action(db)
    action.params = json.dumps({"ref": None, "started": True})
    action.status = "running"
    return action


def test_handler_running_reschedules_then_times_out(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("running", None, ""))
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "running" and action.attempts == 1
    action.attempts = 91  # потолок ожидания updater'а
    pending_actions._handle_self_update(db, action)
    assert action.status == "error"
    assert "не завершился" in action.result


def test_handler_success_records_new_ref(db, monkeypatch):
    cleaned = []
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("exited", 0, "NEW_REF=deadbeef\nUPDATE_OK"))
    monkeypatch.setattr("app.services.self_update.cleanup_updater",
                        lambda: cleaned.append(True))
    monkeypatch.setattr("app.services.self_update.read_update_state",
                        lambda: {"current_ref": "deadbeefcafe", "status": "updated"})
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "done"
    assert "deadbeefcafe"[:12] in action.result
    assert cleaned == [True]
    assert "UPDATE_OK" in action.log


def test_handler_already_up_to_date(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("exited", 0, "PREV_REF=x\nALREADY_UP_TO_DATE"))
    monkeypatch.setattr("app.services.self_update.cleanup_updater", lambda: None)
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "done"
    assert "актуальная" in action.result


def test_handler_build_failed_keeps_running_version(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("exited", 3, "BUILD_FAILED"))
    monkeypatch.setattr("app.services.self_update.cleanup_updater", lambda: None)
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "error"
    assert "не тронута" in action.result  # build-first: рабочая версия жива


def test_handler_health_failure_rolled_back(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("exited", 42, "HEALTH_FAILED\nROLLED_BACK"))
    monkeypatch.setattr("app.services.self_update.cleanup_updater", lambda: None)
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "error"
    assert "откат" in action.result


def test_handler_missing_updater_container(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("missing", None, ""))
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "error"


# --- update_state / host_install_dir / обогащение /api/version ---

def test_read_update_state(monkeypatch, tmp_path):
    from app.services import self_update
    f = tmp_path / "update_state.json"
    monkeypatch.setattr(self_update, "UPDATE_STATE_FILE", f)
    assert self_update.read_update_state() == {}   # файла нет
    f.write_text('{"current_ref": "abc", "status": "updated"}', encoding="utf-8")
    assert self_update.read_update_state()["current_ref"] == "abc"
    f.write_text("мусор не-json", encoding="utf-8")
    assert self_update.read_update_state() == {}   # терпимо к мусору


def test_host_install_dir_from_data_mount(monkeypatch):
    from app.services import self_update

    class FakeContainer:
        attrs = {"Mounts": [
            {"Destination": "/app/uploads", "Source": "/opt/exo/uploads"},
            {"Destination": "/app/data", "Source": "/opt/exo/data"},
        ]}

    class FakeClient:
        class containers:
            @staticmethod
            def get(name):
                return FakeContainer()

    assert self_update.host_install_dir(FakeClient()) == "/opt/exo"


def test_host_install_dir_missing_mount(monkeypatch):
    from app.services import self_update

    class FakeContainer:
        attrs = {"Mounts": []}

    class FakeClient:
        class containers:
            @staticmethod
            def get(name):
                return FakeContainer()

    with pytest.raises(self_update.SelfUpdateError):
        self_update.host_install_dir(FakeClient())


def test_version_endpoint_includes_update_state(api_env, monkeypatch):
    _, _, client = api_env
    monkeypatch.setattr("app.services.self_update.read_update_state",
                        lambda: {"current_ref": "cafebabe", "previous_ref": "oldref",
                                 "status": "updated", "updated_at": "2026-07-03T00:00:00Z"})
    body = client.get("/api/version").json()
    assert body["update"]["previous_ref"] == "oldref"
    assert body["git_sha"] == "cafebabe"  # fallback из update_state
