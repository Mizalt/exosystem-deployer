"""Общие фикстуры и фейки для тестов.

Тесты ядра не должны зависеть от реального Docker или файловой БД, поэтому:
- БД — in-memory SQLite (свежая на каждый тест);
- Docker — заменяется лёгкими фейками (FakeDockerClient).
"""
import docker
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models


# --------------------------------------------------------------------------- #
#  База данных
# --------------------------------------------------------------------------- #
@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def deployment(db):
    """Готовый граф: группа портов + blueprint + artifact + deployment(target=1)."""
    group = models.AppGroup(name="backend-services", start_port=9001, end_port=9003)
    bp = models.AppBlueprint(name="qwe")
    db.add_all([group, bp])
    db.commit()
    art = models.Artifact(
        version_tag="v1.0.0",
        zip_hash="hash123",
        stored_zip_path="uploads/hash123.zip",
        blueprint_id=bp.id,
    )
    db.add(art)
    db.commit()
    dep = models.Deployment(
        blueprint_id=bp.id,
        artifact_id=art.id,
        target_replicas=1,
        group_name="backend-services",
    )
    db.add(dep)
    db.commit()
    return dep


# --------------------------------------------------------------------------- #
#  Фейковый Docker
# --------------------------------------------------------------------------- #
class FakeContainer:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status
        self.attrs = {}
        self.stopped = False
        self.removed = False

    def stop(self):
        self.stopped = True
        self.status = "exited"

    def remove(self, force=False, v=False):
        self.removed = True


class FakeContainers:
    def __init__(self):
        self._by_name = {}

    def add(self, container):
        self._by_name[container.name] = container

    def get(self, name):
        if name in self._by_name:
            return self._by_name[name]
        raise docker.errors.NotFound(f"container {name} not found")

    def list(self, all=False, filters=None):
        return list(self._by_name.values())


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()


@pytest.fixture
def fake_docker():
    return FakeDockerClient()
