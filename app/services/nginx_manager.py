# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/services/nginx_manager.py ---

from app.services.nginx_service import CERTBOT_CONTAINER_NAME
from app.services import docker_manager

import os  # Добавляем os
from typing import Optional
from app import config

# Хост и порт деплоера С ТОЧКИ ЗРЕНИЯ NGINX-КОНТЕЙНЕРА.
# По умолчанию — имя контейнера деплоера в общей сети deployer-net ('deployer').
# Для запуска деплоера процессом на хосте (legacy) задайте
# DEPLOYER_PROXY_HOST=host.docker.internal в окружении.
DEPLOYER_HOST = os.environ.get("DEPLOYER_PROXY_HOST", "deployer")
DEPLOYER_PORT = int(os.environ.get("DEPLOYER_PROXY_PORT", "7999"))

RESOLVER_BLOCK = ""
SET_HOST_VAR = ""

# --- P0: САНИТАРНЫЙ RATE-LIMIT (OSS-ядро, ADR-099) ---
#
# Зоны limit_req/limit_conn объявляются в http-контексте. Каталог nginx_configs
# смонтирован как /etc/nginx/conf.d и включается стоковым nginx.conf ВНУТРИ http{},
# поэтому limit_req_zone/limit_conn_zone здесь валидны. Имя `00-zones.conf`
# грузится по алфавиту ДО app/panel-конфигов — зоны определены к моменту ссылки.
#
# Дефолты — сдержанный «предохранитель»: 30r/s + burst 60 nodelay комфортны для
# SPA/dashboard/API и режут ботов/сканеры; limit_conn 40/IP душит только явный
# флуд; 100m не мешает обычным загрузкам, но ставит потолок. Зоны 10m ≈ ~160k IP.
RATE_LIMIT_RATE = "30r/s"
RATE_LIMIT_BURST = 60
RATE_LIMIT_CONN = 40
CLIENT_MAX_BODY_SIZE = "100m"

ZONES_CONFIG_TEMPLATE = f"""# Сгенерировано EXOSYSTEM DEPLOY (P0 rate-limit, ADR-099). Не редактировать вручную.
limit_req_zone $binary_remote_addr zone=app_rl:10m rate={RATE_LIMIT_RATE};
limit_conn_zone $binary_remote_addr zone=app_conn:10m;
"""

def rate_limit_directives(burst: int | None = None, conn: int | None = None,
                          body_size: str | None = None) -> str:
    """Директивы лимитов для location / app-домена.

    P0 (дефолт, все аргументы None) → сдержанный «предохранитель». P1/PRO
    (демо-фича `rate_limit_ui`) вызывает с per-app значениями, перекрывая дефолты для
    конкретного приложения — поэтому генерация конфига параметризована здесь, а не
    жёстко зашита строкой. Зоны (`app_rl`/`app_conn`) общие (объявлены в 00-zones.conf),
    per-app меняется только burst/limit_conn/client_max_body_size в самом location.
    """
    burst = RATE_LIMIT_BURST if burst is None else burst
    conn = RATE_LIMIT_CONN if conn is None else conn
    body_size = CLIENT_MAX_BODY_SIZE if body_size is None else body_size
    return (
        f"limit_req zone=app_rl burst={burst} nodelay; "
        f"limit_conn app_conn {conn}; "
        f"client_max_body_size {body_size};"
    )


# Директивы лимитов по умолчанию (P0). Оставлено как константа для обратной
# совместимости кода/тестов, ссылавшихся на неё; эквивалентно rate_limit_directives().
RATE_LIMIT_DIRECTIVES = rate_limit_directives()

# --- КОНФИГУРАЦИЯ CATCHALL И ЗАГЛУШКИ SSL ---

# Заглушка SSL для default_server (нужна, чтобы Nginx стартовал с listen 443 default_server)
DEFAULT_SSL_DIR = config.SSL_DIR / "default"
DEFAULT_CRT_PATH = DEFAULT_SSL_DIR / "default.crt"
DEFAULT_KEY_PATH = DEFAULT_SSL_DIR / "default.key"

