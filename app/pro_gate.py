"""Точка расширения ядра → PRO-слой (OSS, всегда присутствует; ADR-100/ADR-019).

Ядро **никогда** не импортирует `app.pro` жёстко (иначе публичный срез без каталога
`app/pro/` не соберётся). Вместо этого ядро зовёт функции ЭТОГО модуля, а он лениво и
через `try/except ImportError` пытается подтянуть PRO-слой:

  • нет каталога `app/pro/` (OSS-срез)      → `load_pro()` = None → все хуки = None/no-op;
  • есть каталог, но лицензия невалидна      → `pro_feature(name)` = None (второй гейт);
  • есть каталог И валидная лицензия И editions разрешает фичу → отдаём хук из реестра.

Двойной гейт (лестница `docs/20` ступень 2): НАЛИЧИЕ бандла и ВАЛИДНОСТЬ лицензии —
независимые условия. `editions.py` знает тир фичи (PRO), а лицензия доказывает право.

**Fail-secure/OSS-инвариант:** любой сбой импорта/лицензии → ядро работает как OSS,
никаких падений. P0-rate-limit живёт в ядре (`nginx_manager`) и от этого модуля не
зависит — истечение лицензии не «раздевает» уже применённую защиту.
"""
from __future__ import annotations

import importlib
import threading

from app import editions

# Кэш результата импорта app.pro. `False` — ещё не пробовали; None — пробовали, нет
# каталога/ошибка (OSS); модуль — PRO-слой загружен. Отдельно от None, чтобы отличать
# «не пробовали» от «пробовали и нет».
_pro_module: object | bool = False
_lock = threading.Lock()


def load_pro():
    """Лениво импортирует `app.pro` (реестр PRO-хуков) или возвращает None (OSS).

    Кэширует результат: первый успешный/неуспешный импорт фиксируется. `ImportError`
    (нет каталога в срезе) → None — штатное OSS-поведение, НЕ ошибка. Любая другая
    ошибка импорта PRO-слоя тоже гасится в None (fail-secure: битый PRO не роняет ядро).
    """
    global _pro_module
    if _pro_module is not False:
        return _pro_module or None
    with _lock:
        if _pro_module is not False:  # другой поток успел
            return _pro_module or None
        try:
            _pro_module = importlib.import_module("app.pro")
        except ImportError:
            _pro_module = None  # каталога нет — чистый OSS
        except Exception:  # noqa: BLE001 — битый PRO-слой не должен ронять ядро
            _pro_module = None
    return _pro_module or None


def reset_cache() -> None:
    """Сбрасывает кэш импорта — только для тестов (симуляция наличия/отсутствия среза)."""
    global _pro_module
    with _lock:
        _pro_module = False


def pro_feature(name: str):
    """Хук PRO-фичи `name` из реестра `app.pro`, если ДВА гейта пройдены, иначе None.

    Гейт 1 (наличие): `load_pro()` вернул модуль (каталог `app/pro/` есть в дереве).
    Гейт 2 (право): PRO-слой сам проверяет валидность лицензии И editions-тир фичи
    (`app.pro.feature_hook`). Любая проблема → None → вызывающее ядро идёт по OSS-ветке.
    """
    pro = load_pro()
    if pro is None:
        return None
    try:
        return pro.feature_hook(name)
    except Exception:  # noqa: BLE001 — fail-secure: сбой гейта = фича выключена
        return None


def register_pro_routers(app) -> None:
    """Подключает PRO-роутеры к FastAPI-приложению ядра (no-op в OSS-срезе).

    Ядро зовёт это в `main.py` при старте. Нет `app/pro/` → no-op (ничего не
    регистрируется, эндпоинты `/api/pro/*` отсутствуют → 404, чистый OSS). Наличие
    каталога ≠ активная лицензия: сами роутеры гейтятся зависимостью
    `require_pro_feature(...)` внутри `app.pro` (без лицензии → 403).
    """
    pro = load_pro()
    if pro is None:
        return
    try:
        pro.register_routers(app)
    except Exception as e:  # noqa: BLE001 — сбой PRO-регистрации не валит старт ядра
        print(f"WARN: регистрация PRO-роутеров не удалась (ядро продолжает как OSS): {e!r}")


def start_pro_background_tasks() -> list:
    """Стартует фоновые PRO-таски (проверка лицензии). Пустой список в OSS-срезе.

    Ядро зовёт это в lifespan и хранит вернувшиеся asyncio-таски, чтобы отменить их на
    shutdown. Нет `app/pro/` → [] (никаких лишних тасок в OSS).
    """
    pro = load_pro()
    if pro is None:
        return []
    try:
        return list(pro.start_background_tasks())
    except Exception as e:  # noqa: BLE001 — сбой PRO-таски не валит старт ядра
        print(f"WARN: запуск PRO-фоновых задач не удался: {e!r}")
        return []


def pro_feature_available(name: str) -> bool:
    """Доступна ли PRO-фича прямо сейчас (для гейтинга UI/эндпоинта `/api/edition`).

    Не тащит сам хук — только факт «двойной гейт пройден». editions-тир учитывается
    внутри `pro_feature`/PRO-слоя. В OSS-срезе или без лицензии → False.
    """
    # Быстрый отсев по editions: если издание вообще не даёт фичу — не трогаем PRO-слой.
    if not editions.is_feature_enabled(name):
        return False
    pro = load_pro()
    if pro is None:
        return False
    try:
        return bool(pro.feature_available(name))
    except Exception:  # noqa: BLE001
        return False
