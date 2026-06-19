"""API-тесты через FastAPI TestClient.

БД подменяется на in-memory (StaticPool — общая на все сессии), аутентификация —
через override зависимости. Lifespan НЕ запускается (TestClient без контекста),
поэтому оркестратор/nginx не стартуют.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import models, security


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
def test_protected_endpoint_requires_auth(api_env):
    _, _, client = api_env
    r = client.get("/api/blueprints")
    assert r.status_code == 401


def test_blueprints_crud(auth_client):
    client, _ = auth_client
    assert client.get("/api/blueprints").json() == []

    r = client.post("/api/blueprints", json={"name": "myapp", "description": "demo"})
    assert r.status_code == 201
    assert r.json()["name"] == "myapp"

    names = [b["name"] for b in client.get("/api/blueprints").json()]
    assert "myapp" in names


def test_blueprint_duplicate_rejected(auth_client):
    client, _ = auth_client
    client.post("/api/blueprints", json={"name": "dup"})
    r = client.post("/api/blueprints", json={"name": "dup"})
    assert r.status_code == 400


def test_groups_crud_and_validation(auth_client):
    client, _ = auth_client

    ok = client.post("/api/groups", json={"name": "g1", "start_port": 9001, "end_port": 9010})
    assert ok.status_code == 200

    bad = client.post("/api/groups", json={"name": "g2", "start_port": 9010, "end_port": 9001})
    assert bad.status_code == 422  # start_port >= end_port


def test_login_flow(api_env):
    _, Session, client = api_env
    s = Session()
    s.add(models.User(username="admin", hashed_password=security.get_password_hash("pw12345")))
    s.commit()
    s.close()

    ok = client.post("/api/auth/token", data={"username": "admin", "password": "pw12345"})
    assert ok.status_code == 200
    assert "access_token" in ok.json()

    wrong = client.post("/api/auth/token", data={"username": "admin", "password": "nope"})
    assert wrong.status_code == 401
