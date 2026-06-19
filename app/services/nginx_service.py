# ---  app/services/nginx_service.py ---

import asyncio
from docker.errors import NotFound, APIError
from pathlib import Path

from app.environment import get_docker_client

# Имена наших системных контейнеров
NGINX_CONTAINER_NAME = "deployer-nginx-proxy"
CERTBOT_CONTAINER_NAME = "deployer-certbot-companion"

# Общая docker-сеть для всего стека (деплоер + nginx + certbot + app-контейнеры).
# Все компоненты в ней резолвятся друг к другу по имени контейнера — это заменяет
# host.docker.internal и host-порты (см. docs/05_DECISIONS.md, ADR сетевой модели).
DEPLOYER_NETWORK = "deployer-net"

# Единый docker-клиент окружения (путь к сокету резолвит app/environment.py).
client = get_docker_client()


def ensure_network():
    """Создаёт общую docker-сеть, если её ещё нет. Идемпотентно."""
    try:
        client.networks.get(DEPLOYER_NETWORK)
    except NotFound:
        print(f"INFO: Creating shared docker network '{DEPLOYER_NETWORK}'...")
        client.networks.create(DEPLOYER_NETWORK, driver="bridge")
    except APIError as e:
        print(f"ERROR: Could not ensure network '{DEPLOYER_NETWORK}': {e}")


async def run_sync_docker_call(func, *args, **kwargs):
    """Оборачивает синхронный вызов docker-py для асинхронного выполнения."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def _ensure_container_running(container_name: str, container_config: dict):
    """Универсальная функция для проверки, запуска и ожидания перехода контейнера в активный статус."""
    container = None
    try:
        container = await run_sync_docker_call(client.containers.get, container_name)
        if container.status == "running":
            print(f"INFO: Container '{container_name}' is already running.")
            return
        else:
            print(f"INFO: Found stopped container '{container_name}'. Starting it...")
            await run_sync_docker_call(container.start)

            # Ожидаем, пока статус действительно сменится на 'running'
            for _ in range(10):
                await asyncio.sleep(1)
                container = await run_sync_docker_call(client.containers.get, container_name)
                if container.status == "running":
                    print(f"INFO: Container '{container_name}' successfully transitioned to running.")
                    return
            print(f"WARN: Container '{container_name}' started, but status is currently: {container.status}")
            return
    except NotFound:
        print(f"INFO: Container '{container_name}' not found. Creating it...")
    except APIError as e:
        print(f"ERROR: APIError with container '{container_name}': {e}. Recreating...")
        if container:
            try:
                await run_sync_docker_call(container.remove, force=True)
            except Exception as rm_e:
                print(f"Error removing problematic container '{container_name}': {rm_e}")

    try:
        new_container = await run_sync_docker_call(client.containers.run, **container_config)
        # Ожидаем запуск нового контейнера
        for _ in range(10):
            await asyncio.sleep(1)
            new_container = await run_sync_docker_call(client.containers.get, container_name)
            if new_container.status == "running":
                print(f"SUCCESS: Container '{container_name}' created and is now running.")
                return
        print(f"WARN: Container '{container_name}' was created, but status is: {new_container.status}")
    except APIError as e:
        print(f"FATAL: Failed to create container '{container_name}': {e}")
        raise


async def ensure_infrastructure_running(
        nginx_configs_path: Path,
        ssl_certs_path: Path,
        acme_challenge_path: Path
):
    """
    Главная функция: Управляет запуском Nginx и Certbot контейнеров.
    Использует порты 80 и 443 на хосте для Nginx.
    """
    volumes = {
        str(nginx_configs_path.resolve()): {'bind': '/etc/nginx/conf.d', 'mode': 'rw'},
        str(ssl_certs_path.resolve()): {'bind': '/etc/letsencrypt', 'mode': 'rw'},
        str(acme_challenge_path.resolve()): {'bind': '/var/www/acme_challenge', 'mode': 'rw'},
    }

    ensure_network()

    nginx_config = {
        "image": "nginx:1.25-alpine",
        "name": NGINX_CONTAINER_NAME,
        "ports": {'80/tcp': 80, '443/tcp': 443},
        "volumes": volumes,
        "network": DEPLOYER_NETWORK,
        "extra_hosts": {
            "host.docker.internal": "host-gateway"
        },
        "restart_policy": {"Name": "unless-stopped"},
        "detach": True
    }

    certbot_config = {
        "image": "certbot/certbot:latest",
        "name": CERTBOT_CONTAINER_NAME,
        "volumes": volumes,
        "network": DEPLOYER_NETWORK,
        "entrypoint": "/bin/sh -c",
        "command": "'trap exit TERM; while :; do sleep 1; done'",
        "user": "root",
        "restart_policy": {"Name": "unless-stopped"},
        "detach": True
    }

    await _ensure_container_running(NGINX_CONTAINER_NAME, nginx_config)
    await _ensure_container_running(CERTBOT_CONTAINER_NAME, certbot_config)