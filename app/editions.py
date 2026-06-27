"""Слой редакций (open-core): одна кодовая база → несколько изданий.

Цель — НЕ держать «несколько разных проектов». Издание выбирается переменной
окружения ``DEPLOYER_EDITION`` (``oss`` | ``pro`` | ``cloud``); ядро остаётся
edition-agnostic и спрашивает доступность фичи через :func:`is_feature_enabled`.

Коммерческий код живёт в отдельных каталогах (``app/pro/``, ``app/cloud/``),
которые **вырезаются из публичного среза** (см. ``AGENTS.md`` и
``docs/10_EDITIONS.md``) ровно так же, как внутренние доки. Стратегия и ADR —
``docs/10_EDITIONS.md`` / ``docs/05_DECISIONS.md`` (ADR-019).

Модуль намеренно не зависит от остального ``app`` (нет циклов) и читает окружение
лениво при каждом вызове — это удобно для тестов (выставил env → проверил).
"""

import os

OSS = "oss"
PRO = "pro"
CLOUD = "cloud"
VALID_EDITIONS = (OSS, PRO, CLOUD)

# Иерархия: cloud ⊇ pro ⊇ oss. Каждое издание включает фичи предыдущих уровней.
_INCLUDES = {
    OSS: frozenset({OSS}),
    PRO: frozenset({OSS, PRO}),
    CLOUD: frozenset({OSS, PRO, CLOUD}),
}

# Минимальный уровень издания для фичи. Ядро/OSS-фичи здесь НЕ перечисляем —
# по умолчанию неизвестная фича считается OSS-уровня (доступна всем). Здесь —
# только платные возможности (Этап 3-4 роадмапа). Список расширяется по мере роста.
_FEATURE_TIER = {
    # Pro (open-core, платно при self-host):
    "roles": PRO,                 # роли деплоера (Super-Admin/Admin/Developer/Viewer)
    "protected_mode": PRO,        # аутентификатор опубликованных приложений
    "audit_log": PRO,             # журнал действий администратора
    "backups": PRO,               # бэкапы БД/состояния
    "priority_support": PRO,
    # Cloud (managed, наш хостинг):
    "multi_tenancy": CLOUD,       # изоляция арендаторов
    "billing": CLOUD,             # тарификация/подписки
    "managed_dns": CLOUD,         # управляемый DNS/домены
    "control_plane": CLOUD,       # control-plane облака (провижининг VPS и т.п.)
}


def get_edition() -> str:
    """Текущее издание из ``DEPLOYER_EDITION`` (по умолчанию и при мусоре — ``oss``)."""
    edition = (os.environ.get("DEPLOYER_EDITION") or OSS).strip().lower()
    return edition if edition in VALID_EDITIONS else OSS


def edition_includes(edition: str) -> frozenset:
    """Множество уровней, которые покрывает данное издание."""
    return _INCLUDES.get(edition, _INCLUDES[OSS])


def is_feature_enabled(feature: str, edition: str | None = None) -> bool:
    """Доступна ли фича в текущем (или указанном) издании.

    Неизвестная фича считается OSS-уровня → доступна всем (fail-open для ядра,
    платные фичи перечислены явно в ``_FEATURE_TIER``).
    """
    edition = edition or get_edition()
    tier = _FEATURE_TIER.get(feature, OSS)
    return tier in edition_includes(edition)


def describe() -> dict:
    """Сводка издания и фич — для startup-лога и эндпоинта ``/api/edition``."""
    edition = get_edition()
    return {
        "edition": edition,
        "tiers": sorted(edition_includes(edition)),
        "features": {f: is_feature_enabled(f, edition) for f in sorted(_FEATURE_TIER)},
    }
