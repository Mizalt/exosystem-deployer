"""Тесты прокси-роутера приложений (app/routers/proxy.py).

Покрывают ветки маршрутизации без реального сетевого соединения: не найдено,
требуется basic-auth, нет онлайн-реплик, ошибка соединения с репликой.
"""
import base64

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import models, security


@pytest.fixture
def proxy_env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    import main
    app = main.app

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield Session, TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _seed_app(Session, *, with_user=False, online=True):
    """Создаёт blueprint→artifact→deployment→application (+опц. реплику/юзера)."""
    s = Session()
    bp = models.AppBlueprint(name="app")
    s.add(bp); s.commit()
    art = models.Artifact(version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id)
    s.add(art); s.commit()
    dep = models.Deployment(blueprint_id=bp.id, artifact_id=art.id, target_replicas=1, group_name="g")
    s.add(dep); s.commit()
    if online:
        s.add(models.Instance(
            deployment_id=dep.id, assigned_port=9001, status="online", container_name="deployer-app-1"
        ))
        s.commit()
    application = models.Application(name="app", domain="app.example.com", deployment_id=dep.id)
    s.add(application); s.commit()
    if with_user:
        s.add(models.AppUser(
            username="bob",
            hashed_password=security.get_password_hash("pw"),
            application_id=application.id,
        ))
        s.commit()
    s.close()


def test_proxy_unknown_application_404(proxy_env):
    _, client = proxy_env
    r = client.get("/api/proxy/nope/path")
    assert r.status_code == 404


def test_proxy_requires_basic_auth_401(proxy_env):
    Session, client = proxy_env
    _seed_app(Session, with_user=True)
    r = client.get("/api/proxy/app/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_proxy_wrong_credentials_401(proxy_env):
    Session, client = proxy_env
    _seed_app(Session, with_user=True)
    token = base64.b64encode(b"bob:wrong").decode()
    r = client.get("/api/proxy/app/", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 401


def test_proxy_no_online_instances_503(proxy_env):
    Session, client = proxy_env
    _seed_app(Session, online=False)
    r = client.get("/api/proxy/app/")
    assert r.status_code == 503


def test_proxy_connect_error_502(proxy_env, monkeypatch):
    Session, client = proxy_env
    _seed_app(Session, online=True)

    from app.routers import proxy

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/")
    assert r.status_code == 502
