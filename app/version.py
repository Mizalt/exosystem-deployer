"""Идентичность версии деплоера + capability-negotiation (Ночь 11, ADR-071).

ЛК должен работать со ВСЕМИ версиями нод. Совместимость строим НЕ на сравнении
номеров версий, а на списке **capabilities** — стабильных строк-фич, которые
поддерживает данная сборка. Нода отдаёт их публично (`GET /api/version`), а ЛК
гейтит кнопки/действия по наличию нужной capability (`node.supports(cap)`), не по
номеру версии. Это снимает боль «старые ноды на отстающем публичном срезе» без
комбинаторной матрицы «версия ЛК × версия ноды».

Модуль зависит только от :mod:`app.editions` (тоже без циклов) и читает окружение
лениво — удобно для тестов (выставил env → проверил).
"""
import os

from app import editions

# Semver сборки деплоера. Бампать при значимых изменениях API/поведения. На проде
# может переопределяться env `DEPLOYER_VERSION` (напр. проставляется при сборке образа).
VERSION = "0.19.2"

# Минимальная версия, на которую БЕЗОПАСЕН откат из UI (страж даунгрейда, Ночь 16,
# ADR-085). Миграции ноды forward-only: ниже 0.11.0 нет самого механизма
# обновления (updater-джоба + задача self_update, ADR-071) — откат туда лишает
# ноду возможности обновиться обратно из ЛК (только руками по SSH). Бампать при
# несовместимых изменениях схемы/контрактов.
MIN_COMPATIBLE_VERSION = "0.11.0"

# Capabilities ЭТОЙ сборки — стабильные строки-фичи. Список ТОЛЬКО пополняем
# (аддитивно): существующие строки не переименовываем и не удаляем, иначе старые
# ЛК/ноды перестанут их понимать (см. договор совместимости ADR-071). ЛК держит
# реестр «фича ЛК → нужная capability» и гейтит UI по этому списку.
CAPABILITIES = (
    "version",          # сам этот эндпоинт (нода умеет отдавать версию/capabilities)
    "pending_actions",  # фоновые задачи публикации/SSL, центр задач (ADR-069)
    "apex_publish",     # публикация на apex/«@» + заявки на любой домен (ADR-070)
    "dns_requests",     # очередь заявок на A-записи от ЛК (ADR-057)
    "sso_redeem",       # SSO Phase 2: POST /api/sso/redeem (cpk, ADR-067)
    "admin_recover",    # восстановление доступа: POST /api/admin/recover (cpk, ADR-067)
    "github_import",    # импорт версий из GitHub, в т.ч. приватных (ADR-033/055)
    "advanced_build",   # расширенный режим сборки: база/команда/порт/env (ADR-021)
    "replicas_scale",   # масштабирование реплик + round-robin в прокси (ADR-020)
    "self_update",      # самообновление/откат: POST /api/admin/{update,rollback} (cpk, ADR-071)
    "host_health",      # здоровье хоста: GET /api/host/health (диск/RAM/swap/load, Ночь 13)
    "op_metrics",       # замеры операций: GET /api/operation-metrics + стадии/ETA задач (Ночь 14)
    "ssl_renewal",      # автопродление SSL + GET /api/ssl/expiring (алерты сроков, Ночь 16)
    "update_info",      # история версий + страж отката: GET /api/admin/update-info (Ночь 16)
    "metrics_history",  # история метрик хоста: GET /api/system/metrics/history (Ночь 19)
    "terminal_exec",    # веб-терминал: POST /api/admin/exec (одна команда → вывод, ADR-090)
    "panel_embed",      # панель внутри ЛК: CSP frame-ancestors по пушу origin (ADR-092)
    "rate_limit",       # санитарный rate-limit nginx в ядре: зоны+limit_req/conn (P0, ADR-099)
    "pro_license",      # приём/рефреш/отзыв PRO-лицензии: POST /api/pro/license[/revoke] (P1, ADR-100)
    "panel_ai",         # ИИ-помощник панели: GET /api/panel/ai-availability + embedded-виджет (ADR-103)
)


def as_tuple(v: str | None) -> tuple[int, ...]:
    """Semver-строка → кортеж для сравнения («0.13.1» < «0.14.0»). Терпима к мусору:
    нечисловые куски отбрасываются, пусто → (0,) (сравнение не падает никогда)."""
    parts = []
    for chunk in (v or "").strip().lstrip("v").split("."):
        digits = ""
        for ch in chunk:  # только ведущие цифры сегмента: «0-rc1» → 0
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def get_version() -> str:
    """Текущая версия сборки (env `DEPLOYER_VERSION` переопределяет константу)."""
    return (os.environ.get("DEPLOYER_VERSION") or VERSION).strip()


def git_sha() -> str | None:
    """Короткий git-SHA сборки, если проставлен в окружении при провиженинге/сборке."""
    sha = (os.environ.get("DEPLOYER_GIT_SHA") or "").strip()
    return sha or None


def capabilities() -> list[str]:
    """Отсортированный список capabilities этой сборки (детерминированно для тестов/UI)."""
    return sorted(CAPABILITIES)


def supports(capability: str) -> bool:
    """Поддерживает ли ЭТА сборка деплоера данную capability."""
    return capability in CAPABILITIES


def describe() -> dict:
    """Сводка версии/возможностей — для публичного `GET /api/version` и лога старта.

    Публично и без секретов: только несекретные метаданные сборки (версия, SHA,
    издание, capabilities), чтобы ЛК мог согласовать совместимость до/без входа.
    """
    return {
        "version": get_version(),
        "git_sha": git_sha(),
        "edition": editions.get_edition(),
        "capabilities": capabilities(),
    }
