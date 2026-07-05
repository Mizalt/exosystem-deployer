"""Автопродление SSL-сертификатов (Ночь 16, ADR-085).

«Пользователь об SSL не думает вообще»: часовой чекер следит за сроками ВСЕХ
сертификатов, которыми реально пользуются (приложения + домен панели), и за
~30 дней до истечения ставит фоновую задачу `ssl_renew` (паттерн PendingAction,
инвариант №7 — ничего не блокирует UI). Если продление не удаётся, а до
истечения ≤14 дней — задача падает в error с громким текстом: это алерт в
центре задач панели, он же зеркалится в ЛК (`GET /api/ssl/expiring` →
`Deployer.ssl_alerts`) и триггерит письмо владельцу.

Сертификаты двух видов:
  • Let's Encrypt (`ssl_certs/live/<домен>/…`) — продлеваем сами (certbot);
  • загруженные вручную (`ssl_certs/<имя>/…`) — продлить не можем, только
    честно предупреждаем заранее.

Модуль не импортирует `pending_actions` (обработчик задачи живёт там и
импортирует нас) — цикл зависимостей исключён.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app import config, crud, models, panel_config
from app.database import SessionLocal

# За сколько дней до истечения начинаем продлевать (окно renew certbot — 30 дней).
RENEW_BEFORE_DAYS = 30
# Порог алерта: продление всё ещё не удалось, осталось ≤14 дней → error-задача
# (алерт в центре задач + зеркало ЛК + письмо владельцу).
ALERT_DAYS = 14
# Пауза между повторными попытками продления (сек): 4 попытки в сутки — далеко
# от лимитов Let's Encrypt (5 failed validations / час), но не даёт молча ждать.
RETRY_SECONDS = 6 * 3600
# Как часто чекер сканирует сроки (сек).
SWEEP_INTERVAL = 3600
# После завершённой (done/error) задачи продления новую по тому же серту не
# ставим сутки: error-задача остаётся видимым алертом, а не спамом копий.
RESWEEP_COOLDOWN = 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cert_paths(cert_name: str) -> list:
    """Возможные пути fullchain.pem: LE-структура и «свой» загруженный серт."""
    return [config.SSL_DIR / "live" / cert_name / "fullchain.pem",
            config.SSL_DIR / cert_name / "fullchain.pem"]


def is_letsencrypt(cert_name: str) -> bool:
    """LE-сертификат (можем продлить сами) или загруженный вручную (не можем)."""
    return (config.SSL_DIR / "live" / cert_name / "fullchain.pem").exists()


def cert_expiry(cert_name: str) -> datetime | None:
    """Срок действия сертификата (not_valid_after, UTC) из PEM. None — нет/битый."""
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    for path in cert_paths(cert_name):
        if not path.exists():
            continue
        try:
            cert = x509.load_pem_x509_certificate(path.read_bytes(), default_backend())
            return _as_aware(cert.not_valid_after_utc)
        except Exception as e:  # noqa: BLE001 — битый PEM не должен ронять чекер
            print(f"[SSL-RENEW] не удалось прочитать {path}: {e}")
    return None


def days_left(expiry: datetime) -> float:
    return (_as_aware(expiry) - _utcnow()).total_seconds() / 86400


def days_phrase(days: float) -> str:
    """Человекочитаемый срок: «истекает через N дн.» / «истёк N дн. назад».

    Просроченный сертификат не должен «истекать через -104 дн.» — при
    отрицательном остатке считаем дни назад (тексты задач ssl_renew, лог чекера)."""
    n = int(days)
    return f"истёк {-n} дн. назад" if n < 0 else f"истекает через {n} дн."


def expiry_text(expiry: datetime, days: float) -> str:
    """То же с датой: «истекает 22.03.2026 (через N дн.)» / «истёк 22.03.2026 (N дн. назад)»."""
    n = int(days)
    if n < 0:
        return f"истёк {expiry:%d.%m.%Y} ({-n} дн. назад)"
    return f"истекает {expiry:%d.%m.%Y} (через {n} дн.)"


def certificates_in_use(db) -> list[dict]:
    """Сертификаты, которыми реально пользуются: приложения + домен панели.

    → [{"name": имя серта, "uses": ["app:<домен>", …, "panel"]}]. Неиспользуемые
    серты не трогаем: их истечение ничего не ломает (и продлевать их LE-путём
    часто уже нельзя — домен мог уехать).
    """
    by_name: dict[str, list[str]] = {}
    for app_row in db.query(models.Application).all():
        if app_row.ssl_cert_name:
            by_name.setdefault(app_row.ssl_cert_name, []).append(f"app:{app_row.domain}")
    panel = panel_config.load_settings()
    if panel.ssl_cert_name:
        by_name.setdefault(panel.ssl_cert_name, []).append("panel")
    return [{"name": name, "uses": uses} for name, uses in sorted(by_name.items())]


def expiring_report(db) -> list[dict]:
    """Отчёт «что истекает» для UI панели и зеркала ЛК (только серты в работе).

    status: warning (≤30 дн., продление идёт/запланировано) | alert (≤14 дн. —
    считаем тревогой независимо от причин: времени осталось мало).
    """
    items = []
    for cert in certificates_in_use(db):
        expiry = cert_expiry(cert["name"])
        if expiry is None:
            continue
        d = days_left(expiry)
        if d > RENEW_BEFORE_DAYS:
            continue
        items.append({
            "name": cert["name"],
            "not_after": expiry.isoformat(),
            "days_left": int(d),
            "renewable": is_letsencrypt(cert["name"]),
            "uses": cert["uses"],
            "status": "alert" if d <= ALERT_DAYS else "warning",
        })
    return sorted(items, key=lambda i: i["days_left"])


def _has_recent_renew_task(db, cert_name: str) -> bool:
    """Уже есть активная задача продления этого серта — или завершённая недавно
    (сутки): error-версия остаётся видимым алертом, дубликаты не плодим."""
    rows = (db.query(models.PendingAction)
            .filter(models.PendingAction.type == "ssl_renew")
            .order_by(models.PendingAction.id.desc()).limit(50).all())
    for action in rows:
        try:
            params = json.loads(action.params or "{}")
        except (ValueError, TypeError):
            params = {}
        if params.get("cert") != cert_name:
            continue
        if action.status in ("pending", "running"):
            return True
        ref = _as_aware(action.updated_at or action.created_at)
        if ref and (_utcnow() - ref).total_seconds() < RESWEEP_COOLDOWN:
            return True
    return False


def sweep_once() -> int:
    """Один проход чекера: поставить задачи продления на всё, что скоро истечёт.

    Возвращает число созданных задач (для тестов/лога). Сам ничего не продлевает —
    продление делает обработчик `ssl_renew` в pending_actions (бэкофф/алерты там).
    """
    created = 0
    db = SessionLocal()
    try:
        for cert in certificates_in_use(db):
            name = cert["name"]
            expiry = cert_expiry(name)
            if expiry is None:
                continue
            d = days_left(expiry)
            if d > RENEW_BEFORE_DAYS or _has_recent_renew_task(db, name):
                continue
            renewable = is_letsencrypt(name)
            crud.create_pending_action(
                db, type="ssl_renew",
                title=f"Продление SSL: {name}",
                params=json.dumps({
                    "cert": name,
                    "renewable": renewable,
                    "not_after": expiry.isoformat(),
                    "uses": cert["uses"],
                }, ensure_ascii=False))
            created += 1
            print(f"[SSL-RENEW] сертификат «{name}» {days_phrase(d)} — "
                  f"задача продления поставлена ({'LE' if renewable else 'ручной'}).")
    finally:
        db.close()
    return created


async def run_ssl_renewal_loop() -> None:
    """Фоновый цикл чекера сроков (запускается в lifespan рядом с оркестратором)."""
    print("[SSL-RENEW] Expiry checker started...")
    while True:
        try:
            await asyncio.to_thread(sweep_once)
        except Exception as e:  # noqa: BLE001 — чекер живёт при любых сбоях прохода
            print(f"[SSL-RENEW] Checker error: {e}")
        await asyncio.sleep(SWEEP_INTERVAL)
