"""Тесты ядра-оркестратора (reconcile).

Покрывают именно те сценарии, где раньше были критичные баги:
- лавина контейнеров на новых портах (BUG-2),
- CrashLoopBackOff,
- несоответствие имени контейнера,
- кросс-платформенный путь к артефакту.
"""
import pytest

from app.services import orchestrator
from app import models
from app.services.orchestrator import MAX_RESTARTS
from tests.conftest import FakeContainer


@pytest.fixture
def patched(monkeypatch, fake_docker):
    """Подменяет docker-клиент оркестратора и фиксирует вызовы deploy/remove."""
    monkeypatch.setattr(orchestrator, "client", fake_docker)

    calls = {"deploy": [], "remove": []}

    def fake_deploy(zip_path, instance_name, port, status="restarting"):
        cname = f"deployer-{instance_name}"
        fake_docker.containers.add(FakeContainer(cname, status="running"))
        calls["deploy"].append({"zip_path": zip_path, "name": instance_name, "port": port})
        return f"id_{cname}", cname

    def fake_remove(container_name):
        calls["remove"].append(container_name)
        fake_docker.containers._by_name.pop(container_name, None)
        return True

    monkeypatch.setattr(orchestrator.docker_manager, "deploy_service", fake_deploy)
    monkeypatch.setattr(orchestrator.docker_manager, "remove_service_container", fake_remove)
    return calls


def _instances(db, dep):
    return db.query(models.Instance).filter(models.Instance.deployment_id == dep.id).all()


# --------------------------------------------------------------------------- #
def test_scale_up_creates_single_instance(db, deployment, patched, fake_docker):
    orchestrator.reconcile(db)

    insts = _instances(db, deployment)
    assert len(insts) == 1
    assert len(patched["deploy"]) == 1
    # имя контейнера сохранено с реальным префиксом 'deployer-'
    assert insts[0].container_name == "deployer-dep_qwe_v1.0.0_9001"
    assert insts[0].assigned_port == 9001


def test_zip_path_normalized_to_posix(db, deployment, patched):
    # артефакт «загружен на Windows» — с обратными слешами
    deployment.artifact.stored_zip_path = "uploads\\hash123.zip"
    db.commit()

    orchestrator.reconcile(db)

    zip_path = patched["deploy"][0]["zip_path"]
    # as_posix() платформо-независим. На Linux без нормализации обратный слеш
    # остался бы частью имени файла ("uploads\\hash123.zip") и тест бы упал.
    assert zip_path.as_posix() == "uploads/hash123.zip"


def test_running_container_marked_online(db, deployment, patched, fake_docker):
    inst = models.Instance(
        deployment_id=deployment.id,
        container_name="deployer-dep_qwe_v1.0.0_9001",
        assigned_port=9001,
        status="starting",
        restart_count=2,
    )
    db.add(inst)
    db.commit()
    fake_docker.containers.add(FakeContainer(inst.container_name, status="running"))

    orchestrator.reconcile(db)
    db.refresh(inst)

    assert inst.status == "online"
    assert inst.restart_count == 0
    assert len(patched["deploy"]) == 0  # цель достигнута, ничего не создаём


def test_crashloop_marks_failed_without_cascade(db, deployment, patched, fake_docker):
    """Главный антирегресс: падающий контейнер НЕ плодит реплики на новых портах."""
    inst = models.Instance(
        deployment_id=deployment.id,
        container_name="deployer-dep_qwe_v1.0.0_9001",
        assigned_port=9001,
        status="starting",
        restart_count=0,
    )
    db.add(inst)
    db.commit()
    container = FakeContainer(inst.container_name, status="restarting")
    fake_docker.containers.add(container)

    # Прогоняем несколько циклов — контейнер всё время «restarting».
    for _ in range(MAX_RESTARTS + 2):
        container.status = "restarting"  # docker «поднимает» его снова
        orchestrator.reconcile(db)

    db.refresh(inst)
    insts = _instances(db, deployment)

    assert len(insts) == 1                     # никакой лавины
    assert inst.assigned_port == 9001          # порт не инкрементировался
    assert inst.status == "failed"             # CrashLoopBackOff
    assert inst.restart_count >= MAX_RESTARTS
    assert container.stopped is True           # restart_policy заглушён
    assert len(patched["deploy"]) == 0         # новые контейнеры не создавались


def test_notfound_container_releases_slot(db, deployment, patched, fake_docker):
    deployment.target_replicas = 0  # чтобы исключить немедленный scale up
    inst = models.Instance(
        deployment_id=deployment.id,
        container_name="deployer-dep_qwe_v1.0.0_9001",
        assigned_port=9001,
        status="online",
        restart_count=0,
    )
    db.add(inst)
    db.commit()
    # контейнер в fake_docker отсутствует -> NotFound

    orchestrator.reconcile(db)

    assert len(_instances(db, deployment)) == 0


def test_failed_slot_blocks_new_scaleup(db, deployment, patched, fake_docker):
    """failed-инстанс занимает слот: при target=1 новый контейнер не создаётся."""
    inst = models.Instance(
        deployment_id=deployment.id,
        container_name="deployer-dep_qwe_v1.0.0_9001",
        assigned_port=9001,
        status="failed",
        restart_count=MAX_RESTARTS,
    )
    db.add(inst)
    db.commit()
    fake_docker.containers.add(FakeContainer(inst.container_name, status="exited"))

    orchestrator.reconcile(db)

    assert len(_instances(db, deployment)) == 1
    assert len(patched["deploy"]) == 0


def test_scale_down_removes_extra(db, deployment, patched, fake_docker):
    for port in (9001, 9002):
        name = f"deployer-dep_qwe_v1.0.0_{port}"
        db.add(models.Instance(
            deployment_id=deployment.id,
            container_name=name,
            assigned_port=port,
            status="online",
            restart_count=0,
        ))
        fake_docker.containers.add(FakeContainer(name, status="running"))
    db.commit()
    # target=1, а живых 2 -> ожидаем удаление одного

    orchestrator.reconcile(db)

    assert len(_instances(db, deployment)) == 1
    assert len(patched["remove"]) == 1


def test_get_available_port_skips_used(db, deployment, patched, fake_docker):
    db.add(models.Instance(
        deployment_id=deployment.id,
        container_name="deployer-dep_qwe_v1.0.0_9001",
        assigned_port=9001,
        status="online",
        restart_count=0,
    ))
    db.commit()

    port = orchestrator.get_available_port(db, "backend-services")
    assert port == 9002


def test_no_free_ports_returns_none(db, deployment, patched, fake_docker):
    for port in (9001, 9002, 9003):
        db.add(models.Instance(
            deployment_id=deployment.id,
            container_name=f"deployer-dep_qwe_v1.0.0_{port}",
            assigned_port=port,
            status="online",
            restart_count=0,
        ))
    db.commit()

    assert orchestrator.get_available_port(db, "backend-services") is None
