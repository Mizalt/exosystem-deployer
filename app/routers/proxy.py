# --- app/routers/proxy.py ---

import logging
import threading

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session
from app import crud, run_config
from app.database import get_db
from app.rate_limit import LoginRateLimiter
from app.services import placeholder

# Hop-by-hop заголовки (RFC 2616 §13.5.1) — не проксируем: их смысл только для одного
# соединения. transfer-encoding убираем особенно: httpx уже снял chunked-framing, а
# StreamingResponse сам решит, как отдавать тело (иначе двойное кодирование). А вот
# content-length и content-encoding СОХРАНЯЕМ — aiter_raw() отдаёт «сырое» тело как с
# провода (всё ещё gzip'нутое, нужной длины), они должны дойти до клиента.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade"}

logger = logging.getLogger(__name__)

router = APIRouter()
http_client = httpx.AsyncClient(timeout=60.0)

# Round-robin балансировка по online-репликам. Состояние — счётчик в процессе
# деплоера (единственный uvicorn-процесс). Гонки безвредны: максимум — лёгкий
# перекос распределения, не ошибка. Балансируются только stateless-сервисы
# (stateful/replicas=1 — отдельный режим, см. ADR-018, Идея 5).
_rr_counters: dict[int, int] = {}
_rr_lock = threading.Lock()

# Анти-брутфорс basic-auth проксируемых приложений (V-08). Публичная точка входа
# (доступна из интернета) — без троттлинга пароли app-пользователей перебираются
# свободно. Свой лимитер (счётчики приложений не мешаются с логином панели/ЛК).
app_auth_limiter = LoginRateLimiter()


def _pick_round_robin(deployment_id: int, instances: list):
    """Выбирает следующую online-реплику по кругу (round-robin).

    Реплики сортируются по id для стабильного порядка ротации. Раньше прокси брал
    всегда первую (online_instances[0]) — 2-я и далее реплики не получали трафик
    (известное ограничение, закрывается здесь — Идея 5 фаза 1).
    """
    ordered = sorted(instances, key=lambda i: i.id)
    with _rr_lock:
        idx = _rr_counters.get(deployment_id, 0)
        _rr_counters[deployment_id] = idx + 1
    return ordered[idx % len(ordered)]


def _accepts_html(accept: str) -> bool:
    """`text/html` заявлен в Accept с q > 0 (клиент реально ГОТОВ принять html).

    Подстрочный матч был бы неверен дважды: `text/html;q=0` — это явный ОТКАЗ от
    html (RFC 9110 §12.4.2), а не согласие; и наоборот, парсим по media-range,
    чтобы браузерные списки вида `text/html,application/xhtml+xml,...;q=0.9`
    корректно матчились.
    """
    for part in accept.lower().split(","):
        pieces = part.strip().split(";")
        if pieces[0].strip() != "text/html":
            continue
        q = 1.0
        for param in pieces[1:]:
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 1.0
        if q > 0:
            return True
    return False


def _is_navigation_request(request: Request) -> bool:
    """Браузерная СТРАНИЧНАЯ навигация? (задача #3, ADR-142 + дофикс по ревью)

    Разграничение КЛЮЧЕВОЕ: заглушку окна неготовности показываем только
    страничным переходам браузера. API/XHR/fetch/ассеты сервиса (Accept
    application/json, */* и т.п.) должны получать прежние 502/503 — иначе фронт
    самого сервиса в окно поднятия ловил бы HTML-мусор вместо ошибки.

    Три сигнала (дофикс по ревью — раньше был только подстрочный Accept):
    1. Метод: навигация — это GET/HEAD. Браузерный POST формы должен получить
       честную ошибку (иначе тело формы молча теряется, а 200-заглушка выглядит
       как «данные приняты»).
    2. Sec-Fetch-Mode (если браузер его прислал — все современные по HTTPS):
       только `navigate`. Отсекает XHR/fetch за html-ФРАГМЕНТАМИ (jQuery
       dataType:'html' шлёт Accept с text/html, но Sec-Fetch-Mode: cors) —
       иначе фрагмент-запрос получил бы ЦЕЛУЮ страницу-заглушку со статусом 200.
    3. Фолбэк — Accept с text/html и q > 0 (по HTTP до выпуска SSL браузеры
       Sec-Fetch-* не шлют: заголовок только для trustworthy-origin).
    """
    if request.method not in ("GET", "HEAD"):
        return False
    sec_fetch_mode = request.headers.get("sec-fetch-mode")
    if sec_fetch_mode is not None:
        return sec_fetch_mode.strip().lower() == "navigate"
    return _accepts_html(request.headers.get("accept", ""))


def _placeholder_response() -> HTMLResponse:
    """HTML-заглушка «тут скоро будет что-то новенькое» для окна неготовности.

    Статус 200 (а не 503+Retry-After) — гарантия показа брендстраницы во всех
    браузерах/прокси (некоторые подменяют 5xx своей страницей ошибки).
    Cache-Control: no-store — иначе после готовности посетитель мог бы увидеть
    закэшированную заглушку. X-Robots-Tag: noindex — не индексировать (в HTML
    продублировано <meta name=robots> + <meta refresh> ~10с на авто-обновление).
    Контент генеричен, без данных запроса — см. app/services/placeholder.py.
    """
    return HTMLResponse(
        placeholder.render_placeholder(),
        status_code=200,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )

@router.api_route("/api/proxy/{app_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_to_application(
        app_name: str,
        path: str,
        request: Request,
        db: Session = Depends(get_db)
):
    """
    Проксирует запрос к ПРИЛОЖЕНИЮ, которое указывает на СЕРВИС (Deployment).
    """
    # 1. Находим публичное приложение по его имени
    application = crud.get_application_by_name(db, name=app_name)
    if not application:
        # Тело БЕЗ app_name (дофикс по ревью): path-сегмент контролируется
        # клиентом (URL-decoded) — отражать его в ответ без Content-Type нельзя
        # (reflected-инъекция + MIME-sniffing). media_type даёт честный
        # text/plain; charset=utf-8.
        return Response("Application not found.", status_code=404, media_type="text/plain")

    # 2. Проверяем аутентификацию для этого приложения
    if application.users:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.lower().startswith("basic "):
            return Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Basic"})

        import base64
        try:
            auth_decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = auth_decoded.split(":", 1)
        except Exception:
            return Response("Invalid auth header", status_code=401, headers={"WWW-Authenticate": "Basic"})

        # Анти-брутфорс (V-08): лимит по IP клиента и по (приложение+имя пользователя).
        ip = request.client.host if request.client else "unknown"
        limit_keys = [f"ip:{ip}", f"appuser:{app_name}:{username}"]
        wait = app_auth_limiter.retry_after(limit_keys)
        if wait > 0:
            return Response("Too many attempts", status_code=429,
                            headers={"Retry-After": str(wait)})

        user_in_db = crud.get_app_user_by_username(db, application.id, username)
        if not user_in_db or not crud.verify_password(password, user_in_db.hashed_password):
            app_auth_limiter.record_failure(limit_keys)
            return Response("Invalid credentials", status_code=401, headers={"WWW-Authenticate": "Basic"})
        app_auth_limiter.reset(limit_keys)  # успех — сбрасываем счётчик

    # 3. Находим деплой, на который указывает приложение
    deployment = application.deployment
    if not deployment:
        # Достижимо при висячем deployment_id (SQLite без PRAGMA foreign_keys) —
        # по смыслу то же окно неготовности: навигации заглушка, телу — генерика
        # без имени приложения (дофикс по ревью).
        if _is_navigation_request(request):
            return _placeholder_response()
        return Response("Service is not ready.", status_code=503, media_type="text/plain")

    # Находим онлайн реплики этого деплоя
    online_instances = [inst for inst in deployment.instances if inst.status == 'online']
    if not online_instances:
        # Окно неготовности (задача #3): vhost уже есть (запрос дошёл сюда), но ни
        # одна реплика ещё не online (контейнер поднимается / SSL только выпущен).
        # Браузерной навигации — заглушка; API/ассетам — прежний 503 (тело без
        # app_name — не палим имя внутренней сущности анонимному посетителю).
        if _is_navigation_request(request):
            return _placeholder_response()
        return Response("No online instances are available.", status_code=503, media_type="text/plain")

    # 4. Балансируем запрос по online-репликам (round-robin).
    # В сетевой модели deployer-net обращаемся к контейнеру по ИМЕНИ на внутренний
    # порт приложения (по умолчанию 80, настраивается в расширенном режиме — Идея 2а).
    target_instance = _pick_round_robin(deployment.id, online_instances)
    target_port = run_config.effective_port(deployment.internal_port, deployment.detected_port)
    target_url = f"http://{target_instance.container_name}:{target_port}/{path}"

    headers = dict(request.headers)
    headers["X-Real-IP"] = request.client.host
    headers.pop("host", None)
    headers.pop("authorization", None)

    try:
        proxied_req = http_client.build_request(
            method=request.method, url=target_url, headers=headers,
            params=request.query_params, content=await request.body()
        )
        proxied_resp = await http_client.send(proxied_req, stream=True)
        # ВАЖНО: стримим через StreamingResponse, а не Response(content=<async gen>) —
        # базовый Response пытается .encode() контент и падает на async-генераторе.
        # aclose() в background закрывает upstream-соединение после отдачи тела.
        resp_headers = {k: v for k, v in proxied_resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return StreamingResponse(
            proxied_resp.aiter_raw(),
            status_code=proxied_resp.status_code,
            headers=resp_headers,
            background=BackgroundTask(proxied_resp.aclose),
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Реплика числится online, но соединиться не удалось: refused/DNS-fail
        # (ConnectError) ИЛИ таймаут коннекта (ConnectTimeout — НЕ подкласс
        # ConnectError, дофикс по ревью) — окно между стартом контейнера и
        # готовностью слушать порт. Навигации — заглушка, иначе генеричный 502
        # (имя контейнера — только в лог: префикс deployer-* деанонимизирует
        # платформу публичному посетителю, нарушая white-label ADR-142).
        logger.warning("Прокси: не удалось соединиться с репликой '%s' (%s)",
                       target_instance.container_name, target_url)
        if _is_navigation_request(request):
            return _placeholder_response()
        return Response("Could not connect to the service.", status_code=502, media_type="text/plain")
    except Exception:
        # Текст исключения может содержать внутренний target_url/детали httpx —
        # наружу константа, подробности только в лог (дофикс по ревью).
        logger.exception("Прокси: ошибка при запросе к %s", target_url)
        return Response("Proxy error.", status_code=500, media_type="text/plain")