"""Тесты docker_manager: генерация Dockerfile и сборка сирот."""
from app.services import docker_manager
from tests.conftest import FakeContainer, FakeDockerClient


def test_generate_dockerfile_has_uvicorn_cmd():
    df = docker_manager.generate_dockerfile()
    assert "uvicorn" in df
    assert "CMD" in df
    assert "--port" in df


def test_cleanup_orphans_removes_only_unknown(monkeypatch):
    fake = FakeDockerClient()
    keep = FakeContainer("deployer-keep")
    orphan = FakeContainer("deployer-orphan", status="restarting")
    fake.containers.add(keep)
    fake.containers.add(orphan)
    monkeypatch.setattr(docker_manager, "client", fake)

    removed = docker_manager.cleanup_orphan_containers({"deployer-keep"})

    assert removed == 1
    assert orphan.removed is True
    assert keep.removed is False


def test_cleanup_orphans_empty_known_removes_all(monkeypatch):
    fake = FakeDockerClient()
    a = FakeContainer("deployer-a")
    b = FakeContainer("deployer-b")
    fake.containers.add(a)
    fake.containers.add(b)
    monkeypatch.setattr(docker_manager, "client", fake)

    removed = docker_manager.cleanup_orphan_containers(set())

    assert removed == 2
    assert a.removed and b.removed