CATCHALL_CONFIG_TEMPLATE = """server {
    listen 80 default_server;
    listen 443 ssl http2 default_server;
    server_name _;

    ssl_certificate /etc/letsencrypt/default/default.crt;
    ssl_certificate_key /etc/letsencrypt/default/default.key;

    # ACME HTTP-01: ВСЕГДА отдаём challenge из webroot, даже если для домена ещё
    # нет своего server-блока (или он не успел перезагрузиться). Иначе выпуск SSL
    # ловит 403 от этого catchall — частый footgun панельного/приложенческого SSL.
    location /.well-known/acme-challenge/ {
        root /var/www/acme_challenge;
    }

    location / {
        return 403;
    }
}
"""


def _ensure_default_ssl_files():
    """
    Проверяет наличие заглушки SSL. Если нет, генерирует самоподписанный сертификат,
    используя OpenSSL внутри контейнера Certbot.
    """
    DEFAULT_SSL_DIR.mkdir(exist_ok=True)
    if DEFAULT_CRT_PATH.exists() and DEFAULT_KEY_PATH.exists():
        return

    print("INFO: Generating self-signed default SSL using Certbot container...")

    # Команда OpenSSL для генерации сертификата (разделена на строки для читаемости)
    openssl_cmd = [
        "openssl", "req", "-x509", "-nodes", "-days", "365",
        "-newkey", "rsa:2048",
        "-keyout", "/etc/letsencrypt/default/default.key",  # Путь внутри контейнера
        "-out", "/etc/letsencrypt/default/default.crt",  # Путь внутри контейнера
        "-subj", "/CN=default.local"
    ]

    try:
        # Выполняем openssl ВНУТРИ certbot-контейнера через docker-py exec.
        exit_code, output = docker_manager.exec_in_container(
            CERTBOT_CONTAINER_NAME, openssl_cmd, user="root"
        )
        if exit_code != 0:
            raise RuntimeError(f"openssl завершился с кодом {exit_code}: {output.strip()}")
        print("SUCCESS: Default SSL files created inside Certbot container and mapped to host.")
    except Exception as e:
        print(f"ERROR: Failed to create default SSL files via Docker exec: {e}")
        raise


def _write_catchall_if_changed() -> bool:
    """Идемпотентно (пере)записывает 00-catchall.conf актуальным шаблоном.

    Шаблон включает ACME-локацию (ADR-044). Возвращает True, если файл реально
    изменился (значит нужен reload). Не трогает panel/app-конфиги.
    """
    catchall_path = config.NGINX_SITES_DIR / "00-catchall.conf"
    _ensure_default_ssl_files()
    current = catchall_path.read_text(encoding="utf-8") if catchall_path.exists() else None
    if current != CATCHALL_CONFIG_TEMPLATE:
        catchall_path.write_text(CATCHALL_CONFIG_TEMPLATE, encoding="utf-8")
        print("INFO: Catchall config written/updated.")
        return True
    return False


def _write_zones_if_changed() -> bool:
    """Идемпотентно (пере)записывает 00-zones.conf с http-зонами rate-limit (P0).

    По образцу `_write_catchall_if_changed`: сравнить с шаблоном → перезаписать при
    отличии → вернуть True, если файл реально изменился (значит нужен reload). Файл
    грузится по алфавиту ДО app/panel-конфигов — зоны определены к моменту ссылки на
    них из location-блоков приложений. Появляется и на уже установленных нодах, т.к.
    вызывается на старте деплоера и в update_panel_nginx_config.
    """
    zones_path = config.NGINX_SITES_DIR / "00-zones.conf"
    current = zones_path.read_text(encoding="utf-8") if zones_path.exists() else None
    if current != ZONES_CONFIG_TEMPLATE:
        zones_path.write_text(ZONES_CONFIG_TEMPLATE, encoding="utf-8")
        print("INFO: Rate-limit zones config written/updated.")
        return True
    return False


