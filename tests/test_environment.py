"""Тесты слоя абстракции окружения (app/environment.py).

Проверяют платформо-зависимый резолвинг docker-сокета и параметры ACME без
обращения к реальному Docker/сети.
"""

from app import environment


# --------------------------------------------------------------------------- #
#  Docker-сокет
# --------------------------------------------------------------------------- #
def test_socket_default_linux(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert environment.docker_socket_url(system="Linux") == environment.UNIX_DOCKER_SOCKET


def test_socket_default_darwin_uses_unix(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert environment.docker_socket_url(system="Darwin") == environment.UNIX_DOCKER_SOCKET


def test_socket_default_windows(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert environment.docker_socket_url(system="Windows") == environment.WINDOWS_DOCKER_SOCKET


def test_docker_host_env_overrides_platform(monkeypatch):
    # DOCKER_HOST имеет приоритет над платформенным дефолтом для ЛЮБОЙ платформы.
    monkeypatch.setenv("DOCKER_HOST", "tcp://1.2.3.4:2375")
    assert environment.docker_socket_url(system="Linux") == "tcp://1.2.3.4:2375"
    assert environment.docker_socket_url(system="Windows") == "tcp://1.2.3.4:2375"


# --------------------------------------------------------------------------- #
#  ACME
# --------------------------------------------------------------------------- #
def test_acme_email_from_env(monkeypatch):
    monkeypatch.setenv("DEPLOYER_ACME_EMAIL", "  ops@example.com  ")
    assert environment.acme_email() == "ops@example.com"  # с обрезкой пробелов


def test_acme_email_args_with_email(monkeypatch):
    monkeypatch.setenv("DEPLOYER_ACME_EMAIL", "ops@example.com")
    assert environment.acme_email_args() == ["--email", "ops@example.com"]


def test_acme_email_args_without_email(monkeypatch):
    monkeypatch.delenv("DEPLOYER_ACME_EMAIL", raising=False)
    assert environment.acme_email_args() == ["--register-unsafely-without-email"]


def test_acme_email_not_hardcoded(monkeypatch):
    # Анти-регресс: без env контактный email пустой — в коде нет хардкода реального
    # адреса (раньше был зашит реальный email, см. ADR-006).
    monkeypatch.delenv("DEPLOYER_ACME_EMAIL", raising=False)
    assert environment.acme_email() == ""


# --------------------------------------------------------------------------- #
#  Клиент
# --------------------------------------------------------------------------- #
def test_get_docker_client_is_cached(monkeypatch):
    # Сбрасываем кэш и убеждаемся, что повторный вызов отдаёт тот же объект
    # (создание клиента не открывает соединение — демон не нужен).
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(environment, "_docker_client", None)
    first = environment.get_docker_client()
    second = environment.get_docker_client()
    assert first is second
