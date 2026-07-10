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
    s.add(bp)
    s.commit()
    art = models.Artifact(version_tag="v1", zip_hash="h", stored_zip_path="uploads/h.zip", blueprint_id=bp.id)
    s.add(art)
    s.commit()
    dep = models.Deployment(blueprint_id=bp.id, artifact_id=art.id, target_replicas=1, group_name="g")
    s.add(dep)
    s.commit()
    if online:
        s.add(models.Instance(
            deployment_id=dep.id, assigned_port=9001, status="online", container_name="deployer-app-1"
        ))
        s.commit()
    application = models.Application(name="app", domain="app.example.com", deployment_id=dep.id)
    s.add(application)
    s.commit()
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


# --- Заглушка окна неготовности (задача #3, ADR-142) ---

def _assert_is_placeholder(r):
    """Общие проверки заглушки: 200, маркер, анти-кэш/анти-индекс, авто-обновление."""
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert 'data-page="warmup-placeholder"' in r.text          # маркер заглушки
    assert r.headers.get("cache-control") == "no-store"        # не кэшировать
    assert r.headers.get("x-robots-tag") == "noindex"          # не индексировать
    assert 'http-equiv="refresh"' in r.text                    # авто-перезагрузка ~10с
    assert 'name="robots" content="noindex"' in r.text


def test_placeholder_html_navigation_no_online_503_path(proxy_env):
    """Нет online-реплик + браузерная навигация (Accept: text/html) → заглушка 200."""
    Session, client = proxy_env
    _seed_app(Session, online=False)
    r = client.get("/api/proxy/app/", headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    _assert_is_placeholder(r)


def test_placeholder_html_navigation_connect_error_path(proxy_env, monkeypatch):
    """Реплика online, но ConnectError + Accept: text/html → заглушка 200 (не 502)."""
    Session, client = proxy_env
    _seed_app(Session, online=True)

    from app.routers import proxy

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/", headers={"Accept": "text/html"})
    _assert_is_placeholder(r)


def test_no_placeholder_for_non_html_requests(proxy_env, monkeypatch):
    """КЛЮЧЕВОЕ разграничение: API/XHR/ассеты сервиса (Accept без text/html)
    получают прежние 502/503, а не HTML-мусор."""
    Session, client = proxy_env

    # 503-путь: нет online-реплик, Accept: application/json
    _seed_app(Session, online=False)
    r = client.get("/api/proxy/app/", headers={"Accept": "application/json"})
    assert r.status_code == 503
    assert "warmup-placeholder" not in r.text

    # Accept: */* (дефолт fetch/httpx) — тоже НЕ навигация → 503
    r = client.get("/api/proxy/app/", headers={"Accept": "*/*"})
    assert r.status_code == 503

    # 502-путь: ConnectError, Accept: application/json
    s = Session()
    dep = s.query(models.Deployment).first()
    s.add(models.Instance(
        deployment_id=dep.id, assigned_port=9001, status="online", container_name="deployer-app-1"
    ))
    s.commit()
    s.close()

    from app.routers import proxy

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/", headers={"Accept": "application/json"})
    assert r.status_code == 502
    assert "warmup-placeholder" not in r.text


def test_placeholder_is_generic_no_request_reflection(proxy_env):
    """🔴 Безопасность: заглушка генерична — никакой подстановки Host/домена/пути/
    имени приложения (нулевая инъекционная поверхность, no reflected content)."""
    Session, client = proxy_env
    _seed_app(Session, online=False)
    evil = "evil.example</h1><script>alert(1)</script>"
    r = client.get(
        "/api/proxy/app/some/inner/path",
        headers={"Accept": "text/html", "X-Evil": evil},
    )
    _assert_is_placeholder(r)
    assert "app.example.com" not in r.text      # домен приложения не отражён
    assert "deployer-app" not in r.text         # внутренние имена контейнеров не палятся
    assert "some/inner/path" not in r.text      # путь запроса не отражён
    assert "<script>" not in r.text             # и вообще никакого reflected-контента
    assert evil not in r.text

    # Рендер намеренно без параметров: страница одинакова для любых запросов.
    from app.services import placeholder
    assert placeholder.render_placeholder() == r.text


def test_proxy_online_replica_still_proxies_html_navigation(proxy_env, monkeypatch):
    """Нормальный путь НЕ тронут: online-реплика отвечает → браузерная навигация
    получает ответ сервиса, а не заглушку."""
    Session, client = proxy_env
    _seed_app(Session, online=True)

    from app.routers import proxy

    class FakeResp:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/html"})

        async def aiter_raw(self):
            yield b"<h1>real service</h1>"

        async def aclose(self):
            pass

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            return FakeResp()

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.text == "<h1>real service</h1>"
    assert "warmup-placeholder" not in r.text


# --- Дофикс #3 по ревью: намерение навигации + генеричные тела ошибок ---

def test_no_placeholder_for_post_form_submission(proxy_env):
    """Дофикс по ревью: браузерный POST формы (Accept: text/html) в окно
    неготовности — честный 503, а НЕ 200-заглушка (иначе тело формы молча
    теряется, а пользователь думает, что данные приняты)."""
    Session, client = proxy_env
    _seed_app(Session, online=False)
    r = client.post("/api/proxy/app/submit", headers={"Accept": "text/html"},
                    data={"field": "value"})
    assert r.status_code == 503
    assert "warmup-placeholder" not in r.text


def test_no_placeholder_for_html_fragment_xhr(proxy_env):
    """Дофикс по ревью: XHR за html-ФРАГМЕНТОМ (jQuery dataType:'html' — Accept
    с text/html, но Sec-Fetch-Mode: cors) — НЕ навигация → прежний 503, иначе
    в DOM фронта вставлялась бы целая страница-заглушка со статусом 200."""
    Session, client = proxy_env
    _seed_app(Session, online=False)
    r = client.get("/api/proxy/app/fragment", headers={
        "Accept": "text/html, */*; q=0.01",
        "Sec-Fetch-Mode": "cors",
    })
    assert r.status_code == 503
    assert "warmup-placeholder" not in r.text


def test_placeholder_for_sec_fetch_navigate(proxy_env):
    """Sec-Fetch-Mode: navigate (современный браузер по HTTPS) → заглушка."""
    Session, client = proxy_env
    _seed_app(Session, online=False)
    r = client.get("/api/proxy/app/", headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Mode": "navigate",
    })
    _assert_is_placeholder(r)


