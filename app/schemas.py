# --- app/schemas.py ---

from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime

from app.validators import DomainStr, OptionalDomainStr, OptionalCertName


# --- НОВЫЕ СХЕМЫ ДЛЯ ПОЛЬЗОВАТЕЛЯ ПАНЕЛИ ---
class UserBase(BaseModel):
    username: str


class UserCreate(UserBase):
    password: str


class User(UserBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# --- Схемы для групп (без изменений) ---
class AppGroupBase(BaseModel):
    name: str
    start_port: int
    end_port: int


class AppGroupCreate(AppGroupBase):
    pass


class AppGroup(AppGroupBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# --- УРОВЕНЬ 1: Схемы для кода ---
class ArtifactBase(BaseModel):
    version_tag: str
    description: Optional[str] = None
    zip_hash: str
    stored_zip_path: str


class ArtifactCreate(ArtifactBase):
    blueprint_id: int


class Artifact(ArtifactBase):
    id: int
    created_at: datetime
    blueprint_id: int

    model_config = ConfigDict(from_attributes=True)


class GithubImportRequest(BaseModel):
    """Импорт версии из GitHub-репозитория (публичного, либо приватного — если
    подключён GitHub-аккаунт, см. GithubConnectionIn)."""
    repo_url: str
    ref: Optional[str] = None          # ветка/тег/SHA (опц.; иначе main→master)
    version_tag: Optional[str] = None  # опц.; иначе из VERSION/авто-бамп
    description: Optional[str] = None  # опц.; иначе из CHANGELOG


class GithubConnectionIn(BaseModel):
    """Подключение GitHub-аккаунта (PAT) — ADR-033."""
    token: str


class GithubConnectionStatus(BaseModel):
    connected: bool
    login: Optional[str] = None
    masked_token: Optional[str] = None


class GithubRepo(BaseModel):
    full_name: str
    private: bool


class AppBlueprintBase(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9-]+$")
    description: Optional[str] = None


class AppBlueprintCreate(AppBlueprintBase):
    pass


class AppBlueprintUpdate(BaseModel):
    name: Optional[str] = Field(default=None, pattern=r"^[a-z0-9-]+$")
    description: Optional[str] = None


class AppBlueprint(AppBlueprintBase):
    id: int
    artifacts: List[Artifact] = []

    model_config = ConfigDict(from_attributes=True)


# --- УРОВЕНЬ 2: ОРКЕСТРАЦИЯ (Deployment и Instance) ---

class InstanceBase(BaseModel):
    assigned_port: int
    status: str
    container_id: Optional[str] = None
    container_name: Optional[str] = None

class Instance(InstanceBase):
    id: int
    deployment_id: int
    deployed_at: datetime

    model_config = ConfigDict(from_attributes=True)

class DeploymentBase(BaseModel):
    target_replicas: int = 1
    group_name: str

class DeploymentCreate(DeploymentBase):
    artifact_id: int
    name: Optional[str] = None

class Deployment(DeploymentBase):
    id: int
    blueprint_id: int
    artifact: Artifact
    instances: List[Instance] = []

    model_config = ConfigDict(from_attributes=True)

class DeploymentScaleRequest(BaseModel):
    target_replicas: int


# --- УРОВЕНЬ 3: ПУБЛИЧНАЯ ТОЧКА ВХОДА (ПРИЛОЖЕНИЕ) ---
class AppUserBase(BaseModel):
    username: str

class AppUserCreate(AppUserBase):
    password: str

class AppUser(AppUserBase):
    id: int
    application_id: int
    model_config = ConfigDict(from_attributes=True)

class ApplicationBase(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9-]+$")
    domain: DomainStr  # валидируется (защита от инъекции в nginx-конфиг)

class ApplicationCreate(ApplicationBase):
    deployment_id: Optional[int] = None
    service_id: Optional[int] = None  # Добавлено для совместимости
    ssl_cert_name: OptionalCertName = None


class ApplicationUpdate(BaseModel):
    domain: OptionalDomainStr = None
    ssl_cert_name: OptionalCertName = None

class Application(ApplicationBase):
    id: int
    ssl_cert_name: Optional[str] = None
    deployment_id: int
    deployment: DeploymentBase
    users: List[AppUser] = []

    model_config = ConfigDict(from_attributes=True)

class IssueSSLRequest(BaseModel):
    domain: DomainStr  # валидируется (домен уходит аргументом в certbot)