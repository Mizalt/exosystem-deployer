# --- main.py ---

import sys

# Принудительный UTF-8 для вывода. В контейнерах/при перенаправлении логов
# Windows иначе использует локальную кодовую страницу (cp1251), и любой
# не-ASCII символ (например эмодзи) роняет print() с UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

import hashlib
from pathlib import Path
from typing import List, Annotated
from contextlib import asynccontextmanager

import uuid

import httpx
import uvicorn
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, Response, UploadFile, WebSocket)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from starlette.staticfiles import StaticFiles

from app import crud, schemas, panel_config, models, security, config, bootstrap, artifact_utils
from app import github_client
from app import environment
from app import editions
from app import version as deployer_version
from app import run_config
from app import security_headers
from app import pro_gate
from app.environment import get_docker_client
from app.database import get_db, init_db_with_migrations, SessionLocal
from app.routers import proxy, ssl, panel, auth, control_plane, pending
from app.services import docker_manager, nginx_manager, nginx_service, build_service
from app.services.ws_manager import manager as ws_manager

import asyncio
from app.services.orchestrator import run_orchestrator_loop
from app.services.pending_actions import run_pending_actions_loop
from app.services.ssl_renewal import run_ssl_renewal_loop
from app.services.metrics_history import run_metrics_history_loop

UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)

# Инициализация БД выполняется в lifespan (startup), а не на уровне модуля —
# чтобы импорт main не имел побочных эффектов (важно для тестов).


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("--- Running startup tasks ---")
    print(f"INFO: Environment: {environment.describe()}")
    print(f"INFO: Edition: {editions.get_edition()}")
    init_db_with_migrations()

    # Онбординг: при первом запуске создаём администратора (без ручного create_admin.py).
    db = SessionLocal()
    try:
        bootstrap.ensure_admin_exists(db)
    finally:
        db.close()

    await nginx_service.ensure_infrastructure_running(
        nginx_configs_path=config.NGINX_SITES_DIR,
        ssl_certs_path=config.SSL_DIR,
        acme_challenge_path=config.ACME_CHALLENGE_DIR
    )

    settings = panel_config.load_settings()
    nginx_manager.update_panel_nginx_config(
        domain=settings.domain,
        ssl_cert_name=settings.ssl_cert_name
    )

    try:
        nginx_manager.reload_nginx()
    except Exception as e:
        print(f"ERROR: Initial Nginx reload failed: {e}")

    # Уборка «сирот»: контейнеры от прошлых запусков, не отслеживаемые в БД,
    # иначе они бесконечно крутятся с restart_policy=unless-stopped.
    db = SessionLocal()
    try:
        known_names = {name for (name,) in db.query(models.Instance.container_name).all() if name}
        docker_manager.cleanup_orphan_containers(known_names)
    except Exception as e:
        print(f"ERROR: Orphan cleanup failed: {e}")
    finally:
        db.close()

    orchestrator_task = asyncio.create_task(run_orchestrator_loop())
    # Фоновый чекер долгих операций (публикация/SSL/DNS) — Ночь 10, ADR-069.
    pending_task = asyncio.create_task(run_pending_actions_loop())
    # Часовой чекер сроков сертификатов → задачи автопродления (Ночь 16, ADR-085).
    ssl_renewal_task = asyncio.create_task(run_ssl_renewal_loop())
    # Минутный сэмплер метрик хоста → графики динамики на дашборде (Ночь 19).
    metrics_task = asyncio.create_task(run_metrics_history_loop())
    # PRO-фоновые задачи (проверка лицензии, ADR-100) — пусто в OSS-срезе (нет app/pro).
    pro_tasks = pro_gate.start_pro_background_tasks()
    print("SUCCESS: Infrastructure ready and Orchestrator started.")

    yield

    print("--- Running shutdown tasks ---")
    orchestrator_task.cancel()
    pending_task.cancel()
    ssl_renewal_task.cancel()
    metrics_task.cancel()
    for t in pro_tasks:
        t.cancel()
    for task, label in ((orchestrator_task, "Orchestrator"), (pending_task, "Pending-actions"),
                        (ssl_renewal_task, "SSL-renewal"), (metrics_task, "Metrics-history"),
                        *((t, "PRO-task") for t in pro_tasks)):
        try:
            await task
        except asyncio.CancelledError:
            print(f"INFO: {label} task cancelled successfully.")


app = FastAPI(title="EXOSYSTEM DEPLOY", lifespan=lifespan)


@app.middleware("http")
async def add_security_headers(request, call_next):
    """Заголовки безопасности на ответы панели (не на проксируемые приложения).

    Набор динамический (ADR-092): при заданном embed-origin (env/пуш ЛК) панель
    разрешает фрейминг РОВНО этому origin (CSP frame-ancestors), иначе — прежний
    fail-closed запрет (X-Frame-Options: DENY + frame-ancestors 'none')."""
    response = await call_next(request)
    if security_headers.should_apply(request.url.path):
        for key, value in security_headers.current_headers().items():
            response.headers.setdefault(key, value)
    return response


app.include_router(ssl.router)
app.include_router(proxy.router)
app.include_router(panel.router)
app.include_router(panel.ai_router)  # ИИ-помощник панели: GET /api/panel/ai-availability (ADR-103)
app.include_router(auth.router)
app.include_router(control_plane.router)  # cpk-эндпоинты; без env-ключа отвечают 404
app.include_router(pending.router)  # центр фоновых задач (публикация/SSL) — ADR-069

# PRO-роутеры (ADR-100): точка расширения с graceful-фолбэком. Нет app/pro (OSS-срез)
# → no-op (эндпоинты /api/pro/* отсутствуют → 404). Есть каталог → роутеры
# зарегистрированы, но каждый вызов гейтится лицензией (require_pro_feature/cpk).
pro_gate.register_pro_routers(app)

app.mount("/static", StaticFiles(directory="static"), name="static")

