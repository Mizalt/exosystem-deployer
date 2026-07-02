"""Привилегированные эндпоинты контрол-плейна (Ночь 3, ADR-067; 17_IDENTITY §3/§5).

Авторизация — ПОДПИСЬЮ ЛК (`cpk`, Ed25519), не паролем админа:
  • `POST /api/sso/redeem`  — SSO Phase 2: одноразовый подписанный токен → панельная
    сессия (JWT). Пароль админа не участвует вообще.
  • `POST /api/admin/recover` — восстановление доступа: сброс пароля админа на новый
    случайный НЕЗАВИСИМО от текущего (пароль потерян/сменён — не важно).

**Fail-safe OSS:** без `DEPLOYER_CONTROL_PLANE_KEY` оба отвечают 404 (не палим
существование; self-host деплоер без ЛК не открывает лишней поверхности).
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from fastapi import Depends

from app import crud, security
from app.database import get_db
from app.services import control_plane

router = APIRouter(tags=["Control plane"])


class CpkTokenIn(BaseModel):
    token: str


def _verified_payload(token: str, typ: str) -> dict:
    if not control_plane.cpk_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        return control_plane.verify_token(token, typ)
    except control_plane.ControlPlaneError as e:
        raise HTTPException(status_code=401, detail=str(e))


def _subject_user(db: Session, payload: dict):
    username = payload.get("sub") or "admin"
    user = crud.get_user_by_username(db, username=username)
    if not user:
        raise HTTPException(status_code=401, detail="Пользователь не найден.")
    return user


@router.post("/api/sso/redeem")
def sso_redeem(data: CpkTokenIn, db: Session = Depends(get_db)):
    """SSO Phase 2: подписанный ЛК одноразовый токен (≤60 c) → панельный JWT."""
    payload = _verified_payload(data.token, "sso")
    user = _subject_user(db, payload)
    access_token = security.create_access_token(
        data={"sub": user.username},
        expires_delta=timedelta(minutes=security.ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/api/admin/recover")
def admin_recover(data: CpkTokenIn, db: Session = Depends(get_db)):
    """Восстановление доступа: сброс пароля админа по подписи ЛК (17_IDENTITY §5).

    Деплоер сам генерирует новый пароль и возвращает его контрол-плейну — ЛК
    сохранит его зашифрованным (`Deployer.admin_password_secret`) и продолжит
    SSO Phase 1/показ кредов владельцу. Токен одноразовый, ≤60 c."""
    payload = _verified_payload(data.token, "recover")
    user = _subject_user(db, payload)
    new_password = secrets.token_urlsafe(12)
    user.hashed_password = security.get_password_hash(new_password)
    db.commit()
    print(f"INFO: control-plane recover: пароль пользователя '{user.username}' перевыпущен по подписи ЛК.")
    return {"username": user.username, "password": new_password}
