# --- app/environment.py ---
"""Слой абстракции окружения (Linux / Windows).

Изолирует платформо-зависимые точки продукта в ОДНОМ месте, чтобы остальной код
не «знал» про разницу между Debian-сервером и Windows-машиной разработчика:

- **Docker-сокет** — путь/URL и единая фабрика docker-клиента
  (`get_docker_client()`). Все модули должны брать клиента отсюда, а не звать
  `docker.from_env()` россыпью.
- **ACME-клиент** — параметры выпуска Let's Encrypt (контактный email и пути
  внутри контейнеров nginx/certbot). Email берётся из окружения, а не из
  хардкода.

Принцип — **Linux-first** (целевая платформа Debian 11, ADR-001): дефолты
рассчитаны на нативный Docker. Windows-паритет достигается переменными окружения
(`DOCKER_HOST`) и/или платформенными дефолтами ниже — без правок прикладного кода.

Модуль НЕ импортирует ничего из `app`, чтобы оставаться безопасным от циклов
импорта (его тянут низкоуровневые сервисы).
"""

import os
import platform

import docker

# --------------------------------------------------------------------------- #
#  Docker-сокет
# --------------------------------------------------------------------------- #
# Платформенные дефолты пути к Docker. Используются, только если не задан
# DOCKER_HOST в окружении (его docker-py читает сам и имеет приоритет).
UNIX_DOCKER_SOCKET = "unix:///var/run/docker.sock"
WINDOWS_DOCKER_SOCKET = "npipe:////./pipe/docker_engine"


def docker_socket_url(system: str | None = None) -> str:
    """URL Docker-сокета для текущего окружения.

    Приоритет: переменная окружения ``DOCKER_HOST`` → платформенный дефолт.
    Параметр ``system`` (значения как у ``platform.system()``) нужен для тестов;
    в проде не передаётся.
    """
    explicit = os.environ.get("DOCKER_HOST")
    if explicit:
        return explicit
    system = system or platform.system()
    if system == "Windows":
        return WINDOWS_DOCKER_SOCKET
    # Linux и Darwin используют unix-сокет.
    return UNIX_DOCKER_SOCKET


_docker_client = None


def get_docker_client() -> docker.DockerClient:
    """Единый кэшированный docker-клиент для всего приложения.

    Создание клиента НЕ открывает соединение (docker-py подключается лениво при
    первом API-вызове), поэтому импорт модулей, зовущих эту функцию, безопасен и
    без запущенного демона (важно для тестов).
    """
    global _docker_client
    if _docker_client is None:
        if os.environ.get("DOCKER_HOST"):
            # Доверяем полной конфигурации из окружения (DOCKER_HOST, TLS и т.п.).
            _docker_client = docker.from_env()
        else:
            _docker_client = docker.DockerClient(base_url=docker_socket_url())
    return _docker_client


# --------------------------------------------------------------------------- #
#  ACME (Let's Encrypt)
# --------------------------------------------------------------------------- #
# Пути ВНУТРИ контейнеров nginx/certbot (см. docker-compose.yml / nginx_service).
# Это не пути хоста — их монтирует compose, поэтому они одинаковы на всех ОС.
ACME_WEBROOT = "/var/www/acme_challenge"
ACME_CERTS_DIR = "/etc/letsencrypt"


def acme_email() -> str:
    """Контактный email для Let's Encrypt (уведомления об истечении).

    Берётся из ``DEPLOYER_ACME_EMAIL``. Пусто — значит регистрируемся без email
    (см. :func:`acme_email_args`). Хардкодить реальный домен в коде нельзя —
    публичный репозиторий (см. docs/06_AGENT_SECURITY.md).
    """
    return os.environ.get("DEPLOYER_ACME_EMAIL", "").strip()


def acme_email_args() -> list[str]:
    """Аргументы certbot для контактного email.

    Если email задан — ``--email <addr>``; иначе явное
    ``--register-unsafely-without-email`` (иначе certbot спросит интерактивно и
    зависнет в неинтерактивном режиме).
    """
    email = acme_email()
    if email:
        return ["--email", email]
    return ["--register-unsafely-without-email"]


def describe() -> str:
    """Однострочное описание окружения для стартового лога."""
    return (
        f"platform={platform.system()} "
        f"docker_socket={docker_socket_url()} "
        f"acme_email={'set' if acme_email() else 'unset'}"
    )
