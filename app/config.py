# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/config.py ---

from pathlib import Path

# Импортируем имена наших системных контейнеров
from .services.nginx_service import NGINX_CONTAINER_NAME, CERTBOT_CONTAINER_NAME

BASE_DIR = Path(__file__).resolve().parent.parent

NGINX_SITES_DIR = BASE_DIR / "nginx_configs"
# ВАЖНО: Certbot по умолчанию работает с /etc/letsencrypt, поэтому меняем путь
# для консистентности. Docker-проброска позаботится об остальном.
SSL_DIR = BASE_DIR / "ssl_certs"
ACME_CHALLENGE_DIR = BASE_DIR / "acme_challenge"

# Аргументы команд, выполняемых ВНУТРИ системных контейнеров через docker-py
# exec (docker_manager.exec_in_container / exec_stream_in_container). Префикс
# `docker exec <name>` больше не нужен — docker-cli из образа деплоера убран.
NGINX_RELOAD_CMD = ["nginx", "-s", "reload"]
NGINX_TEST_CMD = ["nginx", "-t"]

# База команды Certbot; полный набор аргументов формируется в ssl_service.py.
CERTBOT_CMD_BASE = ["certbot"]

# Создаем директории
NGINX_SITES_DIR.mkdir(parents=True, exist_ok=True)
SSL_DIR.mkdir(parents=True, exist_ok=True)
ACME_CHALLENGE_DIR.mkdir(parents=True, exist_ok=True)

print(f"INFO: Nginx configs directory: {NGINX_SITES_DIR}")
print(f"INFO: SSL certs directory: {SSL_DIR}")
print(f"INFO: Nginx commands target container (via docker-py exec): '{NGINX_CONTAINER_NAME}'")
print(f"INFO: Certbot commands target container (via docker-py exec): '{CERTBOT_CONTAINER_NAME}'")