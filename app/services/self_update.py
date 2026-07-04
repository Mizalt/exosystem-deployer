"""Самообновление деплоера: updater-джоба на хосте + build-first + авто-откат (Ночь 11, ADR-071).

Проблема: деплоер живёт В КОНТЕЙНЕРЕ, собранном из install-каталога хоста
(`docker compose up -d --build`), и не может пересоздать сам себя — при `up -d`
его процесс умирает до завершения свопа. Поэтому своп делает **одноразовый
updater-контейнер**: он запускается из ТЕКУЩЕГО образа деплоера (образ уже есть
локально, pull не нужен; в python-slim есть `nsenter` из util-linux) с
`privileged + pid=host` и через `nsenter -t 1` исполняет shell-скрипт в
неймспейсах ХОСТА — там штатные `git` и `docker compose`, т.е. буквально
автоматизация ручного ранбука «git pull && docker compose up -d --build deployer».

Гарантии скрипта (мирроринг ADR-022 build-first):
  1. новый образ собирается ДО свопа — провал сборки не трогает работающий контейнер;
  2. после свопа — health-гейт (контейнер running + HTTP 200 изнутри);
  3. провал health → авто-откат: git checkout прежнего коммита + пересборка.

Результат скрипт пишет в `data/update_state.json` (том `data/` переживает
пересоздание контейнера) — его читают `/api/version` (git_sha/статус) и роут
отката (`previous_ref`). Прогресс задачи ведёт `PendingAction` типа `self_update`
(app/services/pending_actions.py): состояние в БД, переживает своп процесса.
"""
from __future__ import annotations

import json
import os
import posixpath

from app import config
from app.environment import get_docker_client

# Имя собственного контейнера деплоера (фиксировано в docker-compose.yml).
SELF_CONTAINER = os.environ.get("DEPLOYER_SELF_CONTAINER", "deployer")
UPDATER_CONTAINER = "deployer-updater"
UPDATE_STATE_FILE = config.BASE_DIR / "data" / "update_state.json"

# Скрипт исполняется на ХОСТЕ (nsenter в неймспейсы pid 1) от root. Параметры —
# через env контейнера (nsenter наследует окружение процесса). Коды выхода —
# контракт с обработчиком PendingAction: 0 = обновлено/уже актуально,
# 2 = предусловия (git/каталог/ref), 3 = сборка провалилась (ничего не тронуто),
# 42 = health-провал или своп-провал → выполнен авто-откат.
HOST_SCRIPT = r'''
set -u
cd "$INSTALL_DIR" || { echo "E: install-каталог не найден: $INSTALL_DIR"; exit 2; }
command -v git >/dev/null 2>&1 || { echo "E: git не найден на хосте"; exit 2; }
COMPOSE="docker compose"; docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"
PREV=$(git rev-parse HEAD) || { echo "E: не git-репозиторий"; exit 2; }
echo "PREV_REF=$PREV"
git fetch --tags --force origin 2>&1 || echo "W: git fetch не удался (offline?) — пробую локальный ref"
if [ -n "${REF:-}" ]; then
  git checkout -f "$REF" 2>&1 || { echo "E: ref не найден: $REF"; exit 2; }
  git merge --ff-only "origin/$REF" 2>&1 || true
else
  BR=$(git rev-parse --abbrev-ref HEAD); [ "$BR" = "HEAD" ] && BR=main
  git checkout -f "$BR" 2>&1
  git merge --ff-only "origin/$BR" 2>&1 || { echo "E: fast-forward не удался (локальные правки в install-каталоге?)"; exit 2; }
fi
NEW=$(git rev-parse HEAD)
echo "NEW_REF=$NEW"
if [ "$NEW" = "$PREV" ]; then echo "ALREADY_UP_TO_DATE"; exit 0; fi
echo "Собираю новый образ (build-first: работающий контейнер не тронут)..."
$COMPOSE build deployer 2>&1 || { echo "BUILD_FAILED"; git checkout -f "$PREV" 2>&1; exit 3; }
echo "Переключаю контейнер..."
$COMPOSE up -d deployer 2>&1 || {
  echo "SWAP_FAILED — откатываюсь на $PREV";
  git checkout -f "$PREV" 2>&1; $COMPOSE up -d --build deployer 2>&1;
  printf '{"current_ref":"%s","failed_ref":"%s","status":"rolled_back","updated_at":"%s"}\n' \
    "$PREV" "$NEW" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > data/update_state.json 2>/dev/null || true
  exit 42; }
i=0; ok=0
while [ $i -lt 18 ]; do
  sleep 5; i=$((i+1))
  st=$(docker inspect -f '{{.State.Status}}' "$SELF" 2>/dev/null || echo missing)
  if [ "$st" = "running" ] && docker exec "$SELF" python -c 'import urllib.request,sys;sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:7999/",timeout=4).status==200 else 1)' >/dev/null 2>&1; then
    ok=$((ok+1)); [ $ok -ge 2 ] && break
  else
    ok=0
  fi
done
if [ $ok -ge 2 ]; then
  printf '{"current_ref":"%s","previous_ref":"%s","status":"updated","updated_at":"%s"}\n' \
    "$NEW" "$PREV" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > data/update_state.json 2>/dev/null || true
  echo "UPDATE_OK"; exit 0
fi
echo "HEALTH_FAILED — откатываюсь на $PREV"
git checkout -f "$PREV" 2>&1
$COMPOSE build deployer 2>&1 && $COMPOSE up -d deployer 2>&1
printf '{"current_ref":"%s","failed_ref":"%s","status":"rolled_back","updated_at":"%s"}\n' \
  "$PREV" "$NEW" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > data/update_state.json 2>/dev/null || true
echo "ROLLED_BACK"; exit 42
'''


