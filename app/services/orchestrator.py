# --- app/services/orchestrator.py ---
import asyncio
import docker
from pathlib import Path
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app import models
from app import run_config
from app.environment import get_docker_client
from app.services import docker_manager

# Единый docker-клиент окружения (см. app/environment.py).
client = get_docker_client()

# Сколько раз даём контейнеру упасть/перезапуститься, прежде чем признать его
# нерабочим (аналог CrashLoopBackOff в Kubernetes). После этого мы останавливаем
# контейнер и помечаем Instance как 'failed', НЕ пересоздавая его на новом порту.
MAX_RESTARTS = 3

# Сколько раз подряд пробуем СОБРАТЬ образ, прежде чем перестать долбить сборку
# каждые 5 c (аналог CrashLoopBackOff, но для сборки). После лимита деплой считается
# build-failed и ждёт redeploy (смена версии сбрасывает счётчик). Без этого падающая
# сборка повторялась бесконечно и копила остановленные контейнеры неудачных шагов.
MAX_BUILD_ATTEMPTS = 3

# Статусы Docker, которые НЕ считаются здоровыми, но означают, что контейнер всё
# ещё существует (слот занят — новый экземпляр плодить не нужно).
UNHEALTHY_STATES = {"restarting", "created", "exited", "paused", "removing", "dead"}

# Как часто убирать неиспользуемые образы deployer-cache (ADR-025). Шаг цикла — 5 c,
# поэтому 720 циклов ≈ 1 час. Уборка запускается также на первом цикле (старт).
PRUNE_EVERY_CYCLES = 720


def collect_wanted_image_tags(db: Session) -> set:
    """Теги образов, нужных СЕЙЧАС: текущая версия+конфиг каждого деплоя.

    Совпадает с тем, что соберёт build (через docker_manager.compute_image_tag) —
    поэтому prune не тронет образы, которые реально используются.
    """
    tags = set()
    for dep in db.query(models.Deployment).all():
        art = dep.artifact
        if not art or not art.zip_hash:
            continue
        build_config = {
            "base_image": dep.base_image,
            "run_command": dep.run_command,
            "internal_port": run_config.effective_port(dep.internal_port),
        }
        tags.add(docker_manager.compute_image_tag(art.zip_hash, build_config))
    return tags


def prune_unused_images(db: Session) -> int:
    """Считает «нужные» теги и удаляет лишние deployer-cache-образы (ADR-025)."""
    return docker_manager.prune_deployer_images(collect_wanted_image_tags(db))


def get_available_port(db: Session, group_name: str):
    """Ищет свободный порт в диапазоне группы с учетом БД и Docker."""
    group = db.query(models.AppGroup).filter(models.AppGroup.name == group_name).first()
    if not group:
        return None

    used_ports = {i.assigned_port for i in db.query(models.Instance.assigned_port).all()}

    # Дополнительная проверка живых контейнеров на хосте во избежание коллизий
    try:
        for container in client.containers.list():
            ports_data = container.attrs.get('HostConfig', {}).get('PortBindings')
            if ports_data:
                for port_map in ports_data.values():
                    if port_map:
                        for item in port_map:
                            if item.get('HostPort'):
                                used_ports.add(int(item['HostPort']))
    except Exception as e:
        print(f"[ORCHESTRATOR] Warning: Could not get docker ports: {e}")

    for port in range(group.start_port, group.end_port + 1):
        if port not in used_ports:
            return port
    return None