def ensure_acme_webroot_traversable() -> None:
    """ACME-webroot должен быть ПРОХОДИМ nginx-воркером (он работает не под root).

    Footgun: если каталог webroot создан с `umask 077` (напр. cloud-init так защищал
    `.env` и заодно зацепил `mkdir acme_challenge`), он получает режим `0700 root` →
    nginx-воркер (uid != 0) не может в него войти → **403 на ЛЮБОЙ
    `/.well-known/acme-challenge/`**, и certbot, и проверка падают, хотя конфиг nginx
    корректен. Идемпотентно выставляем `0755` на webroot и его challenge-подкаталоги.
    В каталоге только эфемерные HTTP-01 токены (их и так публично качает LE) — секретов
    нет, world-traverse безопасен. Деплоер в контейнере root → chmod доходит до хост-тома.
    """
    try:
        webroot = config.ACME_CHALLENGE_DIR
        webroot.mkdir(parents=True, exist_ok=True)
        os.chmod(webroot, 0o755)
        sub = webroot / ".well-known"
        if sub.exists():
            os.chmod(sub, 0o755)
            ch = sub / "acme-challenge"
            if ch.exists():
                os.chmod(ch, 0o755)
    except OSError as e:
        print(f"WARN: не удалось выставить права ACME-webroot ({config.ACME_CHALLENGE_DIR}): {e}")


def ensure_acme_challenge_ready() -> None:
    """Гарантирует, что nginx отдаёт ACME HTTP-01 challenge для ЛЮБОГО домена.

    Вызывается ПЕРЕД выпуском SSL (ssl_service): самоизлечивает две частые причины
    403 на `/.well-known/acme-challenge/`:
      1. устаревший/отсутствующий catchall (нода из среза ДО ADR-044) — перезапись шаблона;
      2. **непроходимый webroot** (создан с umask 077 → 0700 root) — chmod 0755.
    В отличие от `update_panel_nginx_config`, НЕ удаляет/не переписывает panel- и
    app-конфиги, поэтому безопасно дёргать в любой момент. Reload — только если
    catchall изменился (права webroot reload'а не требуют).
    """
    ensure_acme_webroot_traversable()
    # Зоны rate-limit (P0) должны существовать ДО ссылки на них из app-конфигов.
    # Пишем идемпотентно; при изменении любого из файлов делаем reload.
    zones_changed = _write_zones_if_changed()
    catchall_changed = _write_catchall_if_changed()
    if zones_changed or catchall_changed:
        reload_nginx()


def _get_proxy_headers(proxy_path: str) -> str:
    """Генерирует блок проксирования на деплоер.

    Используем resolver (встроенный DNS Docker, 127.0.0.11) + переменную в
    proxy_pass, чтобы Nginx РЕ-резолвил имя деплоера на каждый запрос. Иначе при
    рестарте контейнера деплоер получает новый IP, а Nginx держит старый
    (литеральный proxy_pass резолвится один раз при загрузке) -> кратковременный 502.

    base_path: "" для панели ("/"), "/api/proxy/<app>" для приложений. Полный URI
    добавляем через $request_uri (обязательно при переменной в proxy_pass).
    """
    base_path = proxy_path.rstrip("/")

    return f"""
            resolver 127.0.0.11 valid=30s ipv6=off;
            set $deployer_upstream {DEPLOYER_HOST};
            proxy_pass http://$deployer_upstream:{DEPLOYER_PORT}{base_path}$request_uri;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 86400;
    """


