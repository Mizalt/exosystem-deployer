"""Веб-терминал ноды (ADR-090): сервис `terminal.run_command` + эндпоинт
`POST /api/admin/exec` (панельный JWT и cpk-путь, выключатель, rate-limit, лимиты).

`run_command` тестируем с моком `subprocess.run` — кроссплатформенно (на Windows
нет /bin/sh). Роут тестируем с моком самого `run_command` (проверяем авторизацию/
гейты/лимиты, а не выполнение).
"""
import subprocess

import pytest

from app import models, security
from app.cloud.services import trust
from app.rate_limit import command_limiter
from app.services import control_plane, terminal


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Процессные синглтоны/env между тестами: rate-limit, anti-replay, выключатель."""
    command_limiter.clear()
    control_plane._used_jti.clear()
    monkeypatch.delenv("DEPLOYER_TERMINAL_ENABLED", raising=False)
    monkeypatch.delenv("DEPLOYER_TERMINAL_TIMEOUT", raising=False)
    yield
    command_limiter.clear()
    control_plane._used_jti.clear()


# --- Сервис run_command ------------------------------------------------------- #
class _FakeCompleted:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_run_command_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _FakeCompleted(b"hello\n", 0))
    res = terminal.run_command("echo hello")
    assert res["exit_code"] == 0 and res["output"].strip() == "hello"
    assert res["timed_out"] is False and res["truncated"] is False


def test_run_command_nonzero_exit(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _FakeCompleted(b"no such file\n", 2))
    res = terminal.run_command("ls /nope")
    assert res["exit_code"] == 2 and "no such file" in res["output"]


def test_run_command_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=1, output=b"partial")

    monkeypatch.setattr(subprocess, "run", boom)
    res = terminal.run_command("sleep 999")
    assert res["timed_out"] is True and res["exit_code"] is None
    assert "таймауту" in res["output"]


def test_run_command_output_is_clipped(monkeypatch):
    big = b"x" * (200 * 1024)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(big, 0))
    res = terminal.run_command("cat big")
    assert res["truncated"] is True
    assert "обрезан" in res["output"]
    # Вывод не превышает лимит + маленький маркер.
    assert len(res["output"].encode("utf-8")) <= terminal._OUTPUT_LIMIT + 200


def test_run_command_empty_is_safe():
    res = terminal.run_command("   ")
    assert res["exit_code"] is None and "Пустая" in res["output"]


def test_run_command_passes_single_argument(monkeypatch):
    """Никакой инъекции в промежуточные слои: команда идёт единым аргументом sh -c."""
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **k: seen.setdefault("argv", argv) or _FakeCompleted(b"", 0))
    terminal.run_command("df -h | grep /dev")
    assert seen["argv"] == ["/bin/sh", "-c", "df -h | grep /dev"]


def test_effective_timeout_env_and_ceiling(monkeypatch):
    monkeypatch.setenv("DEPLOYER_TERMINAL_TIMEOUT", "5")
    assert terminal.effective_timeout() == 5
    monkeypatch.setenv("DEPLOYER_TERMINAL_TIMEOUT", "99999")  # выше потолка
    assert terminal.effective_timeout() == terminal._MAX_TIMEOUT
    monkeypatch.setenv("DEPLOYER_TERMINAL_TIMEOUT", "мусор")
    assert terminal.effective_timeout() == terminal._DEFAULT_TIMEOUT


def test_terminal_enabled_flag(monkeypatch):
    assert terminal.terminal_enabled() is True  # дефолт — включён
    monkeypatch.setenv("DEPLOYER_TERMINAL_ENABLED", "false")
    assert terminal.terminal_enabled() is False
    monkeypatch.setenv("DEPLOYER_TERMINAL_ENABLED", "FALSE")
    assert terminal.terminal_enabled() is False
    monkeypatch.setenv("DEPLOYER_TERMINAL_ENABLED", "true")
    assert terminal.terminal_enabled() is True


# --- Эндпоинт /api/admin/exec ------------------------------------------------- #
@pytest.fixture
def _fake_exec(monkeypatch):
    """Не выполняем реальный shell: подменяем run_command детерминированным ответом."""
    calls = []

    def fake(command):
        calls.append(command)
        return {"command": command, "exit_code": 0, "output": f"ran: {command}",
                "truncated": False, "timed_out": False, "duration_ms": 3}

    monkeypatch.setattr(terminal, "run_command", fake)
    return calls


def _panel_token(client, Session):
    """Создаёт админа и возвращает панельный JWT."""
    s = Session()
    s.add(models.User(username="admin",
                      hashed_password=security.get_password_hash("pw")))
    s.commit()
    s.close()
    r = client.post("/api/auth/token", data={"username": "admin", "password": "pw"})
    return r.json()["access_token"]


def test_exec_requires_auth(api_env, _fake_exec):
    _, _, client = api_env
    r = client.post("/api/admin/exec", json={"command": "df -h"})
    assert r.status_code == 401


def test_exec_panel_jwt_ok_and_audited(api_env, _fake_exec, capsys):
    _, Session, client = api_env
    jwt = _panel_token(client, Session)
    r = client.post("/api/admin/exec", json={"command": "df -h"},
                    headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200
    assert r.json()["output"] == "ran: df -h"
    assert _fake_exec == ["df -h"]
    # Аудит ноды — строка в stdout процесса (у ядра нет БД-журнала).
    assert "AUDIT: terminal exec by panel:admin" in capsys.readouterr().out


def test_exec_disabled_by_flag(api_env, _fake_exec, monkeypatch):
    _, Session, client = api_env
    jwt = _panel_token(client, Session)
    monkeypatch.setenv("DEPLOYER_TERMINAL_ENABLED", "false")
    r = client.post("/api/admin/exec", json={"command": "df -h"},
                    headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 403 and "отключён" in r.json()["detail"]
    assert _fake_exec == []  # ничего не выполнено


def test_exec_cpk_path(api_env, _fake_exec, monkeypatch):
    """Машинный путь (ЛК/MCP): авторизация cpk-подписью, без панельного пароля."""
    _, _, client = api_env
    priv, pub = trust.generate_keypair()
    monkeypatch.setenv("DEPLOYER_CONTROL_PLANE_KEY", pub)
    token = trust.sign_token(priv, typ="exec", aud=1)
    r = client.post("/api/admin/exec", json={"command": "uptime", "token": token})
    assert r.status_code == 200 and r.json()["output"] == "ran: uptime"


def test_exec_cpk_wrong_typ_rejected(api_env, _fake_exec, monkeypatch):
    _, _, client = api_env
    priv, pub = trust.generate_keypair()
    monkeypatch.setenv("DEPLOYER_CONTROL_PLANE_KEY", pub)
    # sso-токен не годится для exec.
    token = trust.sign_token(priv, typ="sso", aud=1)
    r = client.post("/api/admin/exec", json={"command": "uptime", "token": token})
    assert r.status_code == 401
    assert _fake_exec == []


def test_exec_rate_limited(api_env, _fake_exec, monkeypatch):
    _, Session, client = api_env
    jwt = _panel_token(client, Session)
    headers = {"Authorization": f"Bearer {jwt}"}
    # Опускаем лимит до 3/мин, чтобы тест был быстрым и детерминированным.
    monkeypatch.setattr(command_limiter, "max_calls", 3)
    for _ in range(3):
        assert client.post("/api/admin/exec", json={"command": "date"},
                           headers=headers).status_code == 200
    r = client.post("/api/admin/exec", json={"command": "date"}, headers=headers)
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_exec_rejects_empty_command(api_env, _fake_exec):
    _, Session, client = api_env
    jwt = _panel_token(client, Session)
    r = client.post("/api/admin/exec", json={"command": ""},
                    headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 422  # Pydantic min_length
