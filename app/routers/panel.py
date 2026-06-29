# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/routers/panel.py ---

import os
import socket
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, BackgroundTasks
from app import panel_config
from app.environment import get_docker_client
from app.services import nginx_manager
# --- ИМПОРТЫ ДЛЯ АУТЕНТИФИКАЦИИ ---
from app import security, models
from typing import Annotated
CurrentUser = Annotated[models.User, Depends(security.get_current_user)]

router = APIRouter(prefix="/api/panel/settings", tags=["Panel Settings"])

# Файлы docker-compose, которые нужно удалить при закрытии первичного доступа.
_OVERRIDE_FILES = [
    Path("/app/docker-compose.override.yml"),    # provisioned servers (cloud-init)
    Path("/app/docker-compose.firstrun.yml"),      # manual install (install.sh)
]


@router.get("", response_model=panel_config.PanelSettings)
def get_panel_settings(current_user: CurrentUser):
    return panel_config.load_settings()


@router.post("", response_model=panel_config.PanelSettings)
def update_panel_settings(
        settings: panel_config.PanelSettings,
        background_tasks: BackgroundTasks,
        current_user: CurrentUser
):
    panel_config.save_settings(settings)
    nginx_manager.update_panel_nginx_config(domain=settings.domain, ssl_cert_name=settings.ssl_cert_name)
    background_tasks.add_task(nginx_manager.reload_nginx)
    # Если переводим панель на HTTPS (и домен, и SSL-сертификат заданы) —
    # первичный доступ по IP:7999 больше не нужен, закрываем порт в фоне.
    if settings.domain and settings.ssl_cert_name:
        threading.Thread(target=_close_port_background, daemon=True).start()
    return settings


def _find_own_container_id() -> str | None:
    """Возвращает container ID текущего контейнера деплоера.

    В Docker-контейнере hostname = короткий container ID (12 hex). Проверяем через
    docker-py, что контейнер с таким ID существует и имеет имя 'deployer'.
    """
    try:
        short_id = socket.gethostname()
        client = get_docker_client()
        container = client.containers.get(short_id)
        if container and container.name == "deployer":
            return container.id
    except Exception:
        pass
    # fallback: поиск по имени
    try:
        client = get_docker_client()
        for c in client.containers.list():
            if c.name == "deployer":
                return c.id
    except Exception:
        pass
    return None


def _recreate_without_published_port(container_id: str) -> bool:
    """Пересоздаёт контейнер деплоера БЕЗ published-порта 7999.

    Использует docker-py (через docker.sock) для получения полной конфигурации
    контейнера и создания нового с теми же параметрами, но без port binding'а
    7999. Возвращает True при успехе.

    Безопасность: если что-то пошло не так — старый контейнер НЕ удаляется.
    """
    client = get_docker_client()
    try:
        container = client.containers.get(container_id)
        attrs = container.attrs
        config = attrs["Config"]
        host_config = attrs["HostConfig"]
        name = attrs["Name"].lstrip("/")
        image = config.get("Image") or config.get("image") or attrs.get("Image")

        if not image:
            print("ERROR: close-initial-access: не могу определить образ контейнера")
            return False

        # Собираем параметры для recreate
        create_kwargs: dict = {
            "image": image,
            "name": name,
            "hostname": config.get("Hostname"),
            "detach": True,
            "restart_policy": {
                "Name": host_config.get("RestartPolicy", {}).get("Name", "unless-stopped"),
                "MaximumRetryCount": host_config.get("RestartPolicy", {}).get("MaximumRetryCount", 0),
            },
        }

        # Команда и entrypoint
        cmd = config.get("Cmd")
        if cmd:
            create_kwargs["command"] = cmd
        entrypoint = config.get("Entrypoint")
        if entrypoint:
            create_kwargs["entrypoint"] = entrypoint

        # Переменные окружения
        env = config.get("Env")
        if env:
            create_kwargs["environment"] = env

        # Labels (нужны docker-compose для распознавания)
        labels = config.get("Labels")
        if labels:
            create_kwargs["labels"] = labels

        # Volumes / bind mounts
        volumes = host_config.get("Binds")
        if volumes:
            create_kwargs["volumes"] = volumes

        # Network mode
        network_mode = host_config.get("NetworkMode")
        if network_mode and network_mode != "default":
            create_kwargs["network"] = network_mode

        # Port bindings — ВСЕ кроме 7999
        old_bindings = host_config.get("PortBindings") or {}
        exposed_ports = dict(config.get("ExposedPorts") or {})
        new_bindings = {}
        new_exposed = {}
        for container_port, host_bindings in old_bindings.items():
            if container_port != "7999/tcp":
                new_bindings[container_port] = host_bindings
                if container_port in exposed_ports:
                    new_exposed[container_port] = exposed_ports[container_port]

        if new_bindings:
            create_kwargs["ports"] = new_bindings

        # Stop old container, remove it, create new one
        old_id = container.id
        container.stop(timeout=10)
        container.remove()

        new_container = client.containers.run(**create_kwargs)
        print(f"INFO: close-initial-access: контейнер пересоздан без порта 7999 "
              f"(old={old_id[:12]} new={new_container.id[:12]})")
        return True

    except Exception as e:
        print(f"ERROR: close-initial-access: не удалось пересоздать контейнер: {e}")
        # Попытка восстановить старый контейнер, если он был остановлен но не удалён
        try:
            old = client.containers.get(container_id)
            if old.status in ("exited", "stopped"):
                old.start()
                print("INFO: close-initial-access: старый контейнер восстановлен")
        except Exception:
            pass
        return False


def _close_port_background() -> None:
    """Фоновая задача: закрывает порт 7999 пересозданием контейнера."""
    time.sleep(2)  # Даём HTTP-ответу уйти

    # 1. Удаляем override-файлы (на host-файловой системе через bind mount).
    for path in _OVERRIDE_FILES:
        try:
            if path.exists():
                path.unlink()
                print(f"INFO: close-initial-access: удалён {path}")
        except Exception as e:
            print(f"WARN: close-initial-access: не удалось удалить {path}: {e}")

    # 2. Пересоздаём контейнер без порта 7999.
    cid = _find_own_container_id()
    if cid:
        _recreate_without_published_port(cid)
    else:
        print("WARN: close-initial-access: не удалось найти свой container ID")


@router.post("/close-initial-access")
def close_initial_access(current_user: CurrentUser):
    """Закрывает первичный доступ по IP:7999 — панель остаётся только по HTTPS.

    Удаляет docker-compose override-файлы (источник публикации порта 7999) и
    пересоздаёт контейнер деплоера без published-порта. Операция фоновая —
    endpoint возвращает 200 сразу, закрытие порта происходит через 2–10 секунд.
    Идемпотентен: повторный вызов на уже закрытом доступе безопасен (нет
    override-файлов → нечего удалять, порт 7999 не опубликован → нечего менять).
    """
    # Проверяем, есть ли что закрывать
    any_override = any(p.exists() for p in _OVERRIDE_FILES)
    published = False
    cid = _find_own_container_id()
    if cid:
        try:
            c = get_docker_client().containers.get(cid)
            bindings = c.attrs.get("HostConfig", {}).get("PortBindings") or {}
            published = "7999/tcp" in bindings
        except Exception:
            pass

    if not published and not any_override:
        return {"message": "Первичный доступ уже закрыт — порт 7999 не опубликован.",
                "action": "none"}

    threading.Thread(target=_close_port_background, daemon=True).start()
    return {"message": "Первичный доступ закрывается. Панель теперь только по HTTPS. "
                       "Порт 7999 будет закрыт через несколько секунд.",
            "action": "scheduled"}