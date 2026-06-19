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

import uvicorn
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, Response, UploadFile)
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from starlette.staticfiles import StaticFiles

from app import crud, schemas, panel_config, models, security, config, bootstrap
from app import environment
from app.environment import get_docker_client
from app.database import get_db, init_db_with_migrations, SessionLocal
from app.routers import proxy, ssl, panel, auth
from app.services import docker_manager, nginx_manager, nginx_service

import asyncio
from app.services.orchestrator import run_orchestrator_loop

UPLOADS_DIR = Path("uploads")
UPLOADS_DIR.mkdir(exist_ok=True)

# Инициализация БД выполняется в lifespan (startup), а не на уровне модуля —
# чтобы импорт main не имел побочных эффектов (важно для тестов).


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("--- Running startup tasks ---")
    print(f"INFO: Environment: {environment.describe()}")
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
    print("SUCCESS: Infrastructure ready and Orchestrator started.")

    yield

    print("--- Running shutdown tasks ---")
    orchestrator_task.cancel()
    try:
        await orchestrator_task
    except asyncio.CancelledError:
        print("INFO: Orchestrator task cancelled successfully.")


app = FastAPI(title="Cloud Deploy Panel", lifespan=lifespan)

app.include_router(ssl.router)
app.include_router(proxy.router)
app.include_router(panel.router)
app.include_router(auth.router)

app.mount("/static", StaticFiles(directory="static"), name="static")

CurrentUser = Annotated[models.User, Depends(security.get_current_user)]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def read_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# === СОВМЕСТИМОСТЬ С СЕРВИСАМИ ДЛЯ ФРОНТЕНДА (/api/services) ===

@app.get("/api/services", tags=["Services Compatibility Layer"])
def get_services_compat(current_user: CurrentUser, db: Session = Depends(get_db)):
    """Эмулирует список сервисов из списка деплоев."""
    deployments = crud.get_deployments(db)
    services = []
    for dep in deployments:
        first_instance = dep.instances[0] if dep.instances else None
        port = first_instance.assigned_port if first_instance else 0
        status = first_instance.status if first_instance else "offline"

        services.append({
            "id": dep.id,
            "name": dep.blueprint.name,
            "assigned_port": port,
            "status": status,
            "artifact": {
                "id": dep.artifact.id,
                "version_tag": dep.artifact.version_tag,
                "blueprint_id": dep.blueprint_id,
                "created_at": dep.artifact.created_at.isoformat() if dep.artifact.created_at else None
            }
        })
    return services


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

    dep_data = schemas.DeploymentCreate(
        artifact_id=artifact_id,
        target_replicas=1,
        group_name=group_name
    )
    dep = crud.create_deployment(db, dep_data, blueprint_id=artifact.blueprint_id)

    return {
        "id": dep.id,
        "name": dep.blueprint.name,
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

    dep.artifact_id = artifact_id

    client = get_docker_client()
    for inst in dep.instances:
        try:
            container = client.containers.get(inst.container_name)
            container.stop()
            container.remove(v=True)
        except Exception:
            pass
        db.delete(inst)

    db.commit()
    return {"message": "Service redeployed"}


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
    dep = crud.get_deployment(db, service_id)
    if not dep or not dep.instances:
        return JSONResponse(content={"logs": "Нет активных экземпляров."})

    inst = dep.instances[0]
    try:
        client = get_docker_client()
        container = client.containers.get(inst.container_name)
        logs = docker_manager.get_container_logs(container)
        return JSONResponse(content={"logs": logs})
    except Exception as e:
        return JSONResponse(content={"logs": f"Не удалось получить логи: {e}"})


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


# === УРОВЕНЬ 1: API для Библиотеки (Blueprints & Artifacts) ===

@app.get("/api/blueprints", response_model=List[schemas.AppBlueprint], tags=["Level 1: Blueprints"])
def get_blueprints(current_user: CurrentUser, db: Session = Depends(get_db)):
    return crud.get_blueprints(db)


@app.post("/api/blueprints", response_model=schemas.AppBlueprint, status_code=201, tags=["Level 1: Blueprints"])
def create_blueprint(blueprint: schemas.AppBlueprintCreate, current_user: CurrentUser, db: Session = Depends(get_db)):
    if crud.get_blueprint_by_name(db, blueprint.name):
        raise HTTPException(status_code=400, detail="Приложение с таким именем уже существует.")
    return crud.create_blueprint(db, blueprint)


@app.post("/api/blueprints/{blueprint_id}/artifacts", response_model=schemas.Artifact, tags=["Level 1: Blueprints"])
async def upload_artifact(
        blueprint_id: int,
        version_tag: Annotated[str, Form()],
        zip_file: Annotated[UploadFile, File()],
        current_user: CurrentUser, db: Session = Depends(get_db)
):
    if not crud.get_blueprint(db, blueprint_id):
        raise HTTPException(status_code=404, detail="Приложение (blueprint) не найдено.")

    content = await zip_file.read()
    zip_hash = hashlib.sha256(content).hexdigest()
    stored_zip_path = UPLOADS_DIR / f"{zip_hash}.zip"

    if not stored_zip_path.exists():
        stored_zip_path.write_bytes(content)

    artifact_data = schemas.ArtifactCreate(
        version_tag=version_tag,
        zip_hash=zip_hash,
        stored_zip_path=stored_zip_path.as_posix(),  # forward-slash: кроссплатформенно (Linux-контейнер)
        blueprint_id=blueprint_id
    )
    try:
        return crud.create_artifact(db, artifact_data)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Версия с тегом '{version_tag}' уже существует для этого приложения."
        )


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
                "name": app_item.deployment.blueprint.name
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
            "name": deployment.blueprint.name
        },
        "users": []
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