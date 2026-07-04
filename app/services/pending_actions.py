"""Фоновый исполнитель долгих операций панели (Ночь 10, ADR-069).

Мотивация (инвариант №7, `18_RELEASE_PLAN`): публикация сервиса с выпуском SSL
раньше держала модалку открытой, пока распространяется DNS — а это может занять
до суток. Такие операции теперь становятся `PendingAction` в БД, а этот модуль —
периодический чекер (рядом с reconcile-циклом оркестратора) — доводит их до конца,
переживая перезагрузку страницы и закрытие вкладки.

Ключевой принцип: каждая «проба» задачи делает ОДИН неблокирующий шаг (один DNS-чек
или один прогон certbot) и планирует следующую пробу на будущее (`next_check_at`,
бэкофф). Цикл не спит внутри задачи — он спит между проходами и берёт только те
задачи, которым «пора».
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from datetime import datetime, timedelta

import httpx

from app import config, crud, environment, panel_config, schemas
from app.database import SessionLocal
from app.services import docker_manager, nginx_manager, op_metrics
from app.services.ssl_service import acme_preflight

# Как часто чекер просыпается (задачи всё равно гейтятся своим next_check_at).
LOOP_INTERVAL = 5.0
# Сколько всего ждём распространения DNS, прежде чем сдаться (реальный DNS — до суток).
DNS_MAX_AGE_SECONDS = 36 * 3600
# Сколько раз пробуем выпустить сертификат (после подтверждённого DNS), затем error.
SSL_MAX_ATTEMPTS = 6
# Предохранитель от бесконечных внутренних ошибок одной задачи.
HARD_MAX_ATTEMPTS = 40
# Обрезаем накопленный лог, чтобы строка в БД не росла бесконечно.
MAX_LOG_CHARS = 12000

# Кэш публичного IP сервера (ipify): не дёргаем внешний сервис на каждый DNS-чек.
_server_ip_cache: dict = {"ip": None, "at": 0.0}


class ActionError(Exception):
    """Понятная пользователю причина провала задачи (уходит в result)."""


# --- Помощники состояния задачи (params — JSON, log — накопительный) ---

def _load(action) -> dict:
    try:
        return json.loads(action.params) if action.params else {}
    except (ValueError, TypeError):
        return {}


def _store(action, params: dict) -> None:
    action.params = json.dumps(params, ensure_ascii=False)


def _append_log(action, line: str) -> None:
    ts = datetime.utcnow().strftime("%H:%M:%S")
    action.log = ((action.log or "") + f"[{ts}] {line}\n")[-MAX_LOG_CHARS:]


def _done(action, result: str) -> None:
    action.status = "done"
    action.result = result
    action.next_check_at = None
    _append_log(action, f"OK: {result}")


def _fail(action, result: str) -> None:
    action.status = "error"
    action.result = result
    action.next_check_at = None
    _append_log(action, f"STOP: {result}")


def _due_in(seconds: float) -> datetime:
    return datetime.utcnow() + timedelta(seconds=seconds)


def _dns_interval(attempts: int) -> float:
    """Бэкофф DNS-проверок: быстро в начале (15 c), реже позже (потолок 5 мин)."""
    return min(15 * (2 ** min(attempts, 5)), 300)


def _ssl_interval(attempts: int) -> float:
    """Бэкофф повторного выпуска SSL: 60 c → потолок 1 час."""
    return min(60 * (3 ** min(attempts, 4)), 3600)


# --- Проверки внешнего мира (DNS / сертификат) ---

def _public_ip() -> str | None:
    now = time.time()
    if _server_ip_cache["ip"] and now - _server_ip_cache["at"] < 300:
        return _server_ip_cache["ip"]
    try:
        resp = httpx.get("https://api.ipify.org", timeout=10.0)
        resp.raise_for_status()
        ip = resp.text.strip()
        _server_ip_cache.update(ip=ip, at=now)
        return ip
    except httpx.HTTPError:
        return _server_ip_cache["ip"]


def _resolve_ips(domain: str) -> list[str]:
    try:
        info = socket.getaddrinfo(domain, None, family=socket.AF_INET)
        return sorted({i[4][0] for i in info})
    except socket.gaierror:
        return []


def dns_matches(domain: str) -> tuple[bool, str]:
    """Указывает ли A-запись домена на этот сервер (детерминированно: IP среди записей)."""
    server_ip = _public_ip()
    if not server_ip:
        return False, "не удалось узнать публичный IP сервера"
    ips = _resolve_ips(domain)
    if not ips:
        return False, "A-запись домена ещё не найдена"
    if server_ip in ips:
        return True, f"домен указывает на {server_ip}"
    return False, f"домен → {', '.join(ips)}, сервер {server_ip}"


def _cert_exists(domain: str) -> bool:
    live = config.SSL_DIR / "live" / domain / "fullchain.pem"
    archive = config.SSL_DIR / "archive" / domain
    return live.exists() or (archive.is_dir() and any(archive.glob("fullchain*.pem")))


def issue_certificate(domain: str) -> tuple[bool, str]:
    """Синхронно выпускает Let's Encrypt-сертификат (без WebSocket). (успех, лог).

    Повторяет путь `ssl_service.perform_ssl_issuance`, но без стрима логов в UI —
    вывод certbot возвращается строкой и кладётся в лог задачи.
    """
    lines: list[str] = []
    try:
        nginx_manager.ensure_acme_challenge_ready()
    except Exception as e:  # noqa: BLE001 — самоизлечение не должно ронять выпуск
        lines.append(f"WARN: не удалось обновить catchall: {e!r}")

    ok_pre, detail = acme_preflight(domain)
    lines.append(f"Пред-проверка ACME: {'OK' if ok_pre else 'ВНИМАНИЕ'} — {detail}")

    certs_path = environment.ACME_CERTS_DIR
    certbot_command = config.CERTBOT_CMD_BASE + [
        "certonly", "--webroot", "-w", environment.ACME_WEBROOT, "-d", domain,
        *environment.acme_email_args(), "--agree-tos", "--non-interactive",
        "--rsa-key-size", "4096", "--config-dir", certs_path,
        "--work-dir", f"{certs_path}/lib", "--logs-dir", f"{certs_path}/logs",
    ]
    try:
        code, out = docker_manager.exec_in_container(config.CERTBOT_CONTAINER_NAME, certbot_command)
        lines.append((out or "").strip()[:4000] or f"certbot завершился с кодом {code}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"Ошибка запуска certbot: {e!r}")

    nginx_manager.reload_nginx()  # сам делает nginx -t и не бросает наружу

    if _cert_exists(domain):
        lines.append("Сертификат на месте.")
        return True, "\n".join(lines)
    lines.append("Сертификат не появился (вероятно DNS ещё не распространился или закрыт 80-й порт).")
    return False, "\n".join(lines)


# --- Операции над панелью/приложениями (переиспользуют существующий код) ---

def _create_application(db, name: str, domain: str, service_id: int, cert: str | None) -> int:
    if crud.get_application_by_domain(db, domain):
        raise ActionError(f"Домен {domain} уже используется другим приложением.")
    if crud.get_application_by_name(db, name):
        raise ActionError(f"Приложение с именем «{name}» уже существует.")
    if not crud.get_deployment(db, service_id):
        raise ActionError("Сервис для публикации не найден.")
    try:
        data = schemas.ApplicationCreate(
            name=name, domain=domain, service_id=service_id, ssl_cert_name=cert)
    except ValueError as e:
        raise ActionError(f"Некорректные данные публикации: {e}")
    app_row = crud.create_application(db, data)
    nginx_manager.update_application_nginx_config(app_row.name, app_row.domain, app_row.ssl_cert_name)
    nginx_manager.reload_nginx()
    return app_row.id


def _bind_cert_to_app(db, app_id: int, domain: str) -> None:
    app_row = crud.get_application(db, app_id)
    if not app_row:
        return
    crud.update_application(db, app_id, schemas.ApplicationUpdate(ssl_cert_name=domain))
    nginx_manager.update_application_nginx_config(app_row.name, app_row.domain, domain)
    nginx_manager.reload_nginx()


def _save_panel(domain: str, cert: str | None) -> None:
    panel_config.save_settings(panel_config.PanelSettings(domain=domain, ssl_cert_name=cert))
    nginx_manager.update_panel_nginx_config(domain=domain, ssl_cert_name=cert)
    nginx_manager.reload_nginx()


# --- Общий шаг «дождаться DNS → выпустить SSL → (опц.) привязать» ---

def _advance_ssl(db, action, params: dict, domain: str,
                 bind_app_id: int | None = None, on_success=None) -> None:
    now = time.time()
    params.setdefault("wait_since", now)

    matched, detail = dns_matches(domain)
    if not matched:
        if now - params["wait_since"] > DNS_MAX_AGE_SECONDS:
            # Замер честного провала ожидания (Ночь 14): сколько прождали впустую.
            op_metrics.record("dns_wait", subject=domain,
                              duration_seconds=round(now - params["wait_since"], 1),
                              outcome="error", db=db)
            _fail(action, "DNS так и не начал указывать на сервер (ждали более 36 часов). "
                          "Проверьте A-запись домена и нажмите «Повторить».")
            return
        action.attempts = (action.attempts or 0) + 1
        _append_log(action, f"Жду распространения DNS для {domain}… ({detail})")
        action.next_check_at = _due_in(_dns_interval(action.attempts))
        _store(action, params)
        return

    if not params.get("dns_confirmed"):
        # Первое подтверждение DNS — фиксируем стадию (для UI-стадии задачи) и
        # замер длительности ожидания (сырьё аналитики; ETA по нему не строим —
        # фаза принципиально непредсказуема, UI показывает вилку).
        params["dns_confirmed"] = True
        _store(action, params)
        op_metrics.record("dns_wait", subject=domain,
                          duration_seconds=round(now - params["wait_since"], 1), db=db)

    _append_log(action, f"DNS подтверждён ({detail}). Выпускаю сертификат…")
    ssl_started = time.time()
    ok, ssl_log = issue_certificate(domain)
    op_metrics.record("ssl_issue", subject=domain,
                      duration_seconds=round(time.time() - ssl_started, 1),
                      outcome="done" if ok else "error", db=db)
    _append_log(action, ssl_log)
    if ok:
        if bind_app_id:
            _bind_cert_to_app(db, bind_app_id, domain)
        if on_success:
            on_success()
        _done(action, f"https://{domain}")
        return

    ssl_attempts = params.get("ssl_attempts", 0) + 1
    params["ssl_attempts"] = ssl_attempts
    _store(action, params)
    if ssl_attempts >= SSL_MAX_ATTEMPTS:
        _fail(action, "Сертификат не выпущен после нескольких попыток. Проверьте, что "
                      "A-запись верна и открыт 80-й порт, затем нажмите «Повторить».")
        return
    _append_log(action, f"Повторю выпуск через {int(_ssl_interval(ssl_attempts))} c…")
    action.next_check_at = _due_in(_ssl_interval(ssl_attempts))


# --- Обработчики типов задач ---

def _handle_publish(db, action) -> None:
    params = _load(action)
    domain = params["domain"]
    ssl_mode = params.get("ssl_mode", "issue")

    # Публикуем сразу по HTTP (сервис виден мгновенно; ACME-challenge отдаёт его же
    # nginx-конфиг). SSL — отдельным шагом ниже, когда распространится DNS.
    if not params.get("app_id"):
        cert = params.get("existing_cert") if ssl_mode == "existing" else None
        try:
            app_id = _create_application(db, params["name"], domain, params["service_id"], cert)
        except ActionError as e:
            _fail(action, str(e))
            return
        params["app_id"] = app_id
        _store(action, params)
        _append_log(action, f"Опубликовано: {'https' if cert else 'http'}://{domain}")
        # Фиксируем стадию сразу: приложение уже создано (crud закоммитил его), поэтому
        # при рестарте между этими шагами app_id не должен потеряться (иначе повторная
        # публикация упрётся в «домен занят»).
        db.commit()
        if ssl_mode != "issue":
            _done(action, f"{'https' if cert else 'http'}://{domain}")
            return

    _advance_ssl(db, action, params, domain, bind_app_id=params["app_id"])


def _handle_issue_ssl(db, action) -> None:
    params = _load(action)
    _advance_ssl(db, action, params, params["domain"], bind_app_id=params.get("app_id"))


def _handle_panel_ssl(db, action) -> None:
    params = _load(action)
    domain = params["domain"]
    # Сначала домен панели должен слушать :80 (иначе ACME-challenge некому отдать).
    if not params.get("http_saved"):
        _save_panel(domain, None)
        params["http_saved"] = True
        _store(action, params)
        _append_log(action, f"Домен панели сохранён по HTTP: http://{domain}")
    _advance_ssl(db, action, params, domain, on_success=lambda: _save_panel(domain, domain))


def _handle_self_update(db, action) -> None:
    """Самообновление деплоера (Ночь 11, ADR-071): запустить updater-джобу на хосте
    и следить за ней. Своп контейнера убивает ЭТОТ процесс — задача переживает его
    в БД, и НОВЫЙ процесс доводит её (видит exit-код updater'а)."""
    from app.services import self_update

    params = _load(action)
    if not params.get("started"):
        try:
            self_update.launch_updater(params.get("ref"))
        except self_update.SelfUpdateError as e:
            _fail(action, str(e))
            return
        params["started"] = True
        params["started_ts"] = time.time()  # для замера self_update и ETA (Ночь 14)
        _store(action, params)
        _append_log(action, "Updater запущен: build-first, при провале health — авто-откат. "
                            "Панель может быть недоступна несколько секунд в момент переключения.")
        action.status = "running"
        action.next_check_at = _due_in(10)
        return

    state, code, logs = self_update.updater_status()
    if state == "running":
        action.attempts = (action.attempts or 0) + 1
        if action.attempts > 90:  # ~15 мин — сборка+health давно должны были кончиться
            _fail(action, "Updater не завершился за отведённое время — проверьте контейнер "
                          f"«{self_update.UPDATER_CONTAINER}» и журнал docker.")
            return
        action.next_check_at = _due_in(10)
        return
    if state == "missing":
        _fail(action, "Updater-контейнер исчез до завершения (удалён вручную?).")
        return

    if logs:
        _append_log(action, logs)
    self_update.cleanup_updater()

    def _measure(outcome: str) -> None:
        """Замер полного самообновления (Ночь 14) — от запуска updater'а до финала."""
        started = params.get("started_ts")
        if started:
            op_metrics.record("self_update", subject=(params.get("ref") or "latest")[:40],
                              duration_seconds=round(time.time() - started, 1),
                              outcome=outcome, db=db)

    if code == 0:
        if "ALREADY_UP_TO_DATE" in logs:
            # Без замера: «уже актуально» — не обновление, испортило бы среднее ETA.
            _done(action, "Уже актуальная версия — обновление не потребовалось.")
            return
        cur = (self_update.read_update_state().get("current_ref") or "")[:12]
        _measure("done")
        _done(action, f"Обновление применено{' (' + cur + ')' if cur else ''}.")
    elif code == 3:
        _measure("error")
        _fail(action, "Сборка новой версии провалилась — работающая версия не тронута (build-first).")
    elif code == 42:
        _measure("error")
        _fail(action, "Новая версия не прошла проверку здоровья — выполнен автоматический "
                      "откат на прежнюю версию.")
    else:
        _measure("error")
        _fail(action, f"Updater завершился с кодом {code} — подробности в журнале выше.")


HANDLERS = {
    "publish_on_dns": _handle_publish,
    "issue_ssl": _handle_issue_ssl,
    "panel_ssl": _handle_panel_ssl,
    "self_update": _handle_self_update,
}


# --- Стадия активной задачи для UI (Ночь 14, ADR-082) ---------------------------

STAGE_LABELS = {
    "publish": "Публикация приложения",
    "dns_wait": "Ожидание распространения DNS",
    "ssl_issue": "Выпуск сертификата",
    "self_update": "Обновление деплоера",
}

# Честная вилка для непредсказуемой фазы (провайдер DNS вне нашего контроля) —
# UI показывает её ВМЕСТО числового ETA (паттерн dns_unpredictable ADR-066).
DNS_HINT = "от минут до суток — зависит от DNS-провайдера"


def describe_stage(db, action, stats: dict | None = None) -> dict | None:
    """Текущая стадия активной задачи + ETA по средним прошлых прогонов.

    Единый источник для центра задач панели и зеркала задач в ЛК: семантика
    `params` (wait_since/dns_confirmed/app_id/started_ts) живёт здесь же, где
    её пишут обработчики. None — задача завершена или тип без стадий.
    """
    if action.status not in ("pending", "running"):
        return None
    params = _load(action)

    if action.type == "self_update":
        eta = None
        avg = op_metrics.avg_seconds(db, "self_update", stats=stats)
        if avg:
            elapsed = time.time() - params["started_ts"] if params.get("started_ts") else 0
            eta = max(round(avg - elapsed), 15)
        return {"stage": "self_update", "stage_label": STAGE_LABELS["self_update"],
                "eta_seconds": eta, "unpredictable": False}

    if action.type in ("publish_on_dns", "issue_ssl", "panel_ssl"):
        if action.type == "publish_on_dns" and not params.get("app_id"):
            stage = "publish"  # мгновенная стадия до первой пробы чекера
        elif not params.get("dns_confirmed"):
            stage = "dns_wait"
        else:
            stage = "ssl_issue"
        out = {"stage": stage, "stage_label": STAGE_LABELS[stage],
               "eta_seconds": None, "unpredictable": stage == "dns_wait"}
        if stage == "dns_wait":
            out["hint"] = DNS_HINT
        elif stage == "ssl_issue":
            out["eta_seconds"] = op_metrics.avg_seconds(db, "ssl_issue", stats=stats)
        return out
    return None


# --- Проход чекера и фоновый цикл ---

def process_due_actions() -> None:
    """Один синхронный проход: берёт все «поспевшие» задачи и двигает каждую на шаг."""
    db = SessionLocal()
    try:
        for action in crud.list_due_pending_actions(db, datetime.utcnow()):
            handler = HANDLERS.get(action.type)
            if not handler:
                _fail(action, f"Неизвестный тип задачи: {action.type}")
                db.commit()
                continue
            try:
                handler(db, action)
            except Exception as e:  # noqa: BLE001 — сбой одной задачи не роняет проход
                _append_log(action, f"Внутренняя ошибка: {e!r}")
                action.attempts = (action.attempts or 0) + 1
                if action.attempts >= HARD_MAX_ATTEMPTS:
                    _fail(action, f"Задача остановлена после повторных ошибок: {e}")
                else:
                    action.next_check_at = _due_in(60)
            db.commit()
    finally:
        db.close()


async def run_pending_actions_loop() -> None:
    """Асинхронная обёртка для фонового запуска в FastAPI (рядом с оркестратором)."""
    print("[PENDING] Loop started...")
    while True:
        try:
            await asyncio.to_thread(process_due_actions)
        except Exception as e:  # noqa: BLE001
            print(f"[PENDING] Loop Error: {e}")
        await asyncio.sleep(LOOP_INTERVAL)
