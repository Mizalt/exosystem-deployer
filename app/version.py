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
VERSION = "0.13.0"

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
)


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
