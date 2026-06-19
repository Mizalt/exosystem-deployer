# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/models.py ---

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)


# --- УРОВЕНЬ 1: ЛОГИКА И КОД ---

class AppBlueprint(Base):
    """Группировка артефактов по имени приложения, 'репозиторий'."""
    __tablename__ = "app_blueprints"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    artifacts = relationship("Artifact", back_populates="blueprint", cascade="all, delete-orphan")
    deployments = relationship("Deployment", back_populates="blueprint", cascade="all, delete-orphan")


class Artifact(Base):
    """Конкретная версия кода, загруженный ZIP."""
    __tablename__ = "artifacts"
    id = Column(Integer, primary_key=True, index=True)
    version_tag = Column(String, index=True, nullable=False)
    zip_hash = Column(String, nullable=False)
    stored_zip_path = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    blueprint_id = Column(Integer, ForeignKey("app_blueprints.id"), nullable=False)
    blueprint = relationship("AppBlueprint", back_populates="artifacts")
    deployments = relationship("Deployment", back_populates="artifact")

    __table_args__ = (
        UniqueConstraint('blueprint_id', 'version_tag', name='_blueprint_version_uc'),
    )


# --- УРОВЕНЬ 2: ОРКЕСТРАЦИЯ (НОВОЕ) ---

class Deployment(Base):
    """Желаемое состояние: Приложение + Версия (Артефакт) + Количество реплик."""
    __tablename__ = "deployments"
    id = Column(Integer, primary_key=True, index=True)
    blueprint_id = Column(Integer, ForeignKey("app_blueprints.id"), nullable=False)
    artifact_id = Column(Integer, ForeignKey("artifacts.id"), nullable=False)
    target_replicas = Column(Integer, default=1, nullable=False)  # Сколько реплик мы ХОТИМ
    group_name = Column(String, nullable=False)  # Для пула портов

    blueprint = relationship("AppBlueprint", back_populates="deployments")
    artifact = relationship("Artifact", back_populates="deployments")
    instances = relationship("Instance", back_populates="deployment", cascade="all, delete-orphan")
    applications = relationship("Application", back_populates="deployment", cascade="all, delete-orphan")


class Instance(Base):
    """Физический контейнер (Реплика). Создается и удаляется автоматически Оркестратором."""
    __tablename__ = "instances"
    id = Column(Integer, primary_key=True, index=True)
    deployment_id = Column(Integer, ForeignKey("deployments.id"), nullable=False)
    container_id = Column(String, nullable=True)
    container_name = Column(String, nullable=True)
    assigned_port = Column(Integer, unique=True, nullable=False)
    status = Column(String, default="starting")  # starting, online, restarting, failed, offline
    restart_count = Column(Integer, default=0, nullable=False)  # для CrashLoopBackOff
    deployed_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    deployment = relationship("Deployment", back_populates="instances")


# --- УРОВЕНЬ 3: ПУБЛИЧНАЯ ТОЧКА ВХОДА (Nginx) ---
class Application(Base):
    """Публичное приложение: домен + SSL, которое указывает на Deployment (балансировка)."""
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    domain = Column(String, unique=True, index=True, nullable=False)
    ssl_cert_name = Column(String, nullable=True)
    deployment_id = Column(Integer, ForeignKey("deployments.id"), nullable=False)

    deployment = relationship("Deployment", back_populates="applications")
    users = relationship("AppUser", back_populates="application", cascade="all, delete-orphan")


# Вспомогательные модели (AppGroup, AppUser) оставляем без изменений...
class AppGroup(Base):
    __tablename__ = "app_groups"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    start_port = Column(Integer, nullable=False)
    end_port = Column(Integer, nullable=False)


class AppUser(Base):
    __tablename__ = "app_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    application = relationship("Application", back_populates="users")