def update_application_nginx_config(
        app_name: str,
        domain: str,
        ssl_cert_name: Optional[str] = None,
        rate_limit: Optional[dict] = None,
):
    """Генерирует конфиг для пользовательского приложения.

    `rate_limit` (P1/PRO демо-фича `rate_limit_ui`): per-app override `{burst, conn,
    body_size}` поверх P0-дефолтов. None → дефолтный «предохранитель» P0 (обычный путь
    ядра/OSS). Значения приезжают из `data/pro/rate_limits.json` через PRO-роутер под
    валидной лицензией — само ядро дефолты не меняет.
    """
    config_path = config.NGINX_SITES_DIR / f"{app_name}.conf"

    # Путь для проксирования приложений
    app_proxy_path = f"/api/proxy/{app_name}/"
    proxy_headers = _get_proxy_headers(app_proxy_path)

    rl = rate_limit or {}
    directives = rate_limit_directives(
        burst=rl.get("burst"), conn=rl.get("conn"), body_size=rl.get("body_size"))

    # P0: санитарный rate-limit — только в proxy-location `/`, где реально идёт
    #     трафик приложения. НЕ добавляем в ACME-location (отдельный location без
    #     proxy) — выпуск/продление SSL не лимитируется. При ssl_cert_name HTTP-блок
    #     только редиректит (301), лимиты не нужны → вешаем их на HTTPS-блок ниже.
    http_location_body = (
        "return 301 https://$host$request_uri;"
        if ssl_cert_name
        else f"{directives}\n{proxy_headers}"
    )

    # HTTP блок
    http_block = f"""
server {{
    listen 80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root /var/www/acme_challenge;
    }}

    location / {{
        {http_location_body}
    }}
}}"""

    https_block = ""
    if ssl_cert_name:
        # Учитываем, что Certbot сохраняет сертификаты в /etc/letsencrypt/live
        cert_path = f"/etc/letsencrypt/live/{ssl_cert_name}/fullchain.pem"
        key_path = f"/etc/letsencrypt/live/{ssl_cert_name}/privkey.pem"
        https_block = f"""
server {{
    listen 443 ssl;
    http2 on;
    server_name {domain};
    ssl_certificate {cert_path};
    ssl_certificate_key {key_path};

    location / {{
        {directives}
        {proxy_headers}
    }}
}}"""

    final_config = http_block.strip()
    if https_block:
        final_config += f"\n\n{https_block.strip()}"

    config_path.write_text(final_config, encoding="utf-8")
    print(f"INFO: Nginx config for '{app_name}' generated.")


