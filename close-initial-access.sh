#!/usr/bin/env sh
# close-initial-access.sh — закрывает первичный доступ к панели по IP:7999 (ADR-014).
# Пересоздаёт контейнер deployer по базовому docker-compose.yml (без firstrun-
# override), снимая публикацию порта. Идемпотентно: повторный запуск безопасен.
set -eu

INSTALL_DIR="${EXOSYSTEM_DIR:-/opt/exosystem-deployer}"
cd "$INSTALL_DIR"

printf '\033[0;36m[exosystem]\033[0m Закрываю первичный доступ (порт 7999) ...\n'
# Только базовый compose-файл -> deployer пересоздаётся без published-порта.
docker compose -f docker-compose.yml up -d
printf '\033[0;36m[exosystem]\033[0m Готово. Панель доступна только по домену/HTTPS.\n'