CurrentUser = Annotated[models.User, Depends(security.get_current_user)]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def read_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/api/edition", tags=["System"])
def get_edition(current_user: CurrentUser):
    """Текущее издание и доступность фич (open-core: oss/pro/cloud). См. ADR-019.

    Блок `pro_features` (ADR-100) — фактическая доступность лицензионных фич СЕЙЧАС
    (двойной гейт editions+лицензия через `pro_gate`). UI прячет PRO-кнопки, когда фича
    False (нет лицензии / OSS-срез). Ключи — PRO-фичи из каталога; в OSS все False."""
    info = editions.describe()
    info["pro_features"] = {
        name: pro_gate.pro_feature_available(name)
        for name in ("rate_limit_ui", "abuse_shield", "api_map", "scoped_tokens")
    }
    return info


@app.get("/api/version", tags=["System"])
def get_version_info():
    """Публично: версия сборки деплоера + `capabilities` (Ночь 11, ADR-071).

    Без авторизации — только несекретные метаданные сборки. ЛК опрашивает этот
    эндпоинт и адаптирует UI/действия по `capabilities` (совместимость со ВСЕМИ
    версиями нод), а не по номеру версии. Старая нода без эндпоинта → 404.
    После самообновления добавляется блок `update` из data/update_state.json
    (current/previous ref, статус) — его пишет updater-джоба (self_update).
    """
    info = deployer_version.describe()
    from app.services.self_update import read_update_state
    state = read_update_state()
    if state:
        info["update"] = {k: state[k] for k in
                          ("current_ref", "previous_ref", "failed_ref", "status", "updated_at")
                          if state.get(k)}
        info["git_sha"] = info["git_sha"] or state.get("current_ref")
    return info


# === СОВМЕСТИМОСТЬ С СЕРВИСАМИ ДЛЯ ФРОНТЕНДА (/api/services) ===

@app.get("/api/services", tags=["Services Compatibility Layer"])
def get_services_compat(current_user: CurrentUser, db: Session = Depends(get_db)):
    """Эмулирует список сервисов из списка деплоев."""
    from app.services import build_progress, op_metrics
    deployments = crud.get_deployments(db)
    # ETA сборки по средним прошлых прогонов (Ночь 14) — один запрос на список.
    build_avg = op_metrics.avg_seconds(db, "build")
    services = []
    for dep in deployments:
        first_instance = dep.instances[0] if dep.instances else None
        port = first_instance.assigned_port if first_instance else 0
        status = first_instance.status if first_instance else "offline"
        online_count = sum(1 for i in dep.instances if i.status == "online")

        # Живой прогресс сборки (Ночь 14, ADR-082): тег образа content-addressed —
        # формула та же, что у оркестратора, поэтому ищем сборку именно этого
        # (версия+конфиг) в реестре. None — сборка сейчас не идёт.
        build = None
        if dep.artifact and dep.artifact.zip_hash:
            tag = docker_manager.compute_image_tag(dep.artifact.zip_hash, {
                "base_image": dep.base_image,
                "run_command": dep.run_command,
                "internal_port": run_config.effective_port(dep.internal_port),
            })
            build = build_progress.get(tag)
            if build and build_avg:
                build["eta_seconds"] = max(
                    round(build_avg - (build.get("elapsed_seconds") or 0)), 5)

        services.append({
            "id": dep.id,
            "name": dep.name or dep.blueprint.name,
            "blueprint_name": dep.blueprint.name,
            "assigned_port": port,
            "status": status,
            # Реплики: желаемое (target) и сколько реально online — для UI-индикатора
            # «N/N online» и контрола масштабирования (Идея 5 фаза 1).
            "target_replicas": dep.target_replicas,
            "online_count": online_count,
            "instances_count": len(dep.instances),
            # Расширенный режим сборки/рантайма (Идея 2а) — для UI-редактора конфига.
            "internal_port": run_config.effective_port(dep.internal_port, dep.detected_port),
            "run_command": dep.run_command,
            "base_image": dep.base_image,
            "env_vars": run_config.env_from_json(dep.env_vars),
            "artifact": {
                "id": dep.artifact.id,
                "version_tag": dep.artifact.version_tag,
                "blueprint_id": dep.blueprint_id,
                "created_at": dep.artifact.created_at.isoformat() if dep.artifact.created_at else None
            },
            # Связанные публикации — чтобы UI мог дать «Открыть» (через прокси-роут).
            "applications": [
                {"id": a.id, "name": a.name, "domain": a.domain, "ssl": bool(a.ssl_cert_name)}
                for a in dep.applications
            ],
            # Живой прогресс сборки/пулла (Ночь 14): {stage, percent, detail,
            # elapsed_seconds, eta_seconds} или None, когда сборка не идёт.
            "build": build,
        })
    return services


def _apply_run_config(dep, data: dict):
    """Применяет поля расширенного режима (Идея 2а) к Deployment из входного dict.

    Затрагивает только переданные ключи (частичное обновление), не коммитит.
    Валидирует internal_port. Пустые строки → None (вернуться к автогену/дефолту).
    """
    if "internal_port" in data:
        raw = data.get("internal_port")
        if raw is None or raw == "":
            # Пусто → «авто»: снимаем явный порт И сброшенный детект (пересборка может
            # изменить EXPOSE) — оркестратор передетектит из образа при следующем деплое.
            dep.internal_port = None
            dep.detected_port = None
        else:
            try:
                port = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="internal_port должен быть числом")
            if port < 0 or port > 65535:
                raise HTTPException(status_code=400, detail="internal_port вне диапазона 0..65535")
            dep.internal_port = port
    if "run_command" in data:
        dep.run_command = (data.get("run_command") or "").strip() or None
    if "base_image" in data:
        dep.base_image = (data.get("base_image") or "").strip() or None
    if "env_vars" in data:
        dep.env_vars = run_config.env_to_json(data.get("env_vars"))


