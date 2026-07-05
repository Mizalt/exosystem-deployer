"""Общие фикстуры и фейки для тестов.

Тесты ядра не должны зависеть от реального Docker или файловой БД, поэтому:
- БД — in-memory SQLite (свежая на каждый тест);
- Docker — заменяется лёгкими фейками (FakeDockerClient).
"""
import docker
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import models, security


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
#  Изоляция фоновых замеров операций (Ночь 14, ADR-082)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _op_metrics_isolated(monkeypatch):
    """`op_metrics.record` без явной сессии открывает `SessionLocal` (файловая
    data/deployer.db) — из тестов перенаправляем в одноразовую in-memory БД,
    чтобы сборки/задачи в тестах не писали замеры в дев-базу репозитория.
    Ленивая инициализация: тесты, не пишущие замеры, ничего не платят."""
    from app.services import op_metrics
    state = {}

    def _lazy_session():
        if "Session" not in state:
            engine = create_engine(
                "sqlite://", connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            state["Session"] = sessionmaker(bind=engine)
        return state["Session"]()

    monkeypatch.setattr(op_metrics, "SessionLocal", _lazy_session)


# --------------------------------------------------------------------------- #
#  Изоляция журнала версий (Ночь 16, ADR-085)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _update_history_isolated(tmp_path, monkeypatch):
    """Финализация задачи `self_update` пишет журнал в data/update_history.json —
    из тестов перенаправляем во временный файл, чтобы не трогать дев-каталог."""
    from app.services import self_update
    monkeypatch.setattr(self_update, "UPDATE_HISTORY_FILE",
                        tmp_path / "update_history.json")


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


# --------------------------------------------------------------------------- #
#  API (FastAPI TestClient) — общие для всех test_*.py, бьющих по эндпоинтам.
#  БД подменяется на in-memory (StaticPool — общая на все сессии), аутентификация
#  — через override зависимости. Lifespan НЕ запускается (TestClient без
#  контекста), поэтому оркестратор/nginx не стартуют.
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    import main  # импорт здесь, чтобы не было побочных эффектов на этапе сбора тестов
    app = main.app

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, Session, TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def auth_client(api_env):
    app, Session, client = api_env
    fake = models.User(id=1, username="tester", hashed_password="x")
    app.dependency_overrides[security.get_current_user] = lambda: fake
    return client, Session


# --------------------------------------------------------------------------- #
#  Cloud (ЛК / контрол-плейн) TestClient — отдельное приложение + своя БД.
# --------------------------------------------------------------------------- #
@pytest.fixture
def cloud_env():
    from app.cloud.database import CloudBase, get_cloud_db
    from app.cloud.app import cloud_app, cloud_login_limiter

    # Лимитер логина — процессный синглтон с ключом ip:testclient (общим для всех тестов),
    # чистим на каждый тест, чтобы неудачи одного не «протекали» в другой (Ночь 4).
    cloud_login_limiter.clear()
    # ADR-093: процессные кэши сессий/лимитов тоже не должны протекать между тестами.
    from app.cloud import auth as cloud_auth
    from app.cloud.routers.admin import admin_action_limiter
    cloud_auth.clear_last_seen_cache()
    admin_action_limiter.clear()

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CloudBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_cloud_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    cloud_app.dependency_overrides[get_cloud_db] = override_get_cloud_db
    try:
        yield cloud_app, Session, TestClient(cloud_app)
    finally:
        cloud_app.dependency_overrides.clear()
