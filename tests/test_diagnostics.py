"""Диагностируемость сбоя сервиса (запрос: «в ошибочных сценариях понятно, что происходит»).

Классифицируем ТРИ разных причины, которые UI раньше смешивал/не показывал:
  1) приложение вышло само (exit 0 — не остался сервисом) / упало (exit≠0);
  2) хост убил по памяти (OOM);
  3) logging-драйвер хоста не поддерживает чтение логов (проблема сервера, не кода).
"""
from app.services import docker_manager
from main import _diagnose_service


class _FakeContainer:
    def __init__(self, state=None, log_type="json-file", logs_exc=None, logs=b""):
        self.attrs = {"State": state or {},
                      "HostConfig": {"LogConfig": {"Type": log_type}}}
        self._logs_exc = logs_exc
        self._logs = logs

    def reload(self):
        pass

    def logs(self, tail=100):
        if self._logs_exc:
            raise self._logs_exc
        return self._logs


# --- 1. logging-драйвер хоста без чтения → понятное объяснение, не сырой 500 --------

def test_log_driver_unsupported_detected():
    exc = Exception("500 Server Error: configured logging driver does not support reading")
    assert docker_manager.log_driver_unsupported(exc) is True
    assert docker_manager.log_driver_unsupported(Exception("boom")) is False


def test_get_container_logs_explains_driver_issue():
    exc = Exception("configured logging driver does not support reading")
    out = docker_manager.get_container_logs(_FakeContainer(logs_exc=exc))
    assert "logging-драйвер" in out and "json-file" in out
    assert "docs/21_HOST_OPS.md" in out


def test_get_container_logs_generic_error_is_readable():
    out = docker_manager.get_container_logs(_FakeContainer(logs_exc=Exception("nope")))
    assert out.startswith("Не удалось получить логи")


def test_get_container_logs_ok():
    out = docker_manager.get_container_logs(_FakeContainer(logs=b"hello\nworld"))
    assert out == "hello\nworld"


# --- 2. get_container_diagnostics: разбор State/HostConfig -------------------------

def test_diagnostics_reads_oom_and_driver():
    c = _FakeContainer(state={"ExitCode": 137, "OOMKilled": True, "Error": ""},
                       log_type="json-file")
    d = docker_manager.get_container_diagnostics(c)
    assert d["exit_code"] == 137 and d["oom_killed"] is True
    assert d["logs_readable"] is True and d["log_driver"] == "json-file"


def test_diagnostics_flags_unreadable_driver():
    d = docker_manager.get_container_diagnostics(_FakeContainer(log_type="syslog"))
    assert d["logs_readable"] is False and d["log_driver"] == "syslog"


# --- 3. _diagnose_service: одна человекочитаемая строка «что случилось» ------------

def test_diagnose_exit_zero_not_a_service():
    msg = _diagnose_service({"status": "failed", "exit_code": 0})
    assert "СРАЗУ завершился с кодом 0" in msg
    assert "не деплоера" in msg  # явно снимаем вину с деплоера


def test_diagnose_oom_wins_over_exit_code():
    msg = _diagnose_service({"status": "failed", "exit_code": 137, "oom_killed": True})
    assert "OOM" in msg or "памяти" in msg


def test_diagnose_nonzero_exit_is_app_crash():
    msg = _diagnose_service({"status": "failed", "exit_code": 1})
    assert "кодом 1" in msg


def test_diagnose_build_failed():
    msg = _diagnose_service({"status": "build_failed", "exit_code": None})
    assert "Образ не собрался" in msg


def test_diagnose_state_error():
    msg = _diagnose_service({"status": "failed", "exit_code": None,
                             "state_error": "no such file"})
    assert "no such file" in msg


def test_diagnose_healthy_none():
    assert _diagnose_service({"status": "online", "exit_code": 0}) is None
    # exit 0 у ЖИВОГО сервиса (online) не диагностируем как сбой.
