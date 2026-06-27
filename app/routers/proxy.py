# --- app/routers/proxy.py ---

import threading

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session
from app import crud, run_config
from app.database import get_db

# Hop-by-hop заголовки (RFC 2616 §13.5.1) — не проксируем: их смысл только для одного
# соединения. transfer-encoding убираем особенно: httpx уже снял chunked-framing, а
# StreamingResponse сам решит, как отдавать тело (иначе двойное кодирование). А вот
# content-length и content-encoding СОХРАНЯЕМ — aiter_raw() отдаёт «сырое» тело как с
# провода (всё ещё gzip'нутое, нужной длины), они должны дойти до клиента.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade"}

router = APIRouter()
http_client = httpx.AsyncClient(timeout=60.0)

# Round-robin балансировка по online-репликам. Состояние — счётчик в процессе
# деплоера (единственный uvicorn-процесс). Гонки безвредны: максимум — лёгкий
# перекос распределения, не ошибка. Балансируются только stateless-сервисы
# (stateful/replicas=1 — отдельный режим, см. ADR-018, Идея 5).
_rr_counters: dict[int, int] = {}
_rr_lock = threading.Lock()


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
        return Response(f"Application '{app_name}' not found.", status_code=404)

    # 2. Проверяем аутентификацию для этого приложения
    if application.users:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.lower().startswith("basic "):
            return Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Basic"})

        import base64
        try:
            auth_decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = auth_decoded.split(":", 1)
            user_in_db = crud.get_app_user_by_username(db, application.id, username)
            if not user_in_db or not crud.verify_password(password, user_in_db.hashed_password):
                return Response("Invalid credentials", status_code=401, headers={"WWW-Authenticate": "Basic"})
        except Exception:
            return Response("Invalid auth header", status_code=401, headers={"WWW-Authenticate": "Basic"})

    # 3. Находим деплой, на который указывает приложение
    deployment = application.deployment
    if not deployment:
        return Response(f"Deployment for application '{app_name}' not found.", status_code=503)

    # Находим онлайн реплики этого деплоя
    online_instances = [inst for inst in deployment.instances if inst.status == 'online']
    if not online_instances:
        return Response(f"No online instances for application '{app_name}' are available.", status_code=503)

    # 4. Балансируем запрос по online-репликам (round-robin).
    # В сетевой модели deployer-net обращаемся к контейнеру по ИМЕНИ на внутренний
    # порт приложения (по умолчанию 80, настраивается в расширенном режиме — Идея 2а).
    target_instance = _pick_round_robin(deployment.id, online_instances)
    target_port = run_config.effective_port(deployment.internal_port)
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
    except httpx.ConnectError:
        return Response(f"Could not connect to service container '{target_instance.container_name}'.", status_code=502)
    except Exception as e:
        return Response(f"Proxy error: {e}", status_code=500)