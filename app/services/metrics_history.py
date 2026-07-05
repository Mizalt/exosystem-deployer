"""История системных метрик хоста для графиков дашборда (Ночь 19).

«Показатели ЦП/память/диск должны быть графиком с динамикой» — сам по себе
`GET /api/host/health` отдаёт только текущий снимок. Здесь лёгкий фоновый
сэмплер (asyncio-цикл рядом с оркестратором): раз в минуту снимает дешёвый
снимок хоста (`host_health` — чтение /proc, без docker stats) и складывает в
кольцевой буфер на ~24 часа. Дашборд рисует спарклайны из
`GET /api/system/metrics/history`.

Буфер живёт в памяти и периодически сбрасывается в `data/metrics_history.json`
(переживает рестарт контейнера; потеря последних минут при жёстком падении —
приемлемо для графика нагрузки). Никаких внешних зависимостей и тяжёлых вызовов:
сэмпл — микросекунды чтения procfs, поэтому интервал в 1 минуту не грузит хост.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.services import host_health

SAMPLE_INTERVAL_SEC = 60
MAX_POINTS = 1440           # 24 часа по минуте
PERSIST_EVERY = 10          # сбрасываем на диск раз в ~10 минут
STORE_PATH = Path("data") / "metrics_history.json"

# Кольцевой буфер точек [t, cpu%, mem%, disk%] (t — epoch, значения могут быть
# None на dev-Windows без /proc — фронт такие точки пропускает).
_points: list[list] = []
_loaded = False


def _load() -> None:
    """Подхватывает историю с диска (однократно, при первом обращении/старте)."""
    global _points, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            _points = [p for p in raw if isinstance(p, list) and len(p) == 4][-MAX_POINTS:]
    except (OSError, ValueError):
        _points = []


def _persist() -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.write_text(json.dumps(_points), encoding="utf-8")
    except OSError as e:  # noqa: PERF203 — диск может быть переполнен: не роняем цикл
        print(f"WARN: [METRICS] не удалось сохранить историю: {e}")


def sample_point(now: float | None = None) -> list:
    """Одна точка [t, cpu%, mem%, disk%]. ЦП — load(1m)/cpu_count (кап 100),
    как в ЛК: сопоставимые графики в обоих UI."""
    import os
    import time

    t = int(now if now is not None else time.time())
    load = host_health.read_loadavg()
    cpu_n = os.cpu_count()
    cpu_pct = None
    if load and cpu_n:
        cpu_pct = min(100.0, round(load[0] / cpu_n * 100, 1))
    meminfo = host_health.read_meminfo() or {}
    mem_pct = (meminfo.get("memory") or {}).get("used_pct")
    disk_pct = (host_health.disk_usage() or {}).get("used_pct")
    return [t, cpu_pct, mem_pct, disk_pct]


def record_sample(now: float | None = None) -> None:
    """Снимает точку и кладёт в буфер (вызывается циклом; отдельно — для тестов)."""
    _load()
    point = sample_point(now)
    if point[1] is None and point[2] is None and point[3] is None:
        return  # dev-хост без /proc: пустые точки историю не засоряют
    _points.append(point)
    del _points[:-MAX_POINTS]


def history(minutes: int = 1440) -> dict:
    """Срез истории для API: точки за последние `minutes` минут."""
    import time

    _load()
    minutes = max(5, min(minutes, 1440))
    since = time.time() - minutes * 60
    pts = [p for p in _points if p[0] >= since]
    return {"points": pts, "interval_sec": SAMPLE_INTERVAL_SEC}


async def run_metrics_history_loop() -> None:
    """Фоновый цикл сэмплера (lifespan, рядом с оркестратором)."""
    _load()
    print("INFO: [METRICS] History sampler started.")
    ticks = 0
    while True:
        try:
            record_sample()
            ticks += 1
            if ticks % PERSIST_EVERY == 0:
                _persist()
        except Exception as e:  # noqa: BLE001 — сэмплер не должен умирать
            print(f"ERROR: [METRICS] сэмплер: {e}")
        await asyncio.sleep(SAMPLE_INTERVAL_SEC)
