"""Тесты заголовков безопасности панели (app/security_headers.py + middleware)
и embed-origin «панели внутри ЛК» (app/embed_config.py, ADR-092)."""
import pytest
from fastapi.testclient import TestClient

from app import embed_config, security_headers


@pytest.fixture(autouse=True)
def _isolated_embed_config(tmp_path, monkeypatch):
    """Каждый тест — с чистой embed-конфигурацией (не трогаем data/ проекта)."""
    monkeypatch.setattr(embed_config, "CONFIG_FILE", tmp_path / "embed_origin.json")
    monkeypatch.delenv("DEPLOYER_EMBED_ORIGIN", raising=False)
    embed_config._cache["mtime"] = None
    embed_config._cache["value"] = None


# --- should_apply: проксируемые приложения исключены ------------------------ #

def test_should_apply_skips_proxy_paths():
    assert security_headers.should_apply("/api/proxy/myapp/") is False
    assert security_headers.should_apply("/api/proxy/myapp/page") is False


def test_should_apply_panel_paths():
    assert security_headers.should_apply("/") is True
    assert security_headers.should_apply("/api/blueprints") is True
    assert security_headers.should_apply("/static/js/app.js") is True


def test_csp_blocks_framing_and_allows_fonts():
    csp = security_headers.CSP
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp
    assert "fonts.googleapis.com" in csp and "fonts.gstatic.com" in csp


# --- middleware на живом приложении ----------------------------------------- #

def test_panel_response_has_security_headers():
    import main
    client = TestClient(main.app)
    r = client.get("/")  # index.html, без зависимостей от БД/lifespan
    assert r.status_code == 200
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" in r.headers


# --- ADR-092: встраивание панели в ЛК (embed-origin, fail-closed) ------------ #

def test_normalize_origin_accepts_origin_and_rejects_garbage():
    assert embed_config.normalize_origin("https://lk.example.com") == "https://lk.example.com"
    assert embed_config.normalize_origin(" https://LK.Example.com/ ") == "https://lk.example.com"
    assert embed_config.normalize_origin("http://127.0.0.1:8100") == "http://127.0.0.1:8100"
    assert embed_config.normalize_origin(None) is None
    assert embed_config.normalize_origin("  ") is None
    for bad in ("lk.example.com",              # без схемы
                "ftp://lk.example.com",        # не http(s)
                "https://lk.example.com/path",  # путь — не origin
                "https://lk.example.com?x=1",  # query
                "https://user@lk.example.com",  # юзеринфо
                "https://"):
        with pytest.raises(ValueError):
            embed_config.normalize_origin(bad)


def test_headers_fail_closed_without_origin():
    """Без настроенного origin — прежний запрет фрейминга (DENY + 'none')."""
    h = security_headers.current_headers()
    assert h["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]


def test_env_origin_relaxes_framing_for_one_origin(monkeypatch):
    """env `DEPLOYER_EMBED_ORIGIN` → frame-ancestors ровно этому origin, без XFO."""
    monkeypatch.setenv("DEPLOYER_EMBED_ORIGIN", "https://lk.example.com")
    import main
    r = TestClient(main.app).get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors https://lk.example.com" in csp
    assert "'none'" not in csp.split("frame-ancestors")[1]
    assert "X-Frame-Options" not in r.headers
    # Остальной хардинг не тронут.
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_invalid_env_origin_fails_closed(monkeypatch):
    """Мусор в env → НЕ «пропустить всё», а прежний запрет."""
    monkeypatch.setenv("DEPLOYER_EMBED_ORIGIN", "not-an-origin")
    h = security_headers.current_headers()
    assert h["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]


def test_broken_origin_file_fails_closed():
    embed_config.CONFIG_FILE.write_text("{broken json", encoding="utf-8")
    assert embed_config.get_embed_origin() is None
    embed_config.CONFIG_FILE.write_text('{"origin": "garbage-value"}', encoding="utf-8")
    embed_config._cache["mtime"] = None
    assert embed_config.get_embed_origin() is None


def test_embed_origin_endpoint_requires_auth(api_env):
    _, _, client = api_env
    r = client.post("/api/panel/settings/embed-origin",
                    json={"origin": "https://lk.example.com"})
    assert r.status_code == 401


def test_embed_origin_push_flow_and_clear(auth_client):
    """Пуш origin ЛК → заголовки разрешают фрейминг этому origin; null → запрет."""
    client, _ = auth_client
    r = client.post("/api/panel/settings/embed-origin",
                    json={"origin": "https://lk.example.com"})
    assert r.status_code == 200
    assert r.json() == {"origin": "https://lk.example.com",
                        "effective_origin": "https://lk.example.com"}
    page = client.get("/")
    assert "frame-ancestors https://lk.example.com" in page.headers["Content-Security-Policy"]
    assert "X-Frame-Options" not in page.headers
    # Очистка (origin: null) → fail-closed запрет, как раньше.
    r = client.post("/api/panel/settings/embed-origin", json={"origin": None})
    assert r.status_code == 200 and r.json()["effective_origin"] is None
    page = client.get("/")
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]


def test_embed_origin_endpoint_rejects_garbage(auth_client):
    client, _ = auth_client
    for bad in ("lk.example.com", "https://lk.example.com/path", "ftp://x"):
        assert client.post("/api/panel/settings/embed-origin",
                           json={"origin": bad}).status_code == 422
    # И ничего не сохранилось.
    assert embed_config.get_embed_origin() is None


def test_env_origin_beats_pushed_file(auth_client, monkeypatch):
    """env — статическая настройка self-host: приоритетнее пуша контрол-плейна."""
    client, _ = auth_client
    client.post("/api/panel/settings/embed-origin", json={"origin": "https://pushed.example"})
    monkeypatch.setenv("DEPLOYER_EMBED_ORIGIN", "https://env.example")
    assert embed_config.get_embed_origin() == "https://env.example"


def test_panel_embed_capability_declared():
    from app import version
    assert "panel_embed" in version.capabilities()


# --- V-10: HSTS ------------------------------------------------------------- #

def test_hsts_header_present():
    val = security_headers.SECURITY_HEADERS.get("Strict-Transport-Security")
    assert val and "max-age=" in val
    # includeSubDomains осознанно НЕ ставим (субдомен-приложения — своя политика).
    assert "includeSubDomains" not in val


def test_panel_response_has_hsts():
    import main
    client = TestClient(main.app)
    r = client.get("/")
    assert "Strict-Transport-Security" in r.headers
