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


# --- DNS-интеграция «домен из готового» (ADR-057) ---

class DnsZonesIn(BaseModel):
    """Список зон, которыми управляет контрол-плейн (пуш ЛК → деплоер, замена целиком)."""
    zones: List[DomainStr]


class DnsIntegrationStatus(BaseModel):
    connected: bool  # есть ли зоны (= ЛК пушил список)
    zones: List[str] = []


class DnsRecordRequestIn(BaseModel):
    """Заявка на A-запись из UI публикации: субдомен + зона из известного списка.
    Субдомен валидируется тем же доменным алфавитом (безопасен для nginx/DNS)."""
    zone: DomainStr
    subdomain: DomainStr


class DnsRecordRequestOut(BaseModel):
    id: int
    zone: str
    subdomain: str
    fqdn: str
    status: str
    note: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class DnsRecordRequestComplete(BaseModel):
    """Ответ исполнителя (ЛК): заявка выполнена/провалена + заметка для UI."""
    status: str = Field(..., pattern=r"^(created|error)$")
    note: Optional[str] = None


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


# --- Фоновые задачи панели (Ночь 10, ADR-069) ---

class PendingActionOut(BaseModel):
    id: int
    type: str
    status: str
    title: Optional[str] = None
    log: Optional[str] = None
    result: Optional[str] = None
    created_at: datetime
    # Ночь 14 (ADR-082): текущая стадия активной задачи + честный ETA. Аддитивно
    # (старые клиенты игнорируют); считает список-роут через describe_stage.
    stage: Optional[str] = None            # publish|dns_wait|ssl_issue|self_update
    stage_label: Optional[str] = None      # человекочитаемая стадия
    eta_seconds: Optional[int] = None      # оценка по средним прошлых прогонов
    unpredictable: bool = False            # фаза без честного ETA (DNS) → UI показывает вилку
    hint: Optional[str] = None             # текст вилки («от минут до суток…»)

    model_config = ConfigDict(from_attributes=True)


class PublishAsyncRequest(BaseModel):
    """Асинхронная публикация сервиса: создаётся фоновая задача, UI сразу отпускается.
    Для `issue` задача ждёт распространения DNS (до суток) и выпускает SSL в фоне."""
    service_id: int
    domain: DomainStr  # валидируется (уходит в nginx-конфиг/certbot)
    name: Optional[str] = None  # авто из имени сервиса, если пусто
    ssl_mode: str = Field(default="issue", pattern=r"^(none|issue|existing)$")
    existing_cert: OptionalCertName = None
    # Пикер «домен из готового» (ADR-057): заявка на A-запись создаётся синхронно.
    zone: OptionalDomainStr = None
    subdomain: OptionalDomainStr = None


class IssueSslAsyncRequest(BaseModel):
    domain: DomainStr
    app_id: Optional[int] = None  # если задан — привязать выпущенный сертификат к приложению


class PanelSslAsyncRequest(BaseModel):
    domain: DomainStr


# --- Веб-терминал «для знатоков» (ADR-090) ---

class TerminalCommandIn(BaseModel):
    """Одна админская команда для выполнения на ноде. Панель шлёт её под JWT,
    ЛК/MCP — по cpk-подписи (`token`). Длина/непустота проверяются здесь; содержимое
    команды — на ответственности аутентифицированного администратора."""
    command: str = Field(min_length=1, max_length=4000)
    # cpk-подписанный токен (typ="exec") для машинного вызова из ЛК. Для панельного
    # (человеческого) вызова под JWT — не нужен.
    token: Optional[str] = None