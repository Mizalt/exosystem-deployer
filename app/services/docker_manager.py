# --- app/services/docker_manager.py ---

import docker
import zipfile
import tempfile
from pathlib import Path

from app.environment import get_docker_client
from app.services.nginx_service import DEPLOYER_NETWORK, ensure_network

# Единый docker-клиент окружения (см. app/environment.py).
client = get_docker_client()


def generate_dockerfile(base_image="python:3.9-slim"):
    """Генерирует Dockerfile с гарантированной установкой uvicorn и fastapi."""
    return f"""
FROM {base_image}
WORKDIR /app
COPY . .
# Гарантируем установку uvicorn и fastapi, так как они используются в CMD по умолчанию
RUN pip install --no-cache-dir uvicorn fastapi
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
"""


def deploy_service(zip_path: Path, deployment_name: str, port: int):
    """
    Основная функция деплоя: строит образ и запускает контейнер.
    Возвращает (container_id, container_name) или (None, None) в случае ошибки.
    """
    image_tag = f"deployer/{deployment_name}:latest"
    container_name = f"deployer-{deployment_name}"

    with tempfile.TemporaryDirectory() as tmpdir:
        build_context = Path(tmpdir)

        # 1. Распаковываем архив
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(build_context)

        # 2. Создаем Dockerfile
        (build_context / "Dockerfile").write_text(generate_dockerfile())

        # 3. Билдим образ
        print(f"INFO: Building image {image_tag}...")
        try:
            image, build_logs = client.images.build(path=str(build_context), tag=image_tag, rm=True)
            for chunk in build_logs:
                if 'stream' in chunk:
                    print(chunk['stream'].strip())
        except docker.errors.BuildError as e:
            print(f"ERROR: Docker build failed: {e}")
            for chunk in e.build_log:
                if 'stream' in chunk:
                    print(chunk['stream'].strip())
            return None, None

        # 4. Останавливаем и удаляем старый контейнер с таким же именем, если он есть
        try:
            old_container = client.containers.get(container_name)
            print(f"INFO: Stopping and removing old container {container_name}...")
            old_container.stop()
            old_container.remove(v=True)  # v=True удаляет анонимные тома
        except docker.errors.NotFound:
            pass  # Контейнера не было, это нормально

        # 5. Запускаем новый контейнер в общей docker-сети.
        # Host-порт НЕ публикуем: деплоер и nginx обращаются к контейнеру по имени
        # на внутренний порт 80 внутри сети deployer-net. Это убирает исчерпание
        # host-портов и делает модель кроссплатформенной (Linux/Windows).
        ensure_network()
        print(f"INFO: Running new container {container_name} in network {DEPLOYER_NETWORK} (logical port {port})...")
        try:
            container = client.containers.run(
                image=image_tag,
                name=container_name,
                network=DEPLOYER_NETWORK,
                detach=True,
                restart_policy={"Name": "unless-stopped"},
                labels={"manager": "cloud-deployer"}  # Метка для легкого поиска
            )
            print(f"SUCCESS: Container {container.short_id} started.")
            return container.id, container_name
        except Exception as e:
            print(f"ERROR: Failed to run container: {e}")
            return None, None


def exec_in_container(container_name: str, cmd, user: str = "") -> tuple[int, str]:
    """Выполняет команду ВНУТРИ существующего системного контейнера через
    docker-py (без docker-cli в образе деплоера). Возвращает (exit_code, output).

    Заменяет прежние subprocess-вызовы docker-cli (nginx reload/test, генерация
    self-signed SSL в certbot-контейнере).
    """
    container = client.containers.get(container_name)
    result = container.exec_run(cmd, user=user)
    output = result.output.decode("utf-8", errors="replace") if result.output else ""
    return result.exit_code, output


def exec_stream_in_container(container_name: str, cmd, user: str = ""):
    """Генератор: построчно отдаёт объединённый stdout/stderr команды, выполняемой
    ВНУТРИ контейнера (docker-py low-level API — стрим + код возврата, без
    docker-cli). По завершении при ненулевом коде поднимает RuntimeError.

    Используется для стриминга вывода Certbot в WebSocket в реальном времени.
    """
    api = client.api
    exec_id = api.exec_create(container_name, cmd, user=user, tty=False)["Id"]
    pending = b""
    for chunk in api.exec_start(exec_id, stream=True):
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")
    if pending:
        yield pending.decode("utf-8", errors="replace")

    exit_code = api.exec_inspect(exec_id).get("ExitCode")
    if exit_code not in (0, None):
        raise RuntimeError(
            f"Команда {cmd} в контейнере '{container_name}' завершилась с кодом {exit_code}"
        )


