# --- app/crud.py ---

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from . import models, schemas
from . import security
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_user_by_username(db: Session, username: str):
    """Получить пользователя панели по имени."""
    return db.query(models.User).filter(models.User.username == username).first()

# --- CRUD для Групп портов ---

def get_group_by_name(db: Session, name: str):
    return db.query(models.AppGroup).filter(models.AppGroup.name == name).first()

def get_groups(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.AppGroup).offset(skip).limit(limit).all()

def create_group(db: Session, group: schemas.AppGroupCreate):
    db_group = models.AppGroup(**group.model_dump())
    db.add(db_group)
    db.commit()
    db.refresh(db_group)
    return db_group

def get_group(db: Session, group_id: int):
    return db.query(models.AppGroup).filter(models.AppGroup.id == group_id).first()

def delete_group(db: Session, group_id: int):
    db_group = get_group(db, group_id)
    if db_group:
        db.delete(db_group)
        db.commit()
    return db_group

def get_services_by_group_name(db: Session, group_name: str):
    """Находит все деплои, использующие указанную группу."""
    return db.query(models.Deployment).filter(models.Deployment.group_name == group_name).all()


# --- CRUD для Уровня 1: AppBlueprint и Artifact ---

def get_blueprint(db: Session, blueprint_id: int):
    return db.query(models.AppBlueprint).options(
        joinedload(models.AppBlueprint.artifacts)
    ).filter(models.AppBlueprint.id == blueprint_id).first()

def get_blueprint_by_name(db: Session, name: str):
    return db.query(models.AppBlueprint).filter(models.AppBlueprint.name == name).first()

def get_blueprints(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.AppBlueprint).options(
        joinedload(models.AppBlueprint.artifacts)
    ).order_by(models.AppBlueprint.name).offset(skip).limit(limit).all()

def create_blueprint(db: Session, blueprint: schemas.AppBlueprintCreate):
    db_blueprint = models.AppBlueprint(**blueprint.model_dump())
    db.add(db_blueprint)
    db.commit()
    db.refresh(db_blueprint)
    return db_blueprint

def update_blueprint(db: Session, blueprint_id: int, data: schemas.AppBlueprintUpdate):
    db_blueprint = get_blueprint(db, blueprint_id)
    if not db_blueprint:
        return None
    payload = data.model_dump(exclude_unset=True)
    for field, value in payload.items():
        setattr(db_blueprint, field, value)
    db.commit()
    db.refresh(db_blueprint)
    return db_blueprint

def delete_blueprint(db: Session, blueprint_id: int):
    db_blueprint = get_blueprint(db, blueprint_id)
    if db_blueprint:
        db.delete(db_blueprint)
        db.commit()
    return db_blueprint

def get_artifact(db: Session, artifact_id: int):
    return db.query(models.Artifact).filter(models.Artifact.id == artifact_id).first()

def delete_artifact(db: Session, artifact_id: int):
    db_artifact = get_artifact(db, artifact_id)
    if db_artifact:
        db.delete(db_artifact)
        db.commit()
    return db_artifact

def count_artifacts_by_hash(db: Session, zip_hash: str) -> int:
    """Сколько артефактов ссылаются на один и тот же zip (дедупликация файлов)."""
    return db.query(models.Artifact).filter(models.Artifact.zip_hash == zip_hash).count()

def create_artifact(db: Session, artifact: schemas.ArtifactCreate):
    db_artifact = models.Artifact(**artifact.model_dump())
    db.add(db_artifact)
    db.commit()
    db.refresh(db_artifact)
    return db_artifact


# --- CRUD для Уровня 2: Deployment и Instance ---

def get_deployment(db: Session, deployment_id: int):
    return db.query(models.Deployment).options(
        joinedload(models.Deployment.artifact),
        joinedload(models.Deployment.instances),
        joinedload(models.Deployment.applications)
    ).filter(models.Deployment.id == deployment_id).first()

def get_deployments(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.Deployment).options(
        joinedload(models.Deployment.artifact),
        joinedload(models.Deployment.instances)
    ).order_by(models.Deployment.id.desc()).offset(skip).limit(limit).all()

def create_deployment(db: Session, deployment_data: schemas.DeploymentCreate, blueprint_id: int):
    db_deployment = models.Deployment(
        name=deployment_data.name,
        blueprint_id=blueprint_id,
        artifact_id=deployment_data.artifact_id,
        target_replicas=deployment_data.target_replicas,
        group_name=deployment_data.group_name
    )
    db.add(db_deployment)
    db.commit()
    db.refresh(db_deployment)
    return db_deployment

def get_deployment_by_name(db: Session, name: str):
    return db.query(models.Deployment).filter(models.Deployment.name == name).first()

def delete_deployment(db: Session, deployment_id: int):
    db_dep = get_deployment(db, deployment_id)
    if db_dep:
        db.delete(db_dep)
        db.commit()
    return db_dep

# --- CRUD для Уровня 3: Application ---

def get_application(db: Session, app_id: int):
    return db.query(models.Application).options(
        joinedload(models.Application.deployment),
        joinedload(models.Application.users)
    ).filter(models.Application.id == app_id).first()

def get_application_by_name(db: Session, name: str):
    return db.query(models.Application).options(
        joinedload(models.Application.deployment)
    ).filter(models.Application.name == name).first()

def get_application_by_domain(db: Session, domain: str):
    return db.query(models.Application).filter(models.Application.domain == domain).first()

