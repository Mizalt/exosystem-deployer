"""Ночь 16 — версии-UX (ADR-085): журнал версий ноды + страж несовместимого отката.

Инварианты:
  • финализация задачи `self_update` пишет журнал (from→to, статус) — сырьё модалки;
  • версия цели отката выводится из журнала (`previous_ref` ставит только успешный
    апдейт → его `from_version` и есть цель);
  • откат ниже `MIN_COMPATIBLE_VERSION` — 409 с объяснением (forward-only миграции);
  • цель неизвестна (обновляли мимо журнала) → откат разрешён (инструмент
    восстановления), с пометкой в UI.
"""
import json

from app import version as version_mod
from app.services import pending_actions, self_update


# --- as_tuple / журнал --------------------------------------------------------- #

def test_version_as_tuple_tolerant():
    assert version_mod.as_tuple("0.13.1") == (0, 13, 1)
    assert version_mod.as_tuple("v1.2") == (1, 2)
    assert version_mod.as_tuple("0.14.0-rc1") == (0, 14, 0)
    assert version_mod.as_tuple(None) == (0,)
    assert version_mod.as_tuple("мусор") == (0,)
    assert version_mod.as_tuple("0.10.9") < version_mod.as_tuple("0.11.0")


def test_history_roundtrip_and_retention():
    assert self_update.read_update_history() == []
    for i in range(self_update.HISTORY_MAX_ENTRIES + 5):
        self_update.append_update_history({"n": i, "status": "updated"})
    history = self_update.read_update_history()
    assert len(history) == self_update.HISTORY_MAX_ENTRIES
    assert history[-1]["n"] == self_update.HISTORY_MAX_ENTRIES + 4  # старые подрезаны


def test_rollback_target_version_from_journal(monkeypatch):
    monkeypatch.setattr(self_update, "read_update_state",
                        lambda: {"previous_ref": "abc", "current_ref": "def"})
    self_update.append_update_history(
        {"status": "updated", "from_version": "0.12.0", "to_version": "0.13.0"})
    self_update.append_update_history(
        {"status": "build_failed", "from_version": "0.13.0", "to_version": "0.13.0"})
    self_update.append_update_history(
        {"status": "updated", "from_version": "0.13.1", "to_version": "0.14.0"})
    # Цель отката = from_version ПОСЛЕДНЕГО успешного апдейта (он ставил previous_ref).
    assert self_update.rollback_target_version() == "0.13.1"


def test_rollback_target_none_without_journal(monkeypatch):
    monkeypatch.setattr(self_update, "read_update_state",
                        lambda: {"previous_ref": "abc"})
    assert self_update.rollback_target_version() is None


# --- Страж отката --------------------------------------------------------------- #

def test_guard_blocks_below_min_compatible(monkeypatch):
    monkeypatch.setattr(self_update, "read_update_state",
                        lambda: {"previous_ref": "abc"})
    monkeypatch.setattr(self_update, "rollback_target_version", lambda: "0.10.2")
    allowed, target, reason = self_update.rollback_guard()
    assert allowed is False and target == "0.10.2"
    assert version_mod.MIN_COMPATIBLE_VERSION in reason


def test_guard_allows_compatible_and_unknown(monkeypatch):
    monkeypatch.setattr(self_update, "read_update_state",
                        lambda: {"previous_ref": "abc"})
    monkeypatch.setattr(self_update, "rollback_target_version", lambda: "0.13.0")
    assert self_update.rollback_guard() == (True, "0.13.0", None)
    # Цель неизвестна → fail-open: откат — инструмент восстановления.
    monkeypatch.setattr(self_update, "rollback_target_version", lambda: None)
    assert self_update.rollback_guard() == (True, None, None)


def test_guard_requires_previous_ref(monkeypatch):
    monkeypatch.setattr(self_update, "read_update_state", lambda: {})
    allowed, target, reason = self_update.rollback_guard()
    assert allowed is False and target is None and "не обновлялась" in reason


# --- Журнал пишется финализацией задачи self_update ------------------------------ #

