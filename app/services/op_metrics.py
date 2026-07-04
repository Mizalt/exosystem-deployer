"""Замеры долгих операций деплоера — `OperationMetric` (Ночь 14, ADR-082).

Обобщение идеи `ProvisionMetric` ЛК (ADR-066) на сторону ноды: «всё, что
занимает время, должно быть с замерами». Виды операций (kind, пополняем
аддитивно): build (сборка образа, пишет `build_progress`), ssl_issue (прогон
certbot), dns_wait (ожидание распространения DNS — честно непредсказуемая),
self_update (самообновление ноды, ADR-071).

По средним прошлых прогонов UI показывает ETA («сколько осталось (оценка)»),
а супер-админка ЛК — аналитику бутылочных горлышек (зеркало op-stats через
`GET /api/operation-metrics`, capability `op_metrics`).
"""
from __future__ import annotations

import json

from app import crud
from app.database import SessionLocal

# Дефолтные ориентиры длительности (сек), пока своих замеров мало (первые
# операции свежей ноды). Из живых прогонов: сборка питон-образа ~1-3 мин,
# certbot ~0.5-2 мин, self-update ~3-8 мин. dns_wait в ETA не участвует —
# фаза принципиально непредсказуема (минуты…сутки), UI показывает вилку.
DEFAULT_AVG = {"build": 150, "ssl_issue": 90, "self_update": 360}

# Сколько успешных замеров нужно, чтобы доверять своим средним вместо дефолта.
MIN_SAMPLES = 2


def record(kind: str, subject: str | None = None, duration_seconds: float | None = None,
           outcome: str = "done", meta: dict | None = None, db=None) -> None:
    """Пишет замер операции. Best-effort: замер НИКОГДА не роняет саму операцию.

    `db` — использовать сессию вызывающего (обработчики pending_actions);
    без неё открывается своя (сборка в потоке оркестратора/WS).
    """
    meta_json = None
    if meta:
        try:
            meta_json = json.dumps(meta, ensure_ascii=False)
        except (TypeError, ValueError):
            meta_json = None
    try:
        session = db or SessionLocal()
        try:
            crud.record_operation_metric(
                session, kind=kind, subject=subject,
                duration_seconds=duration_seconds, outcome=outcome, meta=meta_json)
        finally:
            if db is None:
                session.close()
    except Exception as e:  # noqa: BLE001 — метрика не важнее операции
        print(f"[OPMETRICS] запись замера {kind} пропущена: {e}")


def avg_seconds(db, kind: str, stats: dict | None = None) -> int | None:
    """Средняя длительность операции по прошлым успешным прогонам (для ETA).

    Меньше MIN_SAMPLES замеров → дефолтный ориентир; для видов без ориентира
    (dns_wait) — None: честной оценки нет, UI показывает вилку, а не число.
    """
    stats = stats if stats is not None else crud.operation_stats(db)
    entry = stats.get(kind) or {}
    if (entry.get("samples") or 0) >= MIN_SAMPLES and entry.get("avg"):
        return round(entry["avg"])
    return DEFAULT_AVG.get(kind)
