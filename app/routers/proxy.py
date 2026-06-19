# --- app/routers/proxy.py ---

import httpx
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session
from app import crud
from app.database import get_db

router = APIRouter()
http_client = httpx.AsyncClient(timeout=60.0)

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

    # 4. Проксируем запрос на первую доступную реплику.
    # В сетевой модели deployer-net обращаемся к контейнеру по ИМЕНИ на внутренний
    # порт 80 (приложение слушает 80). Host-порты больше не используются.
    target_instance = online_instances[0]
    target_url = f"http://{target_instance.container_name}:80/{path}"

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
        return Response(
            content=proxied_resp.aiter_raw(),
            status_code=proxied_resp.status_code,
            headers=proxied_resp.headers
        )
    except httpx.ConnectError:
        return Response(f"Could not connect to service container '{target_instance.container_name}'.", status_code=502)
    except Exception as e:
        return Response(f"Proxy error: {e}", status_code=500)