def remove_service_container(container_name: str):
    """Останавливает и удаляет контейнер сервиса."""
    try:
        container = client.containers.get(container_name)
        print(f"INFO: Stopping container {container_name}...")
        container.stop()
        print(f"INFO: Removing container {container_name}...")
        container.remove(v=True)
        print(f"SUCCESS: Container {container_name} removed.")
        return True
    except docker.errors.NotFound:
        print(f"WARN: Container {container_name} not found, nothing to remove.")
        return True  # Считаем успехом, если его и так нет
    except Exception as e:
        print(f"ERROR: Failed to remove container {container_name}: {e}")
        return False


def cleanup_orphan_containers(known_container_names) -> int:
    """
    Удаляет контейнеры с меткой manager=cloud-deployer, которых нет среди
    known_container_names (т.е. не отслеживаемых в БД как Instance).

    Защищает от «сирот» — контейнеров, оставшихся от прошлых запусков/сбоев,
    которые иначе бесконечно крутятся с restart_policy=unless-stopped.
    Возвращает количество удалённых контейнеров.
    """
    known = set(known_container_names)
    removed = 0
    try:
        containers = client.containers.list(all=True, filters={"label": "manager=cloud-deployer"})
    except Exception as e:
        print(f"ERROR: Could not list managed containers for orphan cleanup: {e}")
        return 0

    for container in containers:
        if container.name in known:
            continue
        try:
            print(f"INFO: Removing orphan container {container.name} (status={container.status})...")
            container.remove(force=True)  # force останавливает и удаляет крутящийся контейнер
            removed += 1
        except Exception as e:
            print(f"ERROR: Failed to remove orphan container {container.name}: {e}")

    if removed:
        print(f"SUCCESS: Removed {removed} orphan container(s).")
    return removed


def get_running_deployment_containers():
    """Возвращает список ID контейнеров, управляемых нашим деплоером."""
    try:
        containers = client.containers.list(filters={"label": "manager=cloud-deployer"})
        return [c.id for c in containers]
    except Exception as e:
        print(f"ERROR: Could not get containers from Docker: {e}")
        return []


def get_container_by_name(container_name: str):
    """Находит контейнер по имени."""
    try:
        return client.containers.get(container_name)
    except docker.errors.NotFound:
        return None


def start_container(container):
    """Запускает контейнер."""
    container.start()


def stop_container(container):
    """Останавливает контейнер."""
    container.stop()


def restart_container(container):
    """Перезапускает контейнер."""
    container.restart()


def get_container_logs(container, tail=100) -> str:
    """Получает последние N строк логов контейнера."""
    try:
        logs = container.logs(tail=tail).decode('utf-8', errors='ignore')
        return logs
    except Exception as e:
        return f"Could not retrieve logs: {e}"


def get_container_stats(container) -> dict:
    """Получает текущую статистику использования ресурсов контейнера."""
    try:
        stats = container.stats(stream=False)
        cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
        system_cpu_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
        number_cpus = stats['cpu_stats'].get('online_cpus', len(stats['cpu_stats']['cpu_usage']['percpu_usage']))
        cpu_percent = (cpu_delta / system_cpu_delta) * number_cpus * 100.0 if system_cpu_delta > 0 else 0
        memory_usage_bytes = stats['memory_stats']['usage']
        memory_limit_bytes = stats['memory_stats']['limit']
        memory_usage_mb = round(memory_usage_bytes / (1024 * 1024), 2)
        memory_limit_mb = round(memory_limit_bytes / (1024 * 1024), 2)
        return {"cpu_percent": round(cpu_percent, 2), "memory_usage_mb": memory_usage_mb, "memory_limit_mb": memory_limit_mb}
    except Exception as e:
        print(f"ERROR: Could not get stats for container {container.name}: {e}")
        return {"cpu_percent": 0, "memory_usage_mb": 0, "memory_limit_mb": 0}