"""Здоровье хоста для UI (Ночь 13, `21_HOST_OPS.md` волна 1, уровень A).

«Владелец видит здоровье своего сервера, не заходя по SSH»: лёгкий снимок
диск/память/swap/load/uptime/docker БЕЗ внешних зависимостей (никакого psutil) —
читаем `/proc` и спрашиваем docker-py. Панель показывает виджет на дашборде,
ЛК зеркалит в карточку сервера (capability `host_health`).

Почему это работает из контейнера: `/proc/meminfo`, `/proc/loadavg`,
`/proc/uptime` в procfs НЕ неймспейсятся — контейнер видит значения ХОСТА
(что нам и нужно: OOM-thrash из инцидента ADR-078 — хостовая беда).
`shutil.disk_usage("/")` в контейнере меряет файловую систему, на которой
лежит overlay (та же партиция, что `/var/lib/docker`) — заполненный диск
хоста виден именно там (инцидент ADR-078: 100% диска dangling-образами).

Все функции чистые и терпимые к отсутствию источника (Windows-dev без /proc,
недоступный Docker): недостающие блоки — None, эндпоинт всегда отвечает 200.
Пороги внимания считаются ЗДЕСЬ (warnings + status), чтобы панель и ЛК
показывали одинаковую правду и не дублировали логику порогов в двух UI.
"""
from __future__ import annotations

import shutil

# Пороги внимания (доли занятости, %): согласованы с `21_HOST_OPS.md` §3.1.
WARN_PCT = 80.0
CRIT_PCT = 92.0


def _read_first_line(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return None


def read_meminfo(path: str = "/proc/meminfo") -> dict | None:
    """Память и swap хоста из /proc/meminfo (kB → MB). None, если файла нет (dev)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = {}
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    raw[key.strip()] = int(parts[0])  # значения в kB
    except OSError:
        return None
    if "MemTotal" not in raw:
        return None
    to_mb = lambda kb: round(kb / 1024)  # noqa: E731
    total = raw.get("MemTotal", 0)
    # MemAvailable — честная «свободная» память (учитывает reclaimable кэш);
    # на очень старых ядрах её нет — грубый fallback на MemFree.
    available = raw.get("MemAvailable", raw.get("MemFree", 0))
    swap_total = raw.get("SwapTotal", 0)
    swap_free = raw.get("SwapFree", 0)
    mem = {
        "total_mb": to_mb(total),
        "available_mb": to_mb(available),
        "used_pct": round((1 - available / total) * 100, 1) if total else None,
    }
    swap = {
        "total_mb": to_mb(swap_total),
        "used_pct": (round((1 - swap_free / swap_total) * 100, 1)
                     if swap_total else None),
    }
    return {"memory": mem, "swap": swap}


def read_loadavg(path: str = "/proc/loadavg") -> list[float] | None:
    """Load average хоста [1m, 5m, 15m]. None, если /proc нет (dev)."""
    line = _read_first_line(path)
    if not line:
        return None
    try:
        return [float(x) for x in line.split()[:3]]
    except (ValueError, IndexError):
        return None


def read_uptime(path: str = "/proc/uptime") -> int | None:
    """Аптайм хоста в секундах. None, если /proc нет (dev)."""
    line = _read_first_line(path)
    if not line:
        return None
    try:
        return int(float(line.split()[0]))
    except (ValueError, IndexError):
        return None


def disk_usage(path: str = "/") -> dict | None:
    """Заполненность файловой системы (в контейнере — партиция overlay/Docker)."""
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None
    to_gb = lambda b: round(b / (1024 ** 3), 1)  # noqa: E731
    return {
        "total_gb": to_gb(du.total),
        "free_gb": to_gb(du.free),
        "used_pct": round(du.used / du.total * 100, 1) if du.total else None,
    }


def docker_summary(client) -> dict | None:
    """Короткая сводка Docker-демона (client.info() уже используется дашбордом)."""
    try:
        info = client.info()
    except Exception:  # noqa: BLE001 — демон недоступен: блок просто отсутствует
        return None
    return {
        "containers_running": info.get("ContainersRunning"),
        "images": info.get("Images"),
        "server_version": info.get("ServerVersion"),
    }


def _pct_level(used_pct) -> str:
    """ok|warn|crit по проценту занятости (общие пороги диска/памяти)."""
    if used_pct is None:
        return "ok"
    if used_pct >= CRIT_PCT:
        return "crit"
    if used_pct >= WARN_PCT:
        return "warn"
    return "ok"


def assess(health: dict) -> tuple[str, list[str]]:
    """Оценка снимка: (status ok|warn|crit, warnings-строки для бейджей UI).

    Правила из живых инцидентов (ADR-078: диск 100% + 2 ГБ RAM без swap → thrash):
      • диск/память >80% — warn, >92% — crit;
      • swap отсутствует вовсе — warn (риск OOM при сборке образа);
      • load(5m) > 2×CPU — warn (хост захлёбывается).
    Тексты — человекочитаемые, БЕЗ эмодзи (инвариант №9): UI красит по status.
    """
    warnings: list[str] = []
    levels: list[str] = []

    disk = health.get("disk") or {}
    lvl = _pct_level(disk.get("used_pct"))
    levels.append(lvl)
    if lvl != "ok":
        warnings.append(f"Диск заполнен на {disk['used_pct']:.0f}%")

    mem = health.get("memory") or {}
    lvl = _pct_level(mem.get("used_pct"))
    levels.append(lvl)
    if lvl != "ok":
        warnings.append(f"Память занята на {mem['used_pct']:.0f}%")

    swap = health.get("swap") or {}
    if swap.get("total_mb") == 0:
        levels.append("warn")
        warnings.append("Swap отсутствует — риск OOM при сборке")

    load = health.get("load")
    cpu = health.get("cpu_count")
    if load and cpu and len(load) >= 2 and load[1] > 2 * cpu:
        levels.append("warn")
        warnings.append(f"Высокая нагрузка: load {load[1]:.1f} при {cpu} CPU")

    status = "crit" if "crit" in levels else ("warn" if "warn" in levels else "ok")
    return status, warnings


def collect(docker_client=None) -> dict:
    """Полный снимок здоровья хоста для `GET /api/host/health`.

    Каждый блок независим и best-effort: недоступный источник даёт None, но
    ответ всегда собирается (эндпоинт не падает ни на Windows-dev, ни при
    лежащем Docker). Формат — `21_HOST_OPS.md` §3.1.
    """
    import os

    meminfo = read_meminfo() or {}
    health = {
        "disk": disk_usage(),
        "memory": meminfo.get("memory"),
        "swap": meminfo.get("swap"),
        "load": read_loadavg(),
        "cpu_count": os.cpu_count(),
        "uptime_sec": read_uptime(),
        "docker": docker_summary(docker_client) if docker_client is not None else None,
    }
    status, warnings = assess(health)
    health["status"] = status
    health["warnings"] = warnings
    return health