def reconcile(db: Session):
    """
    Основная функция цикла согласования.
    Сравнивает Желаемое (Deployments) и Действительное (Instances + Docker).
    """
    deployments = db.query(models.Deployment).all()

    for deployment in deployments:
        current_instances = db.query(models.Instance).filter(models.Instance.deployment_id == deployment.id).all()
        alive_instances = []      # контейнер реально 'running'
        existing_instances = []   # контейнер существует (alive + восстанавливающиеся + failed)

        for instance in current_instances:
            try:
                container = client.containers.get(instance.container_name)
            except docker.errors.NotFound:
                # Контейнера действительно нет (удалён извне) — освобождаем слот и порт.
                print(f"[ORCHESTRATOR] Instance {instance.container_name}: container gone, releasing slot.")
                db.delete(instance)
                db.commit()
                continue

            existing_instances.append(instance)

            if container.status == 'running':
                # Health-gate: контейнер 'running' ещё НЕ значит, что приложение
                # отвечает. Помечаем 'online' (и считаем живой репликой для
                # балансировки) только когда порт реально открыт. Иначе держим
                # 'starting' — proxy не шлёт трафик в неготовую реплику.
                # internal_port=0 → воркер без сетевого порта (бот/очередь): health-gate
                # неприменим, считаем online как только контейнер 'running' (Идея 2а).
                gate_port = run_config.effective_port(deployment.internal_port)
                if gate_port == 0 or docker_manager.is_app_responding(instance.container_name, gate_port):
                    alive_instances.append(instance)
                    if instance.status != 'online' or (instance.restart_count or 0) != 0:
                        instance.status = 'online'
                        instance.restart_count = 0
                        db.commit()
                else:
                    # Запущен, но порт ещё не отвечает (стартует или завис на старте).
                    # Слот занят (existing_instances), новую реплику не плодим.
                    if instance.status != 'starting':
                        instance.status = 'starting'
                        db.commit()
                continue

            if container.status in UNHEALTHY_STATES:
                # Уже признан нерабочим — держим слот занятым и больше не трогаем.
                if instance.status == 'failed':
                    continue

                # Контейнер существует, но не работает (падает/перезапускается).
                # НЕ удаляем и НЕ пересоздаём на новом порту — это и вызывало лавину.
                instance.restart_count = (instance.restart_count or 0) + 1

                if instance.restart_count >= MAX_RESTARTS:
                    # CrashLoopBackOff: прекращаем бесконечные рестарты.
                    if instance.status != 'failed':
                        print(f"[ORCHESTRATOR] Instance {instance.container_name}: "
                              f"CrashLoopBackOff after {instance.restart_count} attempts. Marking as FAILED.")
                        # Снимаем диагностику ДО остановки: код выхода и логи краша
                        # (сохраняем в БД — переживут даже удаление контейнера).
                        try:
                            container.reload()
                            instance.exit_code = container.attrs.get('State', {}).get('ExitCode')
                            instance.last_logs = docker_manager.get_container_logs(container, tail=200)
                        except Exception as e:
                            print(f"[ORCHESTRATOR] Warn: could not capture crash diagnostics: {e}")
                        try:
                            container.stop()  # глушим restart_policy=unless-stopped
                        except Exception as e:
                            print(f"[ORCHESTRATOR] Warn: could not stop failed container: {e}")
                    instance.status = 'failed'
                else:
                    instance.status = 'restarting'
                db.commit()
                # Слот занят (existing) — новый экземпляр на этой итерации не создаём.

        actual_replicas = len(alive_instances)
        managed_replicas = len(existing_instances)  # сколько слотов реально занято
        target_replicas = deployment.target_replicas

        # SCALE UP — только если занятых слотов меньше цели.
        # (failed/restarting контейнеры занимают слот, поэтому каскада больше нет.)
        if managed_replicas < target_replicas:
            # Build backoff: если сборка устойчиво падает — не пытаемся снова каждые
            # 5 c (иначе флуд контейнеров упавшего шага). Ждём redeploy (сброс счётчика).
            if (deployment.build_attempts or 0) >= MAX_BUILD_ATTEMPTS:
                continue

            diff = target_replicas - managed_replicas
            print(f"[ORCHESTRATOR] Deployment {deployment.blueprint.name}: Scaling UP by {diff} instances.")

            for _ in range(diff):
                port = get_available_port(db, deployment.group_name)
                if not port:
                    print(f"[ORCHESTRATOR] ERROR: No free ports in group {deployment.group_name}")
                    break

                instance_name = f"dep_{deployment.blueprint.name}_{deployment.artifact.version_tag}_{port}"

                # Нормализуем разделитель пути: артефакт мог быть загружен на
                # Windows (uploads\hash.zip), а исполняемся в Linux-контейнере.
                zip_path = Path(deployment.artifact.stored_zip_path.replace("\\", "/"))

                # Изолируем сборку/запуск: ошибка одного деплоя не должна
                # ронять весь цикл reconcile (иначе остальные деплои не обслужатся).
                # Параметры расширенного режима (Идея 2а): база/команда/порт для сборки
                # + env-переменные рантайма. Пусто → питоновский автоген на порту 80.
                build_config = {
                    "base_image": deployment.base_image,
                    "run_command": deployment.run_command,
                    "internal_port": run_config.effective_port(deployment.internal_port),
                }
                env_vars = run_config.env_from_json(deployment.env_vars)

                try:
                    # ВАЖНО: храним РЕАЛЬНОЕ имя контейнера (с префиксом 'deployer-'),
                    # которое возвращает deploy_service. Иначе reconcile не найдёт
                    # контейнер по имени и будет бесконечно пересоздавать его.
                    container_id, real_container_name = docker_manager.deploy_service(
                        zip_path, instance_name, port,
                        image_cache_key=deployment.artifact.zip_hash,
                        build_config=build_config,
                        env_vars=env_vars,
                    )
                except Exception as e:
                    # Сохраняем причину (обычно лог сборки) на Deployment — UI покажет,
                    # почему сервис «не запускается», даже когда контейнера нет.
                    # Считаем неудачи: после MAX_BUILD_ATTEMPTS перестаём пытаться (backoff).
                    print(f"[ORCHESTRATOR] ERROR deploying {instance_name}: {e}")
                    deployment.last_build_log = str(e)[:8000]
                    deployment.build_attempts = (deployment.build_attempts or 0) + 1
                    db.commit()
                    break  # не долбим сборку в этом цикле; backoff остановит повторы

                if container_id:
                    new_instance = models.Instance(
                        deployment_id=deployment.id,
                        container_id=container_id,
                        container_name=real_container_name,
                        assigned_port=port,
                        status="starting"
                    )
                    db.add(new_instance)
                    # Сборка удалась — стираем прошлый лог и счётчик неудачных сборок.
                    if deployment.last_build_log or (deployment.build_attempts or 0):
                        deployment.last_build_log = None
                        deployment.build_attempts = 0
                    db.commit()
                    # reload nginx при scale НЕ нужен: в сетевой модели deployer-net
                    # (ADR-005) nginx-конфиг приложения указывает на деплоер
                    # (/api/proxy/<app>), а выбор живой реплики делает proxy.py —
                    # конфиг nginx от числа реплик не зависит.

        # SCALE DOWN
        elif actual_replicas > target_replicas:
            diff = actual_replicas - target_replicas
            print(f"[ORCHESTRATOR] Deployment {deployment.blueprint.name}: Scaling DOWN by {diff} instances.")

            for instance in alive_instances[-diff:]:
                docker_manager.remove_service_container(instance.container_name)
                db.delete(instance)
                db.commit()


def _prune_unused_images_safe(db: Session) -> None:
    """Уборка образов, изолированная от цикла (её сбой не должен ронять reconcile)."""
    try:
        prune_unused_images(db)
    except Exception as e:
        print(f"[ORCHESTRATOR] image prune failed: {e}")


async def run_orchestrator_loop():
    """Асинхронная обертка для фонового запуска в FastAPI"""
    print("[ORCHESTRATOR] Loop started...")
    cycle = 0
    while True:
        db = SessionLocal()
        try:
            await asyncio.to_thread(reconcile, db)
            # Периодическая уборка неиспользуемых образов (и на старте, cycle=0).
            if cycle % PRUNE_EVERY_CYCLES == 0:
                await asyncio.to_thread(_prune_unused_images_safe, db)
        except Exception as e:
            print(f"[ORCHESTRATOR] Loop Error: {e}")
        finally:
            db.close()

        cycle += 1
        await asyncio.sleep(5)