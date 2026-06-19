# --- app/bootstrap.py ---
"""Первичная инициализация при первом запуске (онбординг)."""
import os
import secrets

from sqlalchemy.orm import Session

from app import models
from app.security import get_password_hash


def ensure_admin_exists(db: Session) -> None:
    """Если в системе нет ни одного пользователя — создаёт администратора.

    Источник учётных данных:
      - `DEPLOYER_ADMIN_USERNAME` / `DEPLOYER_ADMIN_PASSWORD` из окружения, либо
      - логин `admin` и СЛУЧАЙНЫЙ пароль, который печатается в лог ОДИН раз
        (установочный скрипт достаёт его из `docker compose logs`).

    Это убирает ручной `create_admin.py` и делает установку «одной командой».
    """
    if db.query(models.User).first():
        return  # администратор уже существует — ничего не делаем

    username = (os.environ.get("DEPLOYER_ADMIN_USERNAME") or "admin").strip() or "admin"
    env_password = os.environ.get("DEPLOYER_ADMIN_PASSWORD")
    generated = env_password is None
    password = env_password or secrets.token_urlsafe(12)

    admin = models.User(username=username, hashed_password=get_password_hash(password))
    db.add(admin)
    db.commit()

    if generated:
        line = "=" * 64
        print(line)
        print("  СОЗДАН АДМИНИСТРАТОР ПАНЕЛИ (первый запуск)")
        print(f"  Логин:  {username}")
        print(f"  Пароль: {password}")
        print("  Сохраните пароль — он показывается только один раз!")
        print(line)
    else:
        print(f"INFO: Администратор '{username}' создан из переменных окружения.")
