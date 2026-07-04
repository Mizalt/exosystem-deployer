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


def test_round_robin_rotates_across_replicas():
    """Идея 5 фаза 1: трафик распределяется по ВСЕМ online-репликам по кругу,
    а не уходит всегда в первую (online_instances[0])."""
    from app.routers import proxy

    class _Inst:
        def __init__(self, id, name):
            self.id = id
            self.container_name = name

    # Передаём неотсортированными — хелпер сортирует по id (стабильный порядок ротации).
    insts = [_Inst(2, "b"), _Inst(1, "a"), _Inst(3, "c")]
    proxy._rr_counters.pop(777, None)
    picks = [proxy._pick_round_robin(777, insts).container_name for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_round_robin_single_replica_is_stable():
    from app.routers import proxy

    class _Inst:
        def __init__(self, id, name):
            self.id = id
            self.container_name = name

    insts = [_Inst(5, "only")]
    proxy._rr_counters.pop(778, None)
    assert [proxy._pick_round_robin(778, insts).container_name for _ in range(3)] == ["only", "only", "only"]


def test_proxy_success_streams_response(proxy_env, monkeypatch):
    """Success-путь: ответ апстрима стримится через StreamingResponse (а не падает на
    Response(content=<async gen>)). Hop-by-hop заголовки (transfer-encoding) убираются."""
    Session, client = proxy_env
    _seed_app(Session, online=True)

    from app.routers import proxy

    class FakeResp:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/plain", "transfer-encoding": "chunked", "x-test": "1"})

        async def aiter_raw(self):
            yield b"hello "
            yield b"world"

        async def aclose(self):
            pass

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            return FakeResp()

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/")
    assert r.status_code == 200
    assert r.content == b"hello world"
    assert r.headers.get("x-test") == "1"
    assert "transfer-encoding" not in {k.lower() for k in r.headers}  # hop-by-hop снят


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


def test_proxy_basic_auth_rate_limited(proxy_env, monkeypatch):
    """V-08: перебор пароля app-пользователя ловится лимитером → 429 после N неудач."""
    Session, client = proxy_env
    _seed_app(Session, with_user=True)

    from app.routers import proxy
    from app.rate_limit import LoginRateLimiter
    monkeypatch.setattr(proxy, "app_auth_limiter", LoginRateLimiter(max_fails=3, window=300))

    tok = base64.b64encode(b"bob:wrong").decode()
    for _ in range(3):
        assert client.get("/api/proxy/app/", headers={"Authorization": f"Basic {tok}"}).status_code == 401
    r = client.get("/api/proxy/app/", headers={"Authorization": f"Basic {tok}"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
