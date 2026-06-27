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
        ssl_cert_name: Optional[str] = None
):
    """Генерирует конфиг для пользовательского приложения."""
    config_path = config.NGINX_SITES_DIR / f"{app_name}.conf"

    # Путь для проксирования приложений
    app_proxy_path = f"/api/proxy/{app_name}/"
    proxy_headers = _get_proxy_headers(app_proxy_path)

    # HTTP блок
    http_block = f"""
server {{
    listen 80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root /var/www/acme_challenge;
    }}

    location / {{
        {'return 301 https://$host$request_uri;' if ssl_cert_name else proxy_headers}
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
    catchall_path = config.NGINX_SITES_DIR / "00-catchall.conf"
    _ensure_default_ssl_files()
    current_catchall = catchall_path.read_text(encoding="utf-8") if catchall_path.exists() else None
    if current_catchall != CATCHALL_CONFIG_TEMPLATE:
        catchall_path.write_text(CATCHALL_CONFIG_TEMPLATE, encoding="utf-8")
        print("INFO: Catchall config written/updated.")

    panel_config_path = config.NGINX_SITES_DIR / "10-panel.conf"
    initial_config_path = config.NGINX_SITES_DIR / "99-initial-access.conf"  # Временный конфиг для IP

    proxy_headers = _get_proxy_headers(proxy_path="/")

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
    location / {{ {proxy_headers} }}
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
    location / {{ {proxy_headers} }}
}}"""
    else:
        # Только HTTP для заданного домена
        content = f"""
server {{
    listen 80;
    server_name {domain};
    location /.well-known/acme-challenge/ {{ root /var/www/acme_challenge; }}
    location / {{ {proxy_headers} }}
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