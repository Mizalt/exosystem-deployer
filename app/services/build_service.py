"""Сборка и атомарный свап деплоя + потоковая (WS) пересборка (ADR-022/023).

Здесь единая логика «build-first + свап», переиспользуемая синхронными эндпоинтами
(redeploy/config) и потоковой пересборкой с живым WS-логом. Вынесено отдельным
модулем, чтобы не плодить циклический импорт с main.py.
"""
import asyncio
from pathlib import Path

from app import models, run_config
from app.database import SessionLocal
from app.environment import get_docker_client
from app.services import docker_manager
from app.services.ws_manager import manager


def drop_instances(dep, db):
    """Останавливает и удаляет контейнеры всех реплик деплоя (reconcile поднимет новые)."""
    client = get_docker_client()
    for inst in list(dep.instances):
        try:
            container = client.containers.get(inst.container_name)
            container.stop()
            container.remove(v=True)
        except Exception:
            pass
        db.delete(inst)


def build_first_swap(db, dep, target_artifact, on_line=None):
    """Build-first (ADR-022): собирает образ для (target_artifact + текущий конфиг
    деплоя) и ТОЛЬКО при успехе свапает деплой на эту версию и сносит старые реплики.

    При ошибке сборки поднимает RuntimeError (вызывающий делает db.rollback —
    работающий сервис не тронут). `on_line` — колбэк живого лога сборки (ADR-023).
    Не коммитит (это делает вызывающий).
    """
    zip_path = Path(target_artifact.stored_zip_path.replace("\\", "/"))
    build_config = {
        "base_image": dep.base_image,
        "run_command": dep.run_command,
        "internal_port": run_config.effective_port(dep.internal_port),
    }
    docker_manager.build_image_if_needed(
        zip_path, image_cache_key=target_artifact.zip_hash,
        build_config=build_config, on_line=on_line,
    )
    dep.artifact_id = target_artifact.id
    dep.build_attempts = 0
    dep.last_build_log = None
    drop_instances(dep, db)


def _streamed_redeploy_sync(deployment_id, artifact_id, task_id, loop):
    """В отдельном потоке: сборка нового образа со стримингом строк в WS + свап.
    Своя сессия БД. При провале — rollback и проброс ошибки наверх."""
    db = SessionLocal()
    try:
        dep = db.get(models.Deployment, deployment_id)
        artifact = db.get(models.Artifact, artifact_id)
        if not dep or not artifact:
            raise RuntimeError("Сервис или версия не найдены.")

        def on_line(line):
            asyncio.run_coroutine_threadsafe(
                manager.send_message(line.rstrip(), task_id), loop
            ).result()

        build_first_swap(db, dep, artifact, on_line=on_line)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def perform_streaming_redeploy(task_id: str, deployment_id: int, artifact_id: int):
    """Async-задача: ждёт подключения WS-клиента, затем в executor собирает образ
    новой версии со стримингом лога и атомарно свапает деплой (build-first). Зеркалит
    паттерн SSL-выпуска (ssl_service.perform_ssl_issuance)."""
    ready_event = manager.register_task(task_id)
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        manager.disconnect(task_id)
        return

    loop = asyncio.get_running_loop()
    try:
        await manager.send_message("=== Сборка образа новой версии ===", task_id)
        await loop.run_in_executor(None, _streamed_redeploy_sync, deployment_id, artifact_id, task_id, loop)
        await manager.send_message("\n=== ГОТОВО. Сервис пересоздаётся с новой версией. ===", task_id)
    except Exception as e:
        await manager.send_message(f"\n--- ОШИБКА СБОРКИ: {e}\nСервис НЕ тронут (откат). ---", task_id)
    finally:
        await asyncio.sleep(1)
        await manager.send_message("CLOSE_CONNECTION", task_id)
        manager.disconnect(task_id)
