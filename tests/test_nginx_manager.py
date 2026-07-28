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


def test_panel_config_has_rate_limit(monkeypatch, tmp_path):
    """Панель (10-panel.conf) НЕСЁТ санитарный limit_req/limit_conn: её домен
    обслуживает и публичный неаутентифицированный POST /api/auth/token, а тело
    поднято до 100m — без conn/req-лимита это усиливало бы flood/slow-body-DoS.
    Держим симметрично app-доменам (общие зоны app_rl/app_conn)."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    # Отключаем побочные эффекты catchall (openssl в контейнере) и правки прав webroot.
    monkeypatch.setattr(nginx_manager, "_write_catchall_if_changed", lambda: False)
    monkeypatch.setattr(nginx_manager, "ensure_acme_webroot_traversable", lambda: None)

    nginx_manager.update_panel_nginx_config(domain="panel.example.com", ssl_cert_name=None)
    content = (tmp_path / "10-panel.conf").read_text(encoding="utf-8")

    assert "server_name panel.example.com" in content
    assert "limit_req zone=app_rl burst=60 nodelay;" in content
    assert "limit_conn app_conn 40;" in content
    # Лимиты — на проксирующем location /, НЕ в ACME-локации (SSL-выпуск не лимитируем).
    acme_block = content.split(".well-known/acme-challenge/")[1].split("}")[0]
    assert "limit_req" not in acme_block


def test_panel_config_has_body_size_limit(monkeypatch, tmp_path):
    """Панель несёт client_max_body_size (100m) во всех ветках location / — иначе
    nginx берёт дефолт 1 МБ и режет публикацию ЛК (мультифайл-сайт с картинками)
    «413 Request Entity Too Large» ДО FastAPI. Rate-зон у панели по-прежнему нет."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    monkeypatch.setattr(nginx_manager, "_write_catchall_if_changed", lambda: False)
    monkeypatch.setattr(nginx_manager, "ensure_acme_webroot_traversable", lambda: None)

    # HTTP-only ветка (домен без SSL).
    nginx_manager.update_panel_nginx_config(domain="panel.example.com", ssl_cert_name=None)
    http_only = (tmp_path / "10-panel.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 100m;" in http_only
    # 100m без conn/req-лимита усиливал бы DoS на публичный /token → лимит соседствует.
    assert "limit_req zone=app_rl burst=60 nodelay;" in http_only

    # HTTPS-ветка: лимит тела висит на проксирующем 443-блоке (не на 301-редиректе 80).
    nginx_manager.update_panel_nginx_config(
        domain="panel.example.com", ssl_cert_name="panel.example.com")
    https = (tmp_path / "10-panel.conf").read_text(encoding="utf-8")
    # HTTP-блок (80) — только редирект, без лимитов; лимиты на проксирующем 443-блоке.
    http80 = https.split("listen 443")[0]
    assert "limit_req" not in http80
    proxied = https.split("listen 443")[1]
    assert "client_max_body_size 100m;" in proxied
    assert "limit_req zone=app_rl burst=60 nodelay;" in proxied
    # 100m ≥ ЛК-максимума ~32 МБ (PUBLISH_MAX_BYTES + PUBLISH_MAX_ASSET_BYTES).
    from app.cloud.services import code_studio
    lk_max = code_studio.PUBLISH_MAX_BYTES + code_studio.PUBLISH_MAX_ASSET_BYTES
    assert 100 * 1024 * 1024 >= lk_max


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


# --- Self-heal: пересборка vhost'ов приложений на старте (тикет blood-pit, 413) ---


# Конфиг СТАРОГО образца — так писал деплоер до ADR-099: proxy-локация без
# client_max_body_size, из-за чего nginx брал дефолт 1 МБ и резал загрузки «413».
_LEGACY_CONF = """server {
    listen 80;
    server_name old.example.com;
    location / { proxy_pass http://deployer:7999/api/proxy/oldapp/; }
}"""


def test_resync_upgrades_legacy_config_without_body_limit(monkeypatch, tmp_path):
    """🔴 Тикет blood-pit (дефект 2): у приложения, созданного СТАРЫМ деплоером,
    в конфиге нет client_max_body_size → nginx режет тело >1 МБ (413), и обновление
    деплоера само по себе это не чинило. Пересборка на старте поднимает конфиг до
    текущего шаблона."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    (tmp_path / "oldapp.conf").write_text(_LEGACY_CONF, encoding="utf-8")

    changed = nginx_manager.resync_application_configs(
        [("oldapp", "old.example.com", None)])

    assert changed == 1
    content = (tmp_path / "oldapp.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 100m;" in content      # загрузки до 100 МБ проходят
    assert "limit_req zone=app_rl" in content           # заодно приехали лимиты P0


def test_resync_is_idempotent_no_needless_reload(monkeypatch, tmp_path):
    """Актуальный конфиг не переписывается — иначе каждый рестарт деплоера дёргал бы
    reload nginx на всех приложениях впустую."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    apps = [("app1", "a1.example.com", "a1.example.com")]

    assert nginx_manager.resync_application_configs(apps) == 1   # первый проход — создал
    assert nginx_manager.resync_application_configs(apps) == 0   # второй — без изменений


def test_resync_preserves_pro_override(monkeypatch, tmp_path):
    """Пересборка НЕ раздевает приложение: per-app override владельца (PRO) остаётся,
    иначе рестарт молча возвращал бы P0-дефолты (ADR-100)."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    (tmp_path / "pro_app.conf").write_text(_LEGACY_CONF, encoding="utf-8")

    nginx_manager.resync_application_configs(
        [("pro_app", "pro.example.com", None)],
        override_lookup=lambda name: {"burst": 200, "conn": 10, "body_size": "500m"})

    content = (tmp_path / "pro_app.conf").read_text(encoding="utf-8")
    assert "client_max_body_size 500m;" in content
    assert "burst=60 nodelay;" not in content


def test_resync_skips_app_without_domain_and_survives_errors(monkeypatch, tmp_path):
    """Приложение без домена своего vhost не имеет (пропуск), а сбой на одном
    приложении не срывает остальные — нода обязана стартовать."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    def boom(name):
        if name == "bad":
            raise RuntimeError("битый override")
        return None

    changed = nginx_manager.resync_application_configs(
        [("nodomain", None, None), ("bad", "bad.example.com", None),
         ("good", "good.example.com", None)],
        override_lookup=boom)

    assert changed == 1                                  # доехало «good»
    assert not (tmp_path / "nodomain.conf").exists()
    assert (tmp_path / "good.conf").exists()