def _started_action(db, started_version="0.13.0"):
    from app import crud
    action = crud.create_pending_action(db, "self_update", "Обновление деплоера",
                                        json.dumps({"ref": None}))
    action.params = json.dumps({"ref": None, "started": True,
                                "started_version": started_version})
    action.status = "running"
    return action


def test_journal_written_on_success(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("exited", 0, "UPDATE_OK"))
    monkeypatch.setattr("app.services.self_update.cleanup_updater", lambda: None)
    monkeypatch.setattr("app.services.self_update.read_update_state",
                        lambda: {"current_ref": "deadbeefcafe", "status": "updated"})
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "done"
    history = self_update.read_update_history()
    assert len(history) == 1
    entry = history[0]
    assert entry["status"] == "updated"
    assert entry["from_version"] == "0.13.0"
    assert entry["to_version"] == version_mod.get_version()  # финализирует НОВЫЙ процесс
    assert entry["current_ref"] == "deadbeefcafe"


def test_journal_written_on_auto_rollback(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("exited", 42, "ROLLED_BACK"))
    monkeypatch.setattr("app.services.self_update.cleanup_updater", lambda: None)
    monkeypatch.setattr("app.services.self_update.read_update_state",
                        lambda: {"current_ref": "prev", "failed_ref": "bad",
                                 "status": "rolled_back"})
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "error"
    entry = self_update.read_update_history()[0]
    assert entry["status"] == "rolled_back" and entry["failed_ref"] == "bad"


def test_journal_skipped_when_already_up_to_date(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.updater_status",
                        lambda: ("exited", 0, "ALREADY_UP_TO_DATE"))
    monkeypatch.setattr("app.services.self_update.cleanup_updater", lambda: None)
    action = _started_action(db)
    pending_actions._handle_self_update(db, action)
    assert action.status == "done"
    assert self_update.read_update_history() == []  # ничего не менялось — не пишем


def test_handler_stores_started_version(db, monkeypatch):
    monkeypatch.setattr("app.services.self_update.launch_updater", lambda ref: None)
    from app import crud
    action = crud.create_pending_action(db, "self_update", "Обновление",
                                        json.dumps({"ref": None}))
    pending_actions._handle_self_update(db, action)
    assert json.loads(action.params)["started_version"] == version_mod.get_version()


# --- Роут отката со стражем + GET /api/admin/update-info ------------------------- #

def test_rollback_route_409_on_incompatible_target(api_env, monkeypatch):
    """Страж на самом деплоере: даже подписанный запрос отката отклоняется."""
    _, _, client = api_env
    # cpk-окружение: подпись валидна (мокаем проверку), guard решает сам.
    monkeypatch.setattr("app.routers.control_plane._verified_payload",
                        lambda token, typ: {"sub": "admin"})
    monkeypatch.setattr("app.services.self_update.rollback_guard",
                        lambda: (False, "0.9.0", "Откат на v0.9.0 запрещён: ниже "
                                 "минимально совместимой v0.11.0."))
    r = client.post("/api/admin/rollback", json={"token": "x"})
    assert r.status_code == 409
    assert "минимально совместимой" in r.json()["detail"]


def test_update_info_endpoint_shape(auth_client, monkeypatch):
    client, _Session = auth_client
    monkeypatch.setattr("app.services.self_update.read_update_state",
                        lambda: {"current_ref": "def", "previous_ref": "abc",
                                 "status": "updated"})
    self_update.append_update_history(
        {"status": "updated", "from_version": "0.13.1", "to_version": "0.14.0"})
    r = client.get("/api/admin/update-info")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == version_mod.get_version()
    assert body["min_compatible_version"] == version_mod.MIN_COMPATIBLE_VERSION
    assert body["history"][0]["to_version"] == "0.14.0"  # новые первыми
    assert body["rollback"]["available"] is True
    assert body["rollback"]["allowed"] is True
    assert body["rollback"]["target_version"] == "0.13.1"


def test_update_info_requires_auth(api_env):
    _app, _Session, client = api_env
    assert client.get("/api/admin/update-info").status_code == 401


def test_update_info_capability_declared():
    assert "update_info" in version_mod.capabilities()