def update_panel_nginx_config(domain: str = None, ssl_cert_name: str = None):
    """Генерирует конфиг для самой панели управления и catchall-ловушку."""

    # 0. Генерируем/обновляем catchall-ловушку. Пишем ВСЕГДА (не только если нет
    #    файла), чтобы улучшения шаблона (напр. ACME-локация) применялись на
    #    существующих установках при следующем сохранении настроек панели.
    #    Reload здесь не делаем — его выполнит caller (общий для catchall и panel).
    #    Заодно чиним права webroot (umask-077 footgun) — ещё на старте деплоера.
    ensure_acme_webroot_traversable()
    # P0: гарантируем наличие http-зон rate-limit (появляются и на старых нодах при
    #     следующем сохранении настроек панели). Reload — общий, его сделает caller.
    _write_zones_if_changed()
    _write_catchall_if_changed()

    panel_config_path = config.NGINX_SITES_DIR / "10-panel.conf"
    initial_config_path = config.NGINX_SITES_DIR / "99-initial-access.conf"  # Временный конфиг для IP

    proxy_headers = _get_proxy_headers(proxy_path="/")

    # Панель проксирует POST /api/blueprints/{id}/artifacts (публикация проекта ЛК:
    # мультифайл-сайт с картинками — легально до ~32 МБ: PUBLISH_MAX_BYTES текста +
    # PUBLISH_MAX_ASSET_BYTES ассетов). Без client_max_body_size у location панели
    # nginx берёт дефолт 1 МБ и режет ZIP «413 Request Entity Too Large» ДО FastAPI
    # (у деплоера MAX_ARTIFACT_BYTES=150 МБ, он не при чём). Ставим CLIENT_MAX_BODY_SIZE
    # (100 МБ): ≥ ЛК-максимума ~32 МБ и ≤ потолка деплоера 150 МБ — цепочка согласована.
    #
    # ⚠️ Но панель — НЕ только доверенный трафик ЛК: домен панели обслуживает и
    # ПУБЛИЧНЫЙ неаутентифицированный POST /api/auth/token (форма спулится ДО
    # login_limiter — тот считает лишь неудачи логина, размер/частоту тела не режет).
    # Поднять потолок тела до 100 МБ без conn/req-лимита = усилить flood/slow-body-DoS
    # на публичный вход (app-домены при том же 100m прикрыты limit_req/limit_conn —
    # держим панель симметрично). Санитарный предохранитель ЛК не мешает: он не шлёт
    # 30+ r/s и не держит 40+ конн/IP, а аноним-флудер упрётся. Зоны app_rl/app_conn
    # объявлены в 00-zones.conf (грузится ДО 10-panel.conf), reload делает caller.
    panel_body_limit = (
        f"limit_req zone=app_rl burst={RATE_LIMIT_BURST} nodelay; "
        f"limit_conn app_conn {RATE_LIMIT_CONN}; "
        f"client_max_body_size {CLIENT_MAX_BODY_SIZE};"
    )

    # --- ЛОГИКА ГЕНЕРАЦИИ ---
    if not domain:
        # Если домен не указан, создаем временный конфиг для доступа по IP
        # Примечание: Этот конфиг теперь не default_server, т.к. catchall уже его занял.
        # Он нужен, если пользователь хочет получить доступ по порту, но без Nginx проксирования.
        # Однако, поскольку панель работает через Nginx, мы должны обеспечить доступ по IP.

        # Чтобы не конфликтовать с catchall, нужно удалить panel.conf
        if panel_config_path.exists():
            panel_config_path.unlink()

        # Возвращаем старый IP-конфиг, но без 'default_server'
        content = f"""
server {{
    listen 80;
    server_name _; # Используем _ для ловли по IP, но catchall имеет приоритет
    location /.well-known/acme-challenge/ {{ root /var/www/acme_challenge; }}
    location / {{ {panel_body_limit} {proxy_headers} }}
}}"""
        # ВНИМАНИЕ: В этой схеме доступ по IP будет ловиться catchall.conf (возврат 403).
        # Чтобы разрешить доступ по IP, нужно либо:
        # а) удалить catchall при отсутствии домена, либо
        # б) удалить Nginx и слушать 7999 напрямую.

        # Для безопасности MVP (чтобы исключить битый SSL), оставляем catchall,
        # и удаляем panel.conf. Панель будет недоступна по IP через Nginx,
        # но будет доступна, если обращаться к 7999 напрямую (если порты проброшены).

        # Удалили panel.conf выше. Завершаем.
        if initial_config_path.exists():
            initial_config_path.unlink()  # Удаляем старый конфиг

        print("INFO: Panel domain not set. Access via IP/old domain is blocked by catchall (403).")
        return

    # Если домен УКАЗАН, создаем основной конфиг для него
    if ssl_cert_name:
        cert_path = f"/etc/letsencrypt/live/{ssl_cert_name}/fullchain.pem"
        key_path = f"/etc/letsencrypt/live/{ssl_cert_name}/privkey.pem"
        content = f"""
server {{
    listen 80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root /var/www/acme_challenge; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
server {{
    listen 443 ssl;
    http2 on;
    server_name {domain};
    ssl_certificate {cert_path};
    ssl_certificate_key {key_path};
    location / {{ {panel_body_limit} {proxy_headers} }}
}}"""
    else:
        # Только HTTP для заданного домена
        content = f"""
server {{
    listen 80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root /var/www/acme_challenge; }}
    location / {{ {panel_body_limit} {proxy_headers} }}
}}"""

    panel_config_path.write_text(content.strip(), encoding="utf-8")

    # Удаляем временные/устаревшие конфиги, чтобы избежать конфликтов
    if initial_config_path.exists():
        initial_config_path.unlink()

    print(f"INFO: Panel config for '{domain}' generated.")


def reload_nginx():
    """Проверяет конфиг и перезагружает Nginx (через docker-py exec, без docker-cli)."""
    try:
        # 1. Тест конфига внутри nginx-контейнера
        test_code, test_out = docker_manager.exec_in_container(
            config.NGINX_CONTAINER_NAME, config.NGINX_TEST_CMD
        )
        if test_code != 0:
            print(f"ERROR: Nginx config test failed:\n{test_out}")
            raise Exception(f"Nginx config test failed: {test_out.strip()}")

        # 2. Перезагрузка
        reload_code, reload_out = docker_manager.exec_in_container(
            config.NGINX_CONTAINER_NAME, config.NGINX_RELOAD_CMD
        )
        if reload_code != 0:
            raise Exception(f"Nginx reload failed: {reload_out.strip()}")
        print("INFO: Nginx reloaded successfully.")
    except Exception as e:
        print(f"ERROR: Could not reload Nginx: {e}")
        # Если тест прошел, а reload упал (что маловероятно), нужно сообщить об этом.


def remove_application_nginx_config(app_name: str):
    config_path = config.NGINX_SITES_DIR / f"{app_name}.conf"
    if config_path.exists():
        config_path.unlink()


def get_deployer_host_for_nginx() -> str:
    return DEPLOYER_HOST