@app.post("/api/services", tags=["Services Compatibility Layer"])
def create_service_compat(service_data: dict, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Эмулирует создание сервиса через деплой."""
    artifact_id = service_data.get("artifact_id")
    group_name = service_data.get("group_name")
    if not artifact_id or not group_name:
        raise HTTPException(status_code=400, detail="Missing artifact_id or group_name")

    artifact = crud.get_artifact(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Имя сервиса: пусто → автоген из имени приложения (с суффиксом при коллизии);
    # задано вручную → требуем уникальности (иначе 409).
    requested_name = (service_data.get("name") or "").strip()
    if requested_name:
        if crud.get_deployment_by_name(db, requested_name):
            raise HTTPException(status_code=409, detail=f"Сервис с именем '{requested_name}' уже существует.")
        service_name = requested_name
    else:
        base = artifact.blueprint.name
        service_name = base
        i = 2
        while crud.get_deployment_by_name(db, service_name):
            service_name = f"{base}-{i}"
            i += 1

    dep_data = schemas.DeploymentCreate(
        name=service_name,
        artifact_id=artifact_id,
        target_replicas=1,
        group_name=group_name
    )
    dep = crud.create_deployment(db, dep_data, blueprint_id=artifact.blueprint_id)

    # Расширенный режим (Идея 2а): опц. база/команда/порт/env. Пусто → питоновский
    # автоген на порту 80 (прежнее поведение). Применяем сразу при создании.
    _apply_run_config(dep, service_data)
    db.commit()

    return {
        "id": dep.id,
        "name": dep.name or dep.blueprint.name,
        "assigned_port": 0,
        "status": "starting",
        "artifact": {
            "id": dep.artifact.id,
            "version_tag": dep.artifact.version_tag,
            "blueprint_id": dep.blueprint_id
        }
    }


@app.post("/api/services/{service_id}/start", tags=["Services Compatibility Layer"])
def start_service_compat(service_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")
    dep.target_replicas = 1
    db.commit()
    return {"message": "Service starting"}


@app.post("/api/services/{service_id}/stop", tags=["Services Compatibility Layer"])
def stop_service_compat(service_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")
    dep.target_replicas = 0
    db.commit()
    return {"message": "Service stopping"}


@app.post("/api/services/{service_id}/restart", tags=["Services Compatibility Layer"])
def restart_service_compat(service_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")

    client = get_docker_client()
    for inst in dep.instances:
        try:
            container = client.containers.get(inst.container_name)
            container.restart()
        except Exception:
            pass
    return {"message": "Service restarted"}


@app.patch("/api/services/{service_id}/config", tags=["Services Compatibility Layer"])
def update_service_config_compat(service_id: int, data: dict, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Меняет расширенный конфиг сборки/рантайма (база/команда/порт/env, Идея 2а) и
    ПЕРЕСОЗДАЁТ реплики, чтобы применить (новый образ и env задаются при запуске
    контейнера). Сбрасывает build-backoff — даём сборке новый шанс."""
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")
    _apply_run_config(dep, data)
    # Build-first (ADR-022): собираем образ с НОВЫМ конфигом ДО сноса реплик и свапаем
    # только при успехе. Провал → откат изменений конфига (db.rollback), сервис не тронут.
    try:
        build_service.build_first_swap(db, dep, dep.artifact)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Сборка с новым конфигом не удалась — сервис не изменён.\n{e}")
    db.commit()
    return {
        "message": "Конфиг применён, сервис пересоздаётся.",
        "internal_port": run_config.effective_port(dep.internal_port, dep.detected_port),
        "run_command": dep.run_command,
        "base_image": dep.base_image,
        "env_vars": run_config.env_from_json(dep.env_vars),
    }


@app.post("/api/services/{service_id}/scale", tags=["Services Compatibility Layer"])
def scale_service_compat(service_id: int, data: schemas.DeploymentScaleRequest, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Меняет желаемое число реплик (Идея 5 фаза 1). Оркестратор сам приведёт
    действительное состояние к target_replicas (scale up/down). Балансировка между
    репликами — round-robin в proxy.py. Только для stateless-сервисов."""
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")
    n = data.target_replicas
    if n < 0 or n > 20:
        raise HTTPException(status_code=400, detail="target_replicas должно быть в диапазоне 0..20")
    dep.target_replicas = n
    db.commit()
    return {"message": f"Цель — {n} реплик(и)", "target_replicas": n}


@app.post("/api/services/{service_id}/redeploy", tags=["Services Compatibility Layer"])
def redeploy_service_compat(service_id: int, data: dict, current_user: CurrentUser, db: Session = Depends(get_db)):
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")

    artifact_id = data.get("artifact_id")
    if not artifact_id:
        raise HTTPException(status_code=400, detail="Missing artifact_id")

    artifact = crud.get_artifact(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Build-first (ADR-022): собираем образ НОВОЙ версии ДО сноса работающих реплик и
    # свапаем только при успехе. Провал → откат смены версии, работающий сервис не тронут
    # (DoD «неудачные деплои не ломают работающий сервис»).
    try:
        build_service.build_first_swap(db, dep, artifact)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Сборка новой версии не удалась — сервис не тронут.\n{e}")
    db.commit()
    return {"message": "Service redeployed"}


@app.post("/api/services/{service_id}/redeploy-stream", tags=["Services Compatibility Layer"])
async def redeploy_stream_start(service_id: int, data: dict, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Запускает редеплой на новую версию с ЖИВЫМ WS-логом сборки (ADR-023). Возвращает
    task_id; фронт открывает /api/services/ws/redeploy/{task_id} и читает лог. Сама
    сборка/свап идут в фоне (build-first + атомарность сохранены)."""
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")
    artifact_id = data.get("artifact_id")
    artifact = crud.get_artifact(db, artifact_id) if artifact_id else None
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    task_id = str(uuid.uuid4())
    asyncio.create_task(build_service.perform_streaming_redeploy(task_id, dep.id, artifact.id))
    return {"task_id": task_id}


# WS не защищаем токеном — доступ по непредсказуемому task_id (как у выпуска SSL).
@app.websocket("/api/services/ws/redeploy/{task_id}")
async def redeploy_ws(websocket: WebSocket, task_id: str):
    await ws_manager.connect(websocket, task_id)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        ws_manager.disconnect(task_id)


@app.delete("/api/services/{service_id}", status_code=204, tags=["Services Compatibility Layer"])
def delete_service_compat(service_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")

    if dep.applications:
        app_names = ", ".join([app.name for app in dep.applications])
        raise HTTPException(
            status_code=409,
            detail=f"Нельзя удалить, так как на этот сервис ссылаются приложения: {app_names}."
        )

    client = get_docker_client()
    for inst in dep.instances:
        try:
            container = client.containers.get(inst.container_name)
            container.stop()
            container.remove(v=True)
        except Exception:
            pass

    crud.delete_deployment(db, service_id)
    return Response(status_code=204)


@app.get("/api/services/{service_id}/logs", tags=["Services Compatibility Layer"])
def get_service_logs_compat(service_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Логи + диагностика сервиса — для понимания, почему он умер/не запускается.

    Работает и для НЕ запущенных контейнеров: у остановленного (failed) контейнера
    логи краша сохраняются Docker'ом; если контейнер уже удалён — отдаём снимок
    логов, снятый оркестратором в момент отказа (`Instance.last_logs`). Если
    контейнер вовсе не создан из-за ошибки сборки — отдаём лог сборки с Deployment.
    """
    dep = crud.get_deployment(db, service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Service not found")

    if not dep.instances:
        if dep.last_build_log:
            return JSONResponse(content={
                "logs": dep.last_build_log, "status": "build_failed",
                "exit_code": None, "restart_count": 0,
            })
        return JSONResponse(content={
            "logs": "Контейнер ещё не создан или сервис остановлен.",
            "status": "offline", "exit_code": None, "restart_count": 0,
        })

    inst = dep.instances[0]
    payload = {
        "status": inst.status, "exit_code": inst.exit_code,
        "restart_count": inst.restart_count or 0, "logs": "",
        "oom_killed": False, "log_driver": None, "logs_readable": True,
        "state_error": None, "diagnosis": None,
    }
    try:
        client = get_docker_client()
        container = client.containers.get(inst.container_name)
        diag = docker_manager.get_container_diagnostics(container)
        if payload["exit_code"] is None:
            payload["exit_code"] = diag["exit_code"]
        payload.update({k: diag[k] for k in
                        ("oom_killed", "log_driver", "logs_readable", "state_error")})
        payload["logs"] = docker_manager.get_container_logs(container, tail=200)
    except Exception:
        # Контейнер недоступен/удалён — отдаём сохранённый при падении снимок.
        payload["logs"] = inst.last_logs or "Логи недоступны (контейнер удалён)."
    payload["diagnosis"] = _diagnose_service(payload)
    return JSONResponse(content=payload)


def _diagnose_service(p: dict) -> str | None:
    """Одна человекочитаемая строка «что случилось» — классифицирует сбой для UI.

    Отделяет проблему ПРИЛОЖЕНИЯ (вышло/упало) от проблемы ХОСТА (logging-драйвер) и
    от нехватки ресурсов (OOM), чтобы пользователь понимал, где чинить (запрос
    диагностируемости). Порядок — от самого «жёсткого» сигнала к мягкому."""
    if p.get("status") == "build_failed":
        return "Образ не собрался — причина в логе сборки ниже. Это ошибка сборки/Dockerfile."
    if p.get("oom_killed"):
        return ("Контейнер убит по нехватке памяти (OOM). Приложению не хватило RAM — "
                "уменьшите потребление, добавьте swap или возьмите сервер побольше.")
    if p.get("state_error"):
        return f"Docker не смог запустить контейнер: {p['state_error']}"
    if p.get("exit_code") == 0 and p.get("status") in ("failed", "restarting", "starting", "offline"):
        return ("Контейнер запустился и СРАЗУ завершился с кодом 0 — процесс не остался "
                "работать как сервис (веб-сервер должен держать порт в foreground, не "
                "уходить в фон и не выходить сам). Проверьте команду запуска/entrypoint "
                "приложения. Это поведение приложения, а не деплоера.")
    if p.get("exit_code") not in (None, 0):
        return (f"Контейнер завершился с кодом {p['exit_code']} — приложение упало на "
                "старте. Причина обычно в логе ниже (отсутствует зависимость/переменная "
                "окружения/неверная команда).")
    if p.get("logs_readable") is False:
        return None  # объяснение уже в самом теле логов (LOG_DRIVER_HELP)
    return None


@app.get("/api/services/{service_id}/stats", tags=["Services Compatibility Layer"])
def get_service_stats_compat(service_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    dep = crud.get_deployment(db, service_id)
    if not dep or not dep.instances:
        return JSONResponse(content={"cpu_percent": 0, "memory_usage_mb": 0, "memory_limit_mb": 0})

    inst = dep.instances[0]
    try:
        client = get_docker_client()
        container = client.containers.get(inst.container_name)
        stats = docker_manager.get_container_stats(container)
        return JSONResponse(content=stats)
    except Exception:
        return JSONResponse(content={"cpu_percent": 0, "memory_usage_mb": 0, "memory_limit_mb": 0})


# === Системные метрики (для дашборда) ===

@app.get("/api/system/metrics", tags=["System"])
def get_system_metrics(current_user: CurrentUser):
    """Сводные метрики хоста/Docker/нагрузки для дашборда.

    Устойчив к сбою Docker: всегда отдаёт 200 с null/нулевыми полями, чтобы
    дашборд не падал при недоступности демона.
    """
    try:
        return docker_manager.get_system_metrics()
    except Exception as e:
        print(f"ERROR: get_system_metrics endpoint failed: {e}")
        return {"host": {}, "disk": {}, "load": {}}


@app.get("/api/system/metrics/history", tags=["System"])
def get_system_metrics_history(current_user: CurrentUser, minutes: int = 1440):
    """История метрик хоста (ЦП/память/диск) для графиков дашборда (Ночь 19).

    Точки [t, cpu%, mem%, disk%] раз в минуту за ≤24 ч (кольцевой буфер сэмплера
    `app/services/metrics_history.py`). Только под auth — операционные данные хоста.
    """
    from app.services import metrics_history
    return metrics_history.history(minutes=minutes)


@app.get("/api/host/health", tags=["System"])
def get_host_health(current_user: CurrentUser):
    """Здоровье ХОСТА: диск/память/swap/load/uptime/docker + warnings (Ночь 13).

    Уровень A observability из `21_HOST_OPS.md`: владелец видит переполняющийся
    диск/отсутствие swap ДО падения (инцидент ADR-078), не заходя по SSH. Панель
    показывает виджет на дашборде; ЛК зеркалит снимок в карточку сервера
    (capability `host_health`). Всегда 200: недоступные источники — None.
    Только под auth — host-данные наружу не светим (в отличие от /api/version).
    """
    from app.services import host_health
    try:
        client = get_docker_client()
    except Exception:  # noqa: BLE001 — Docker лежит: снимок без docker-блока
        client = None
    return host_health.collect(client)


@app.get("/api/operation-metrics", tags=["System"])
def get_operation_metrics(current_user: CurrentUser, db: Session = Depends(get_db),
                          limit: int = 100):
    """Замеры долгих операций ноды + средние (Ночь 14, ADR-082; capability `op_metrics`).

    Средние (по kind: build/ssl_issue/dns_wait/self_update) питают ETA в UI;
    сырые строки — диагностика «что тормозит именно на этой ноде». ЛК зеркалит
    `stats` в супер-админку (аналитика бутылочных горлышек по всем нодам).
    Только под auth: длительности операций — операционные данные владельца.
    """
    limit = max(1, min(limit, 500))
    rows = crud.list_operation_metrics(db, limit=limit)
    return {
        "stats": crud.operation_stats(db),
        "rows": [{
            "id": r.id, "kind": r.kind, "subject": r.subject,
            "duration_seconds": r.duration_seconds, "outcome": r.outcome,
            "meta": r.meta,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


# === УРОВЕНЬ 1: API для Библиотеки (Blueprints & Artifacts) ===

@app.get("/api/blueprints", response_model=List[schemas.AppBlueprint], tags=["Level 1: Blueprints"])
def get_blueprints(current_user: CurrentUser, db: Session = Depends(get_db)):
    return crud.get_blueprints(db)


@app.post("/api/blueprints", response_model=schemas.AppBlueprint, status_code=201, tags=["Level 1: Blueprints"])
def create_blueprint(blueprint: schemas.AppBlueprintCreate, current_user: CurrentUser, db: Session = Depends(get_db)):
    if crud.get_blueprint_by_name(db, blueprint.name):
        raise HTTPException(status_code=400, detail="Приложение с таким именем уже существует.")
    return crud.create_blueprint(db, blueprint)


@app.patch("/api/blueprints/{blueprint_id}", response_model=schemas.AppBlueprint, tags=["Level 1: Blueprints"])
def update_blueprint(blueprint_id: int, data: schemas.AppBlueprintUpdate, current_user: CurrentUser, db: Session = Depends(get_db)):
    bp = crud.get_blueprint(db, blueprint_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Приложение (blueprint) не найдено.")
    if data.name and data.name != bp.name:
        existing = crud.get_blueprint_by_name(db, data.name)
        if existing and existing.id != blueprint_id:
            raise HTTPException(status_code=400, detail="Приложение с таким именем уже существует.")
    return crud.update_blueprint(db, blueprint_id, data)


@app.delete("/api/blueprints/{blueprint_id}", status_code=204, tags=["Level 1: Blueprints"])
def delete_blueprint(blueprint_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    bp = crud.get_blueprint(db, blueprint_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Приложение (blueprint) не найдено.")

    if bp.deployments:
        names = ", ".join(sorted({d.blueprint.name for d in bp.deployments}))
        raise HTTPException(
            status_code=409,
            detail=f"Нельзя удалить: есть запущенные сервисы на этом приложении ({names}). "
                   f"Сначала удалите сервисы."
        )

    crud.delete_blueprint(db, blueprint_id)
    return Response(status_code=204)


@app.delete("/api/blueprints/{blueprint_id}/artifacts/{artifact_id}", status_code=204, tags=["Level 1: Blueprints"])
def delete_artifact(blueprint_id: int, artifact_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    artifact = crud.get_artifact(db, artifact_id)
    if not artifact or artifact.blueprint_id != blueprint_id:
        raise HTTPException(status_code=404, detail="Версия (артефакт) не найдена.")

    if artifact.deployments:
        raise HTTPException(
            status_code=409,
            detail=f"Нельзя удалить версию '{artifact.version_tag}': она используется "
                   f"запущенным сервисом. Сначала обновите или удалите сервис."
        )

    zip_hash = artifact.zip_hash
    crud.delete_artifact(db, artifact_id)

    # Удаляем файл только если на этот zip больше никто не ссылается (дедупликация по hash).
    if crud.count_artifacts_by_hash(db, zip_hash) == 0:
        zip_path = UPLOADS_DIR / f"{zip_hash}.zip"
        try:
            zip_path.unlink(missing_ok=True)
        except OSError as e:
            print(f"WARNING: не удалось удалить файл артефакта {zip_path}: {e}")

    return Response(status_code=204)


@app.post("/api/blueprints/{blueprint_id}/artifacts/inspect", tags=["Level 1: Blueprints"])
async def inspect_artifact_zip(
        blueprint_id: int,
        zip_file: Annotated[UploadFile, File()],
        current_user: CurrentUser, db: Session = Depends(get_db)
):
    """Подсказки для формы загрузки версии: предлагаемый тег и описание из ZIP.

    Не сохраняет артефакт — только инспектирует архив и считает следующий тег.
    """
    bp = crud.get_blueprint(db, blueprint_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Приложение (blueprint) не найдено.")

    content = await _read_upload_capped(zip_file)  # V-07: лимит размера
    meta = artifact_utils.inspect_zip(content)
    existing_tags = [a.version_tag for a in bp.artifacts]
    suggested = meta.get("version") or artifact_utils.suggest_next_version(existing_tags)
    return {"suggested_version": suggested, "description": meta.get("description")}


# Максимальный размер импортируемого архива (защита от OOM при импорте из GitHub).
MAX_IMPORT_BYTES = 150 * 1024 * 1024  # 150 MB
# Тот же потолок для ПРЯМОЙ загрузки артефакта (V-07): раньше `await file.read()`
# читал тело без границы — многогиговый аплоуд исчерпывал RAM ноды (OOM/DoS).
MAX_ARTIFACT_BYTES = 150 * 1024 * 1024  # 150 MB


async def _read_upload_capped(upload: UploadFile) -> bytes:
    """Читает загружаемый файл в память с верхней границей MAX_ARTIFACT_BYTES (V-07).

    Обрывается на превышении (413), не дожидаясь конца потока, — чтобы огромный
    аплоуд не «съел» память до проверки. Читаем модульный `MAX_ARTIFACT_BYTES`
    (не default-аргумент) — так лимит патчится в тестах.
    """
    limit = MAX_ARTIFACT_BYTES
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Файл слишком большой (>{limit // (1024 * 1024)} МБ).")
        chunks.append(chunk)
    return b"".join(chunks)


def _create_artifact_from_zip_bytes(db, bp, content: bytes, version_tag: str, description: str):
    """Сохраняет ZIP-байты как версию (артефакт): хэш+дедуп, авто-фолбэки тега/описания.

    Общий путь для загрузки ZIP и импорта из GitHub (tarball → zip).
    """
    zip_hash = hashlib.sha256(content).hexdigest()
    stored_zip_path = UPLOADS_DIR / f"{zip_hash}.zip"
    if not stored_zip_path.exists():
        stored_zip_path.write_bytes(content)

    # Авто-фолбэки: если поля пустые — тянем из ZIP / генерируем следующий тег.
    version_tag = (version_tag or "").strip()
    description = (description or "").strip()
    if not version_tag or not description:
        meta = artifact_utils.inspect_zip(content)
        if not version_tag:
            existing_tags = [a.version_tag for a in bp.artifacts]
            version_tag = meta.get("version") or artifact_utils.suggest_next_version(existing_tags)
        if not description:
            description = meta.get("description") or ""

    artifact_data = schemas.ArtifactCreate(
        version_tag=version_tag,
        description=description or None,
        zip_hash=zip_hash,
        stored_zip_path=stored_zip_path.as_posix(),  # forward-slash: кроссплатформенно (Linux-контейнер)
        blueprint_id=bp.id,
    )
    try:
        return crud.create_artifact(db, artifact_data)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Версия с тегом '{version_tag}' уже существует для этого приложения."
        )


@app.post("/api/blueprints/{blueprint_id}/artifacts", response_model=schemas.Artifact, tags=["Level 1: Blueprints"])
async def upload_artifact(
        blueprint_id: int,
        zip_file: Annotated[UploadFile, File()],
        current_user: CurrentUser, db: Session = Depends(get_db),
        version_tag: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
):
    bp = crud.get_blueprint(db, blueprint_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Приложение (blueprint) не найдено.")

    content = await _read_upload_capped(zip_file)  # V-07: лимит размера
    return _create_artifact_from_zip_bytes(db, bp, content, version_tag, description)


@app.get("/api/blueprints/{blueprint_id}/artifacts/{artifact_id}/download", tags=["Level 1: Blueprints"])
def download_artifact(blueprint_id: int, artifact_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Отдаёт загруженный ZIP версии для скачивания (под auth)."""
    artifact = crud.get_artifact(db, artifact_id)
    if not artifact or artifact.blueprint_id != blueprint_id:
        raise HTTPException(status_code=404, detail="Версия не найдена.")
    path = Path(artifact.stored_zip_path.replace("\\", "/"))
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл артефакта отсутствует на диске.")
    filename = f"{artifact.blueprint.name}-{artifact.version_tag}.zip"
    return FileResponse(path, media_type="application/zip", filename=filename)


@app.post("/api/blueprints/{blueprint_id}/artifacts/from-github", response_model=schemas.Artifact, tags=["Level 1: Blueprints"])
async def import_artifact_from_github(
        blueprint_id: int,
        data: schemas.GithubImportRequest,
        current_user: CurrentUser, db: Session = Depends(get_db),
):
    """Импортирует версию из GitHub-репозитория по URL.

    Тянет codeload-tarball (без git-бинарника), конвертирует в наш ZIP-формат и
    сохраняет как версию. Публичные репо работают без подключения; для приватных —
    подключите GitHub-аккаунт (`/api/integrations/github`, ADR-033) — токен
    добавится в запрос автоматически.
    """
    bp = crud.get_blueprint(db, blueprint_id)
    if not bp:
        raise HTTPException(status_code=404, detail="Приложение (blueprint) не найдено.")

    parsed = artifact_utils.parse_github_repo(data.repo_url)
    if not parsed:
        raise HTTPException(status_code=400, detail="Не похоже на ссылку GitHub-репозитория.")
    owner, repo, ref_from_url = parsed

    # Порядок проб ref: явный из формы → из /tree/<ref> → main → master.
    refs_to_try, seen = [], set()
    for r in (data.ref, ref_from_url, "main", "master"):
        r = (r or "").strip()
        if r and r not in seen:
            seen.add(r)
            refs_to_try.append(r)

    # Если GitHub-аккаунт подключён — добавляем токен (нужен для приватных репо;
    # для публичных не мешает и снимает rate-limit анонимных запросов codeload).
    headers = {}
    gh_conn = crud.get_github_connection(db)
    if gh_conn:
        from app.secret_box import get_secret_box
        headers["Authorization"] = f"Bearer {get_secret_box().open(gh_conn.token_secret)}"

    tar_bytes = None
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        for ref in refs_to_try:
            url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}"
            try:
                resp = await http.get(url, headers=headers)
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                tar_bytes = resp.content
                break

    if tar_bytes is None:
        hint = ("Убедитесь, что репозиторий публичный, или подключите GitHub-аккаунт "
                "для доступа к приватным." if not gh_conn else
                "Проверьте права токена на этот репозиторий (Repository access).")
        raise HTTPException(
            status_code=404,
            detail=f"Репозиторий или ветка не найдены (пробовал: {', '.join(refs_to_try)}). {hint}"
        )
    if len(tar_bytes) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Архив репозитория слишком большой (>150 МБ).")

    try:
        content = artifact_utils.tarball_to_zip(tar_bytes)
    except Exception:
        raise HTTPException(status_code=422, detail="Не удалось распаковать архив репозитория.")

    # Если тег не задан — пусть хелпер возьмёт из VERSION или авто-бампнет (ref вроде
    # 'main' плохой тег версии; явный тег/файл VERSION предпочтительнее).
    return _create_artifact_from_zip_bytes(db, bp, content, data.version_tag, data.description)


# --- Подключение GitHub-аккаунта (ADR-033): приватные репо в Библиотеке ---

@app.get("/api/integrations/github", response_model=schemas.GithubConnectionStatus, tags=["Integrations"])
def get_github_integration(current_user: CurrentUser, db: Session = Depends(get_db)):
    conn = crud.get_github_connection(db)
    if not conn:
        return schemas.GithubConnectionStatus(connected=False)
    from app.secret_box import get_secret_box
    token = get_secret_box().open(conn.token_secret)
    return schemas.GithubConnectionStatus(
        connected=True, login=conn.login, masked_token=get_secret_box().mask(token))


@app.post("/api/integrations/github", response_model=schemas.GithubConnectionStatus, tags=["Integrations"])
async def connect_github_integration(
        data: schemas.GithubConnectionIn,
        current_user: CurrentUser, db: Session = Depends(get_db),
):
    """Проверяет PAT на живом API и сохраняет его зашифрованным (`SecretBox`)."""
    token = data.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Токен не может быть пустым.")
    try:
        login = await github_client.validate_token(token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    from app.secret_box import get_secret_box
    box = get_secret_box()
    crud.set_github_connection(db, token_secret=box.seal(token), login=login)
    return schemas.GithubConnectionStatus(connected=True, login=login, masked_token=box.mask(token))


@app.delete("/api/integrations/github", tags=["Integrations"])
def disconnect_github_integration(current_user: CurrentUser, db: Session = Depends(get_db)):
    crud.delete_github_connection(db)
    return {"status": "disconnected"}


@app.get("/api/integrations/github/repos", response_model=List[schemas.GithubRepo], tags=["Integrations"])
async def list_github_repos(current_user: CurrentUser, db: Session = Depends(get_db)):
    conn = crud.get_github_connection(db)
    if not conn:
        raise HTTPException(status_code=400, detail="GitHub-аккаунт не подключён.")
    from app.secret_box import get_secret_box
    token = get_secret_box().open(conn.token_secret)
    try:
        repos = await github_client.list_repos(token)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ошибка GitHub API: {e}")
    return repos


# --- DNS-интеграция «домен из готового» (ADR-057) ---
# Деплоер сам DNS-записи НЕ создаёт (API Рег.ру доступен только с egress ЛК):
# ЛК пушит список зон, UI создаёт заявки, реконсайлер ЛК их исполняет (PULL).

@app.get("/api/integrations/dns", response_model=schemas.DnsIntegrationStatus, tags=["Integrations"])
def get_dns_integration(current_user: CurrentUser, db: Session = Depends(get_db)):
    zones = [z.domain for z in crud.list_dns_zones(db)]
    return schemas.DnsIntegrationStatus(connected=bool(zones), zones=zones)


@app.post("/api/integrations/dns", response_model=schemas.DnsIntegrationStatus, tags=["Integrations"])
def set_dns_integration(
        data: schemas.DnsZonesIn,
        current_user: CurrentUser, db: Session = Depends(get_db),
):
    """Полная замена списка управляемых зон (пуш контрол-плейна, паттерн ADR-055)."""
    zones = [z.domain for z in crud.replace_dns_zones(db, data.zones)]
    return schemas.DnsIntegrationStatus(connected=bool(zones), zones=zones)


@app.post("/api/dns/requests", response_model=schemas.DnsRecordRequestOut, status_code=201,
          tags=["Integrations"])
def create_dns_request(
        data: schemas.DnsRecordRequestIn,
        current_user: CurrentUser, db: Session = Depends(get_db),
):
    """Заявка на A-запись `subdomain.zone → IP этой ноды` (из модалки публикации)."""
    known = {z.domain for z in crud.list_dns_zones(db)}
    if data.zone not in known:
        raise HTTPException(status_code=400,
                            detail="Зона не подключена (список зон задаёт контрол-плейн).")
    fqdn = f"{data.subdomain}.{data.zone}"
    existing = crud.get_dns_request_by_fqdn(db, fqdn)
    if existing:
        if existing.status == "error":
            # Повторная заявка после ошибки — перезапускаем (идемпотентный ретрай).
            return crud.complete_dns_request(db, existing.id, "pending", note=None)
        return existing  # pending/created — идемпотентно отдаём как есть
    return crud.create_dns_request(db, zone=data.zone, subdomain=data.subdomain, fqdn=fqdn)


@app.get("/api/dns/requests", response_model=List[schemas.DnsRecordRequestOut], tags=["Integrations"])
def list_dns_requests(
        current_user: CurrentUser, db: Session = Depends(get_db), status: str | None = None,
):
    return crud.list_dns_requests(db, status=status)


@app.post("/api/dns/requests/{request_id}/complete", response_model=schemas.DnsRecordRequestOut,
          tags=["Integrations"])
def complete_dns_request(
        request_id: int,
        data: schemas.DnsRecordRequestComplete,
        current_user: CurrentUser, db: Session = Depends(get_db),
):
    """Исполнитель (ЛК) отмечает заявку: created (A-запись есть) | error (+заметка)."""
    req = crud.complete_dns_request(db, request_id, data.status, data.note)
    if not req:
        raise HTTPException(status_code=404, detail="Заявка не найдена.")
    return req


# === УРОВЕНЬ 3: API для Приложений (публичные точки входа) ===

@app.get("/api/applications", tags=["Level 3: Applications"])
def get_applications(current_user: CurrentUser, db: Session = Depends(get_db)):
    apps = crud.get_applications(db)
    result = []
    for app_item in apps:
        result.append({
            "id": app_item.id,
            "name": app_item.name,
            "domain": app_item.domain,
            "ssl_cert_name": app_item.ssl_cert_name,
            "deployment_id": app_item.deployment_id,
            "deployment": {
                "target_replicas": app_item.deployment.target_replicas,
                "group_name": app_item.deployment.group_name
            },
            "service": {
                "id": app_item.deployment_id,
                "name": app_item.deployment.name or app_item.deployment.blueprint.name
            },
            "users": [
                {"id": u.id, "username": u.username, "application_id": u.application_id}
                for u in app_item.users
            ]
        })
    return result


@app.post("/api/applications", tags=["Level 3: Applications"])
def create_application(
        app_data: schemas.ApplicationCreate,
        background_tasks: BackgroundTasks,
        current_user: CurrentUser,
        db: Session = Depends(get_db)
):
    if crud.get_application_by_name(db, app_data.name) or crud.get_application_by_domain(db, app_data.domain):
        raise HTTPException(status_code=400, detail="Приложение с таким именем или доменом уже существует.")

    dep_id = app_data.deployment_id or app_data.service_id
    if not dep_id:
        raise HTTPException(status_code=400, detail="Необходимо указать deployment_id или service_id.")

    deployment = crud.get_deployment(db, dep_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment для публикации не найден.")

    db_app = crud.create_application(db, app_data)

    nginx_manager.update_application_nginx_config(
        app_name=db_app.name,
        domain=db_app.domain,
        ssl_cert_name=db_app.ssl_cert_name
    )
    background_tasks.add_task(nginx_manager.reload_nginx)

    return {
        "id": db_app.id,
        "name": db_app.name,
        "domain": db_app.domain,
        "ssl_cert_name": db_app.ssl_cert_name,
        "deployment_id": db_app.deployment_id,
        "deployment": {
            "target_replicas": deployment.target_replicas,
            "group_name": deployment.group_name
        },
        "service": {
            "id": deployment.id,
            "name": deployment.name or deployment.blueprint.name
        },
        "users": []
    }


@app.patch("/api/applications/{app_id}", tags=["Level 3: Applications"])
def update_application(
        app_id: int,
        data: schemas.ApplicationUpdate,
        background_tasks: BackgroundTasks,
        current_user: CurrentUser,
        db: Session = Depends(get_db)
):
    db_app = crud.get_application(db, app_id)
    if not db_app:
        raise HTTPException(status_code=404, detail="Приложение не найдено.")

    if data.domain and data.domain != db_app.domain:
        existing = crud.get_application_by_domain(db, data.domain)
        if existing and existing.id != app_id:
            raise HTTPException(status_code=400, detail="Приложение с таким доменом уже существует.")

    updated = crud.update_application(db, app_id, data)

    # Имя приложения не меняется → конфиг перезаписывается под тем же именем.
    nginx_manager.update_application_nginx_config(
        app_name=updated.name,
        domain=updated.domain,
        ssl_cert_name=updated.ssl_cert_name
    )
    background_tasks.add_task(nginx_manager.reload_nginx)

    return {
        "id": updated.id,
        "name": updated.name,
        "domain": updated.domain,
        "ssl_cert_name": updated.ssl_cert_name,
        "deployment_id": updated.deployment_id,
        "service": {"id": updated.deployment_id, "name": updated.deployment.name or updated.deployment.blueprint.name},
    }


@app.delete("/api/applications/{app_id}", status_code=204, tags=["Level 3: Applications"])
def delete_application(
        app_id: int,
        background_tasks: BackgroundTasks,
        current_user: CurrentUser,
        db: Session = Depends(get_db)
):
    db_app = crud.get_application(db, app_id)
    if not db_app:
        raise HTTPException(status_code=404, detail="Приложение не найдено.")

    nginx_manager.remove_application_nginx_config(app_name=db_app.name)
    background_tasks.add_task(nginx_manager.reload_nginx)
    db.delete(db_app)
    db.commit()
    return Response(status_code=204)


# === API для Групп портов ===

@app.get("/api/groups", response_model=List[schemas.AppGroup], tags=["Settings"])
def read_groups(current_user: CurrentUser, db: Session = Depends(get_db)):
    return crud.get_groups(db)


@app.post("/api/groups", response_model=schemas.AppGroup, tags=["Settings"])
def create_group(group: schemas.AppGroupCreate, current_user: CurrentUser, db: Session = Depends(get_db)):
    if crud.get_group_by_name(db, name=group.name):
        raise HTTPException(status_code=400, detail="Группа с таким именем уже существует")
    if group.start_port >= group.end_port:
        raise HTTPException(status_code=422, detail="Начальный порт должен быть меньше конечного")
    return crud.create_group(db=db, group=group)


@app.delete("/api/groups/{group_id}", status_code=204, tags=["Settings"])
def delete_group(group_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    db_group = crud.get_group(db, group_id)
    if not db_group:
        raise HTTPException(status_code=404, detail="Группа не найдена")

    deps_in_group = db.query(models.Deployment).filter(models.Deployment.group_name == db_group.name).all()
    if deps_in_group:
        raise HTTPException(
            status_code=409,
            detail=f"Нельзя удалить группу '{db_group.name}', так как она используется деплоями ({len(deps_in_group)} шт.)."
        )

    crud.delete_group(db, group_id)
    return Response(status_code=204)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7999)