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

# 1b. Проверка logging-драйвера хоста (ADR-076, docs/21_HOST_OPS.md). Если дефолтный
#     драйвер не поддерживает чтение (`docker logs`), панель не сможет показать логи
#     приложений. НЕ меняем daemon.json пользователя молча (это его сервер) — только
#     предупреждаем и советуем. Наши app-контейнеры всё равно ставят json-file явно.
LOG_DRIVER=$(docker info --format '{{.LoggingDriver}}' 2>/dev/null || echo "")
case "$LOG_DRIVER" in
  json-file|local|"") : ;;  # поддерживают чтение — всё ок
  *)
    log "⚠️  Внимание: у Docker выбран logging-драйвер '$LOG_DRIVER', который не"
    log "    поддерживает чтение логов (docker logs). Рекомендуется json-file с"
    log "    ротацией — добавьте в /etc/docker/daemon.json:"
    log '        { "log-driver": "json-file", "log-opts": {"max-size":"10m","max-file":"3"} }'
    log "    и перезапустите Docker (systemctl restart docker)."
    ;;
esac

# 1c. Память/swap (ADR-078, docs/21_HOST_OPS.md). Сборка приложений (напр.
#     `npm run build`) на сервере с малым RAM и БЕЗ swap может исчерпать память и
#     подвесить весь хост. НЕ создаём swap молча (это сервер пользователя, ADR-072) —
#     предупреждаем и даём готовую команду.
RAM_MB=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')
HAS_SWAP=$(swapon --show 2>/dev/null | grep -c swap || echo 0)
if [ -n "$RAM_MB" ] && [ "$RAM_MB" -lt 3000 ] && [ "$HAS_SWAP" -eq 0 ]; then
  log "⚠️  У сервера ~${RAM_MB} МБ RAM и НЕТ swap. Сборка тяжёлых приложений может"
  log "    исчерпать память и подвесить хост. Рекомендуется добавить swap (2 ГБ):"
  log "        fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile"
  log '        echo "/swapfile none swap sw 0 0" >> /etc/fstab'
fi

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

# 5. Первичный доступ: пока домен панели не задан, открываем порт деплоера наружу
#    (firstrun-override), иначе панель за catchall-403 и домен задать негде (ADR-014).
#    Если домен уже задан (повторная установка/обновление) — НЕ переоткрываем порт.
COMPOSE_FILES="-f docker-compose.yml"
FIRSTRUN=0
if ! grep -q '"domain": *"[^"]' data/panel_settings.json 2>/dev/null; then
  COMPOSE_FILES="$COMPOSE_FILES -f docker-compose.firstrun.yml"
  FIRSTRUN=1
fi

# 6. Подъём стека
log "Собираю и запускаю стек (docker compose up -d --build) ..."
# shellcheck disable=SC2086
docker compose $COMPOSE_FILES up -d --build

# 7. Достаём одноразовый пароль администратора из логов
log "Ожидаю инициализацию деплоера ..."
i=0
while [ "$i" -lt 30 ]; do
  if docker compose $COMPOSE_FILES logs deployer 2>/dev/null | grep -q "СОЗДАН АДМИНИСТРАТОР"; then
    break
  fi
  i=$((i + 1)); sleep 1
done

echo
echo "=================================================================="
docker compose $COMPOSE_FILES logs deployer 2>/dev/null | grep -A5 "СОЗДАН АДМИНИСТРАТОР" || \
  log "Администратор уже существовал (повторная установка)."
echo "=================================================================="

# 8. Первичный доступ: как войти и как закрыть доступ после задания домена.
if [ "$FIRSTRUN" -eq 1 ]; then
  SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  log "Первичный доступ к панели ОТКРЫТ по адресу:  http://${SERVER_IP:-<IP-сервера>}:7999"
  log "Войдите, в «Настройки → Панель» задайте домен и выберите «Выпустить сертификат"
  log "сейчас» — домен и HTTPS подключатся одним действием."
  log "Затем ЗАКРОЙТЕ первичный доступ (порт 7999):"
  log "    sh $INSTALL_DIR/close-initial-access.sh"
else
  log "Готово. Домен панели уже задан — откройте https://<ваш-домен>."
fi
log "Логи:    docker compose -f $INSTALL_DIR/docker-compose.yml logs -f deployer"
log "Остановка: docker compose -f $INSTALL_DIR/docker-compose.yml down"
