"""Тесты заголовков безопасности панели (app/security_headers.py + middleware)."""
from fastapi.testclient import TestClient

from app import security_headers


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
