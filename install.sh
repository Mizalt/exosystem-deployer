#!/usr/bin/env sh
# install.sh — установка exosystem-deployer одной командой (Linux).
#   curl -fsSL https://raw.githubusercontent.com/Mizalt/exosystem-deployer/main/install.sh | sh
#
# Идемпотентен: повторный запуск обновляет код и пересобирает стек.
set -eu

REPO_URL="${EXOSYSTEM_REPO:-https://github.com/Mizalt/exosystem-deployer.git}"
INSTALL_DIR="${EXOSYSTEM_DIR:-/opt/exosystem-deployer}"

log()  { printf '\033[0;36m[install]\033[0m %s\n' "$1"; }
die()  { printf '\033[0;31m[install][error]\033[0m %s\n' "$1" >&2; exit 1; }

# 1. Зависимости
command -v docker >/dev/null 2>&1 || die "Docker не установлен. См. https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Не найден плагин 'docker compose'."
command -v git >/dev/null 2>&1 || die "git не установлен."

# 2. Код: клонируем или обновляем
if [ -d "$INSTALL_DIR/.git" ]; then
  log "Обновляю код в $INSTALL_DIR ..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  log "Клонирую $REPO_URL -> $INSTALL_DIR ..."
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# 3. Секрет JWT (.env) — генерируем один раз
if [ ! -f .env ]; then
  if command -v openssl >/dev/null 2>&1; then
    KEY=$(openssl rand -hex 32)
  else
    KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  fi
  printf 'DEPLOYER_SECRET_KEY=%s\n' "$KEY" > .env
  chmod 600 .env
  log "Сгенерирован .env с DEPLOYER_SECRET_KEY."
fi

# 4. Каталоги состояния (чтобы bind-mount'ы не создавались как папки от root)
mkdir -p data uploads nginx_configs ssl_certs acme_challenge

# 5. Подъём стека
log "Собираю и запускаю стек (docker compose up -d --build) ..."
docker compose up -d --build

# 6. Достаём одноразовый пароль администратора из логов
log "Ожидаю инициализацию деплоера ..."
i=0
while [ "$i" -lt 30 ]; do
  if docker compose logs deployer 2>/dev/null | grep -q "СОЗДАН АДМИНИСТРАТОР"; then
    break
  fi
  i=$((i + 1)); sleep 1
done

echo
echo "=================================================================="
docker compose logs deployer 2>/dev/null | grep -A5 "СОЗДАН АДМИНИСТРАТОР" || \
  log "Администратор уже существовал (повторная установка)."
echo "=================================================================="
log "Готово. Задайте домен панели в настройках и откройте https://<домен>."
log "Логи:    docker compose -f $INSTALL_DIR/docker-compose.yml logs -f deployer"
log "Остановка: docker compose -f $INSTALL_DIR/docker-compose.yml down"