class SelfUpdateError(Exception):
    """Понятная причина, почему обновление нельзя запустить (уходит в 400/лог задачи)."""


def read_update_state() -> dict:
    """Содержимое data/update_state.json (пишет updater-скрипт). Пусто — если обновлений
    ещё не было. Терпимо к мусору (файл пишется best-effort шеллом)."""
    try:
        return json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --- Журнал версий + страж отката (Ночь 16, ADR-085) -------------------------- #

UPDATE_HISTORY_FILE = config.BASE_DIR / "data" / "update_history.json"
HISTORY_MAX_ENTRIES = 50


def read_update_history() -> list[dict]:
    """Журнал обновлений/откатов (старые → новые). Пишет финализация задачи
    `self_update` (НОВЫЙ процесс после свопа — он знает свою версию). Пусто —
    нода ни разу не обновлялась через ЛК (или обновлялась руками мимо журнала)."""
    try:
        data = json.loads(UPDATE_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def append_update_history(entry: dict) -> None:
    """Дописывает запись журнала (best-effort: журнал не важнее самого обновления)."""
    try:
        history = read_update_history()
        history.append(entry)
        UPDATE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_HISTORY_FILE.write_text(
            json.dumps(history[-HISTORY_MAX_ENTRIES:], ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: [self-update] журнал версий не записан: {e}")


def rollback_target_version() -> str | None:
    """Версия, на которую вернёт откат (`previous_ref`), — из журнала.

    `previous_ref` выставляется ТОЛЬКО успешным обновлением, поэтому версия цели
    отката = `from_version` последней записи со статусом `updated`. None — журнала
    нет (обновляли руками/до Ночи 16): версия цели неизвестна.
    """
    if not read_update_state().get("previous_ref"):
        return None
    for entry in reversed(read_update_history()):
        if entry.get("status") == "updated":
            return entry.get("from_version") or None
    return None


def rollback_guard() -> tuple[bool, str | None, str | None]:
    """Страж несовместимого отката → (allowed, target_version, reason).

    Миграции forward-only: откат ниже `MIN_COMPATIBLE_VERSION` запрещён с
    объяснением (например, ниже 0.11.0 нода теряет сам механизм обновления).
    Версия цели неизвестна (нет журнала) → пропускаем с пометкой: откат —
    инструмент восстановления, блокировать его без доказательств опаснее.
    """
    from app import version as version_mod

    state = read_update_state()
    if not state.get("previous_ref"):
        return False, None, "Нет сохранённой предыдущей версии (нода ещё не обновлялась через ЛК)."
    target = rollback_target_version()
    if target and (version_mod.as_tuple(target)
                   < version_mod.as_tuple(version_mod.MIN_COMPATIBLE_VERSION)):
        return False, target, (
            f"Откат на v{target} запрещён: ниже минимально совместимой "
            f"v{version_mod.MIN_COMPATIBLE_VERSION}. Миграции ноды forward-only — "
            "старая версия не умеет обновляться из ЛК и не поймёт новую схему данных.")
    return True, target, None


def _self_container(client):
    try:
        return client.containers.get(SELF_CONTAINER)
    except Exception:
        raise SelfUpdateError(
            f"Контейнер деплоера «{SELF_CONTAINER}» не найден — самообновление доступно "
            "только в контейнерной установке (docker compose).")


def host_install_dir(client=None) -> str:
    """Install-каталог на ХОСТЕ: родитель host-пути тома `/app/data` собственного
    контейнера (данные всегда монтируются из `<install>/data`, docker-compose.yml)."""
    client = client or get_docker_client()
    me = _self_container(client)
    for m in me.attrs.get("Mounts", []):
        if m.get("Destination") == "/app/data" and m.get("Source"):
            return posixpath.dirname(m["Source"].replace("\\", "/"))
    raise SelfUpdateError("Не удалось определить install-каталог хоста "
                          "(том /app/data не найден у контейнера деплоера).")


def precheck() -> str | None:
    """Быстрая проверка предусловий для enqueue-роута. None = можно; иначе причина."""
    try:
        host_install_dir()
        return None
    except SelfUpdateError as e:
        return str(e)


def launch_updater(ref: str | None) -> None:
    """Запускает одноразовый updater-контейнер (detach). Бросает SelfUpdateError."""
    client = get_docker_client()
    me = _self_container(client)
    install_dir = host_install_dir(client)

    # Прошлый updater: работающий = параллельное обновление (нельзя), мёртвый — убрать.
    try:
        old = client.containers.get(UPDATER_CONTAINER)
        if old.status == "running":
            raise SelfUpdateError("Обновление уже выполняется (updater-контейнер активен).")
        old.remove(force=True)
    except SelfUpdateError:
        raise
    except Exception:  # noqa: BLE001 — NotFound и прочее: старого updater нет
        pass

    client.containers.run(
        me.image,
        name=UPDATER_CONTAINER,
        command=["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--",
                 "/bin/sh", "-c", HOST_SCRIPT],
        environment={"REF": ref or "", "INSTALL_DIR": install_dir, "SELF": SELF_CONTAINER},
        privileged=True,
        pid_mode="host",
        network_mode="none",  # сеть контейнеру не нужна: nsenter -n уводит скрипт в сеть хоста
        detach=True,
        labels={"deployer.role": "self-updater"},
    )


def updater_status() -> tuple[str, int | None, str]:
    """(state, exit_code, logs): state ∈ running|exited|missing. Логи — хвост 4000."""
    client = get_docker_client()
    try:
        cont = client.containers.get(UPDATER_CONTAINER)
    except Exception:  # noqa: BLE001 — NotFound
        return "missing", None, ""
    try:
        logs = cont.logs(tail=200).decode("utf-8", errors="replace")[-4000:]
    except Exception:  # noqa: BLE001
        logs = ""
    if cont.status == "running":
        return "running", None, logs
    code = (cont.attrs.get("State") or {}).get("ExitCode")
    return "exited", code, logs


def cleanup_updater() -> None:
    """Убирает завершившийся updater-контейнер (best-effort)."""
    try:
        get_docker_client().containers.get(UPDATER_CONTAINER).remove(force=True)
    except Exception:  # noqa: BLE001
        pass
