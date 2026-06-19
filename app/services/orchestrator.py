# --- app/services/orchestrator.py ---
import asyncio
import docker
from pathlib import Path
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app import models
from app.environment import get_docker_client
from app.services import docker_manager

# Единый docker-клиент окружения (см. app/environment.py).
client = get_docker_client()

# Сколько раз даём контейнеру упасть/перезапуститься, прежде чем признать его
# нерабочим (аналог CrashLoopBackOff в Kubernetes). После этого мы останавливаем
# контейнер и помечаем Instance как 'failed', НЕ пересоздавая его на новом порту.
MAX_RESTARTS = 3

# Статусы Docker, которые НЕ считаются здоровыми, но означают, что контейнер всё
# ещё существует (слот занят — новый экземпляр плодить не нужно).
UNHEALTHY_STATES = {"restarting", "created", "exited", "paused", "removing", "dead"}


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
                # Здоров: сбрасываем счётчик неудач, помечаем online.
                alive_instances.append(instance)
                if instance.status != 'online' or (instance.restart_count or 0) != 0:
                    instance.status = 'online'
                    instance.restart_count = 0
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
                try:
                    # ВАЖНО: храним РЕАЛЬНОЕ имя контейнера (с префиксом 'deployer-'),
                    # которое возвращает deploy_service. Иначе reconcile не найдёт
                    # контейнер по имени и будет бесконечно пересоздавать его.
                    container_id, real_container_name = docker_manager.deploy_service(zip_path, instance_name, port)
                except Exception as e:
                    print(f"[ORCHESTRATOR] ERROR deploying {instance_name}: {e}")
                    break  # не долбим сборку каждые 5 c; повторим на следующем цикле

                if container_id:
                    new_instance = models.Instance(
                        deployment_id=deployment.id,
                        container_id=container_id,
                        container_name=real_container_name,
                        assigned_port=port,
                        status="starting"
                    )
                    db.add(new_instance)
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


async def run_orchestrator_loop():
    """Асинхронная обертка для фонового запуска в FastAPI"""
    print("[ORCHESTRATOR] Loop started...")
    while True:
        db = SessionLocal()
        try:
            await asyncio.to_thread(reconcile, db)
        except Exception as e:
            print(f"[ORCHESTRATOR] Loop Error: {e}")
        finally:
            db.close()

        await asyncio.sleep(5)