def test_no_placeholder_for_explicit_html_refusal_q0(proxy_env):
    """Дофикс по ревью: Accept: text/html;q=0 — явный ОТКАЗ от html
    (RFC 9110 §12.4.2), подстрочный матч давал бы заглушку."""
    Session, client = proxy_env
    _seed_app(Session, online=False)
    r = client.get("/api/proxy/app/", headers={"Accept": "text/html;q=0, application/json"})
    assert r.status_code == 503
    assert "warmup-placeholder" not in r.text


def test_placeholder_on_connect_timeout(proxy_env, monkeypatch):
    """Дофикс по ревью: httpx.ConnectTimeout — НЕ подкласс ConnectError, но это
    то же окно неготовности (порт слушается, приложение виснет на инициализации):
    навигации — заглушка, API — генеричный 502 (не сырой 500)."""
    Session, client = proxy_env
    _seed_app(Session, online=True)

    from app.routers import proxy

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/", headers={"Accept": "text/html"})
    _assert_is_placeholder(r)

    r = client.get("/api/proxy/app/", headers={"Accept": "application/json"})
    assert r.status_code == 502
    assert "deployer-app" not in r.text  # имя контейнера не палится


def test_502_body_does_not_leak_container_name(proxy_env, monkeypatch):
    """Дофикс по ревью: 502 для API-клиентов — генеричное тело, внутреннее имя
    контейнера (deployer-*) деанонимизирует платформу (white-label ADR-142)."""
    Session, client = proxy_env
    _seed_app(Session, online=True)

    from app.routers import proxy

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/", headers={"Accept": "application/json"})
    assert r.status_code == 502
    assert "deployer-app" not in r.text
    assert "text/plain" in r.headers.get("content-type", "")


def test_500_body_does_not_leak_exception_text(proxy_env, monkeypatch):
    """Дофикс по ревью: текст произвольного исключения (внутренний URL, детали
    httpx) НЕ отражается публичному посетителю — наружу константа, детали в лог."""
    Session, client = proxy_env
    _seed_app(Session, online=True)

    from app.routers import proxy

    class FakeHttp:
        def build_request(self, **kwargs):
            return object()

        async def send(self, request, stream=False):
            raise RuntimeError("boom at http://deployer-app-1:80/secret")

    monkeypatch.setattr(proxy, "http_client", FakeHttp())
    r = client.get("/api/proxy/app/")
    assert r.status_code == 500
    assert r.text == "Proxy error."
    assert "deployer-app" not in r.text
    assert "boom" not in r.text


def test_404_body_does_not_reflect_app_name(proxy_env):
    """Дофикс по ревью: path-сегмент app_name контролируется клиентом — в тело
    404 не отражается (reflected-инъекция), Content-Type проставлен."""
    _, client = proxy_env
    r = client.get("/api/proxy/%3Cscript%3Ealert(1)%3C%2Fscript%3E/x")
    assert r.status_code == 404
    assert "<script>" not in r.text
    assert "alert(1)" not in r.text
    assert "text/plain" in r.headers.get("content-type", "")


def test_dangling_deployment_navigation_gets_placeholder(proxy_env):
    """Дофикс по ревью: висячий deployment_id (SQLite без enforce FK — деплой
    удалён, приложение осталось) — то же окно неготовности: навигации заглушка,
    API — генеричный 503 без имени приложения."""
    Session, client = proxy_env
    s = Session()
    s.add(models.Application(name="app", domain="app.example.com", deployment_id=999))
    s.commit()
    s.close()

    r = client.get("/api/proxy/app/", headers={"Accept": "text/html"})
    _assert_is_placeholder(r)

    r = client.get("/api/proxy/app/", headers={"Accept": "application/json"})
    assert r.status_code == 503
    assert r.text == "Service is not ready."  # генерика без имени приложения


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
