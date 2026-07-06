"""Тесты генерации конфигов Nginx (без обращения к реальному Docker/FS проекта)."""
from app import config as app_config
from app.services import nginx_manager


def test_app_config_uses_resolver_and_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config(
        app_name="myapp", domain="my.example.com", ssl_cert_name="my.example.com"
    )
    content = (tmp_path / "myapp.conf").read_text(encoding="utf-8")

    # доводка прокси: resolver + переменная (анти-502 при рестарте деплоера)
    assert "resolver 127.0.0.11" in content
    assert "$deployer_upstream" in content
    assert "/api/proxy/myapp" in content
    assert "server_name my.example.com" in content
    # при ssl присутствует https-блок
    assert "listen 443 ssl" in content
    assert "fullchain.pem" in content


def test_app_config_http_only_without_ssl(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config(
        app_name="plainapp", domain="plain.example.com", ssl_cert_name=None
    )
    content = (tmp_path / "plainapp.conf").read_text(encoding="utf-8")

    assert "listen 80" in content
    assert "listen 443" not in content          # без SSL нет https-блока
    assert "proxy_pass" in content              # трафик идёт на деплоер, не redirect


def test_remove_application_config(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config("tmpapp", "t.example.com")
    assert (tmp_path / "tmpapp.conf").exists()

    nginx_manager.remove_application_nginx_config("tmpapp")
    assert not (tmp_path / "tmpapp.conf").exists()


# --- P0: САНИТАРНЫЙ RATE-LIMIT (OSS-ядро, ADR-099) ---


def test_zones_file_contains_rate_limit_zones(monkeypatch, tmp_path):
    """00-zones.conf содержит http-зоны limit_req_zone + limit_conn_zone."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    changed = nginx_manager._write_zones_if_changed()
    assert changed is True  # первый вызов создаёт файл

    content = (tmp_path / "00-zones.conf").read_text(encoding="utf-8")
    assert "limit_req_zone $binary_remote_addr zone=app_rl:10m rate=30r/s;" in content
    assert "limit_conn_zone $binary_remote_addr zone=app_conn:10m;" in content


def test_zones_write_is_idempotent(monkeypatch, tmp_path):
    """Повторная запись зон при неизменном шаблоне не считается изменением."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    assert nginx_manager._write_zones_if_changed() is True   # создано
    assert nginx_manager._write_zones_if_changed() is False  # без изменений → reload не нужен


def test_app_https_block_has_rate_limit(monkeypatch, tmp_path):
    """В HTTPS-блоке app-домена (location /) есть limit_req/limit_conn/body-size."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config(
        app_name="myapp", domain="my.example.com", ssl_cert_name="my.example.com"
    )
    content = (tmp_path / "myapp.conf").read_text(encoding="utf-8")

    assert "limit_req zone=app_rl burst=60 nodelay;" in content
    assert "limit_conn app_conn 40;" in content
    assert "client_max_body_size 100m;" in content
    # limit_req НЕ в ACME-локации (выпуск/продление SSL не лимитируется).
    acme_idx = content.index("acme-challenge")
    acme_block = content[acme_idx:content.index("}", acme_idx)]
    assert "limit_req" not in acme_block


def test_app_http_only_block_has_rate_limit(monkeypatch, tmp_path):
    """Без SSL проксирующий HTTP-блок app-домена тоже лимитирован."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config(
        app_name="plainapp", domain="plain.example.com", ssl_cert_name=None
    )
    content = (tmp_path / "plainapp.conf").read_text(encoding="utf-8")

    assert "limit_req zone=app_rl burst=60 nodelay;" in content
    assert "limit_conn app_conn 40;" in content
    assert "proxy_pass" in content  # это proxy-location, а не редирект


def test_app_https_redirect_block_not_limited(monkeypatch, tmp_path):
    """При SSL HTTP-блок только редиректит (301) — лимиты там не нужны/не мешают ACME."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config(
        app_name="ssapp", domain="ss.example.com", ssl_cert_name="ss.example.com"
    )
    content = (tmp_path / "ssapp.conf").read_text(encoding="utf-8")

    # Разбираем на server-блоки: HTTP (listen 80) — редирект без limit_req.
    http_server = content.split("listen 443")[0]
    assert "return 301 https://$host$request_uri;" in http_server
    assert "limit_req" not in http_server


def test_panel_config_has_no_rate_limit(monkeypatch, tmp_path):
    """Панель (10-panel.conf) НЕ лимитируется — panel и app это разные server-блоки."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    # Отключаем побочные эффекты catchall (openssl в контейнере) и правки прав webroot.
    monkeypatch.setattr(nginx_manager, "_write_catchall_if_changed", lambda: False)
    monkeypatch.setattr(nginx_manager, "ensure_acme_webroot_traversable", lambda: None)

    nginx_manager.update_panel_nginx_config(domain="panel.example.com", ssl_cert_name=None)
    content = (tmp_path / "10-panel.conf").read_text(encoding="utf-8")

    assert "server_name panel.example.com" in content
    assert "limit_req" not in content
    assert "limit_conn" not in content


def test_catchall_template_has_no_rate_limit():
    """Catchall-шаблон (default_server/ACME/403) не содержит limit_req."""
    assert "limit_req" not in nginx_manager.CATCHALL_CONFIG_TEMPLATE
    assert "limit_conn" not in nginx_manager.CATCHALL_CONFIG_TEMPLATE


# --- P1 (ADR-100): per-app override лимитов (демо-фича rate_limit_ui) ---------------

def test_rate_limit_directives_defaults_match_p0():
    """rate_limit_directives() без аргументов = P0-константы (обратная совместимость)."""
    assert nginx_manager.rate_limit_directives() == nginx_manager.RATE_LIMIT_DIRECTIVES
    assert "burst=60 nodelay;" in nginx_manager.RATE_LIMIT_DIRECTIVES
    assert "limit_conn app_conn 40;" in nginx_manager.RATE_LIMIT_DIRECTIVES


def test_rate_limit_directives_override():
    """Per-app override перекрывает burst/conn/body_size, зоны остаются те же."""
    d = nginx_manager.rate_limit_directives(burst=120, conn=25, body_size="250m")
    assert "limit_req zone=app_rl burst=120 nodelay;" in d
    assert "limit_conn app_conn 25;" in d
    assert "client_max_body_size 250m;" in d


def test_app_config_applies_per_app_override(monkeypatch, tmp_path):
    """update_application_nginx_config(rate_limit=...) пишет override в конфиг приложения."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    nginx_manager.update_application_nginx_config(
        app_name="pro_app", domain="pro.example.com", ssl_cert_name="pro.example.com",
        rate_limit={"burst": 200, "conn": 10, "body_size": "500m"})
    content = (tmp_path / "pro_app.conf").read_text(encoding="utf-8")
    assert "limit_req zone=app_rl burst=200 nodelay;" in content
    assert "limit_conn app_conn 10;" in content
    assert "client_max_body_size 500m;" in content
    # Дефолтов P0 в этом конфиге больше нет (перекрыты для этого app).
    assert "burst=60 nodelay;" not in content