def get_applications(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.Application).options(
        joinedload(models.Application.deployment).joinedload(models.Deployment.artifact)
    ).order_by(models.Application.name).offset(skip).limit(limit).all()

def update_application(db: Session, app_id: int, data: schemas.ApplicationUpdate):
    db_app = get_application(db, app_id)
    if not db_app:
        return None
    payload = data.model_dump(exclude_unset=True)
    for field, value in payload.items():
        setattr(db_app, field, value)
    db.commit()
    db.refresh(db_app)
    return db_app

def create_application(db: Session, application: schemas.ApplicationCreate):
    dep_id = application.deployment_id or application.service_id
    db_app = models.Application(
        name=application.name,
        domain=application.domain,
        ssl_cert_name=application.ssl_cert_name,
        deployment_id=dep_id
    )
    db.add(db_app)
    db.commit()
    db.refresh(db_app)
    return db_app


# --- Вспомогательные функции и CRUD для пользователей ---

def get_all_used_ports(db: Session) -> set:
    """Возвращает все порты, используемые запущенными экземплярами в БД."""
    return {inst.assigned_port for inst in db.query(models.Instance.assigned_port).all()}

def get_app_user_by_username(db: Session, application_id: int, username: str):
    return db.query(models.AppUser).filter(
        models.AppUser.application_id == application_id,
        models.AppUser.username == username
    ).first()

def create_app_user(db: Session, user: schemas.AppUserCreate, application_id: int):
    hashed_password = security.get_password_hash(user.password)
    db_user = models.AppUser(
        username=user.username,
        hashed_password=hashed_password,
        application_id=application_id
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def verify_password(plain_password, hashed_password):
    return security.verify_password(plain_password, hashed_password)


# --- Подключение GitHub-аккаунта (ADR-033) ---

def get_github_connection(db: Session):
    return db.query(models.GithubConnection).first()


def set_github_connection(db: Session, token_secret: str, login: str | None):
    """Сохраняет (создаёт/перезаписывает) единственную GitHub-связку деплоера."""
    conn = get_github_connection(db)
    if conn:
        conn.token_secret = token_secret
        conn.login = login
    else:
        conn = models.GithubConnection(token_secret=token_secret, login=login)
        db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


def delete_github_connection(db: Session) -> bool:
    conn = get_github_connection(db)
    if not conn:
        return False
    db.delete(conn)
    db.commit()
    return True


# --- DNS-интеграция «домен из готового» (ADR-057) ---

def list_dns_zones(db: Session) -> list[models.DnsZone]:
    return db.query(models.DnsZone).order_by(models.DnsZone.domain).all()


def replace_dns_zones(db: Session, domains: list[str]) -> list[models.DnsZone]:
    """Полная замена списка зон (ЛК пушит актуальный список целиком)."""
    db.query(models.DnsZone).delete()
    zones = [models.DnsZone(domain=d) for d in dict.fromkeys(domains)]  # dedupe, порядок
    db.add_all(zones)
    db.commit()
    return list_dns_zones(db)


def get_dns_request(db: Session, request_id: int) -> models.DnsRecordRequest | None:
    return db.query(models.DnsRecordRequest).filter(
        models.DnsRecordRequest.id == request_id).first()


def get_dns_request_by_fqdn(db: Session, fqdn: str) -> models.DnsRecordRequest | None:
    return db.query(models.DnsRecordRequest).filter(
        models.DnsRecordRequest.fqdn == fqdn).first()


def list_dns_requests(db: Session, status: str | None = None) -> list[models.DnsRecordRequest]:
    q = db.query(models.DnsRecordRequest)
    if status:
        q = q.filter(models.DnsRecordRequest.status == status)
    return q.order_by(models.DnsRecordRequest.id).all()


def create_dns_request(db: Session, zone: str, subdomain: str, fqdn: str) -> models.DnsRecordRequest:
    req = models.DnsRecordRequest(zone=zone, subdomain=subdomain, fqdn=fqdn, status="pending")
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def complete_dns_request(db: Session, request_id: int, status: str,
                         note: str | None = None) -> models.DnsRecordRequest | None:
    req = get_dns_request(db, request_id)
    if not req:
        return None
    req.status = status
    req.note = note
    db.commit()
    db.refresh(req)
    return req


# --- Фоновые задачи панели (Ночь 10, ADR-069) ---

def create_pending_action(db: Session, type: str, title: str | None,
                          params: str | None) -> models.PendingAction:
    action = models.PendingAction(type=type, title=title, params=params, status="pending")
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


def get_pending_action(db: Session, action_id: int) -> models.PendingAction | None:
    return db.query(models.PendingAction).filter(
        models.PendingAction.id == action_id).first()


def list_pending_actions(db: Session, active_only: bool = False,
                         limit: int = 50) -> list[models.PendingAction]:
    q = db.query(models.PendingAction)
    if active_only:
        q = q.filter(models.PendingAction.status.in_(["pending", "running"]))
    return q.order_by(models.PendingAction.id.desc()).limit(limit).all()


def list_due_pending_actions(db: Session, now) -> list[models.PendingAction]:
    """Активные задачи, которым пора: срок не задан или наступил."""
    return db.query(models.PendingAction).filter(
        models.PendingAction.status.in_(["pending", "running"]),
        or_(models.PendingAction.next_check_at.is_(None),
            models.PendingAction.next_check_at <= now),
    ).order_by(models.PendingAction.id).all()


def delete_pending_action(db: Session, action_id: int) -> models.PendingAction | None:
    action = get_pending_action(db, action_id)
    if action:
        db.delete(action)
        db.commit()
    return action