# --- НОВЫЙ ФАЙЛ: app/security.py ---

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import crud
from .database import get_db

# --- Конфигурация ---
import os
import secrets
from pathlib import Path


def _load_or_create_secret_key() -> str:
    """
    Источник JWT-ключа по приоритету:
      1. Переменная окружения DEPLOYER_SECRET_KEY (рекомендуется для прод/контейнера).
      2. Файл secret.key рядом с проектом (генерируется один раз, чтобы JWT
         переживали перезапуск). Файл в .gitignore — в репозиторий не попадает.
    Хардкод ключа удалён намеренно (была дыра безопасности).
    """
    env_key = os.environ.get("DEPLOYER_SECRET_KEY")
    if env_key:
        return env_key

    key_file = Path("data/secret.key")
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()

    new_key = secrets.token_hex(32)
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(new_key, encoding="utf-8")
        print("INFO: Сгенерирован новый JWT secret.key. Для прода задайте DEPLOYER_SECRET_KEY в окружении.")
    except Exception as e:
        print(f"WARN: Не удалось сохранить secret.key ({e}); ключ только в памяти, JWT не переживут рестарт.")
    return new_key


SECRET_KEY = _load_or_create_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 часа


# --- Схемы ---
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None


# --- Утилиты ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# --- Основная зависимость для защиты эндпоинтов ---
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception

    user = crud.get_user_by_username(db, username=token_data.username)
    if user is None:
        raise credentials_exception
    # V-05: токен действителен только для текущей версии токенов пользователя. После
    # admin-recover/смены пароля версия инкрементится → старые токены отклоняются.
    # Токены без claim `ver` (легаси, до фичи) считаем версией 1 — апгрейд не разлогинит.
    token_ver = payload.get("ver", 1)
    if (user.token_version or 1) != token_ver:
        raise credentials_exception
    return user


def user_token_claims(user) -> dict:
    """Claim'ы панельного JWT для пользователя (V-05: включает версию токенов)."""
    return {"sub": user.username, "ver": user.token_version or 1}