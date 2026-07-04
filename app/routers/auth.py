# --- НОВЫЙ ФАЙЛ: app/routers/auth.py ---

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app import crud, schemas, security
from app.database import get_db
from app.models import User
from app.rate_limit import login_limiter, client_keys

router = APIRouter(
    prefix="/api/auth",
    tags=["Authentication"]
)

@router.post("/token", response_model=security.Token)
def login_for_access_token(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Session = Depends(get_db)
):
    # Анти-брутфорс: лимит неудачных попыток по IP клиента и имени пользователя.
    keys = client_keys(request, form_data.username)
    wait = login_limiter.retry_after(keys)
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Слишком много попыток входа. Повторите через {wait} с.",
            headers={"Retry-After": str(wait)},
        )

    user = crud.get_user_by_username(db, username=form_data.username)
    if not user or not security.verify_password(form_data.password, user.hashed_password):
        login_limiter.record_failure(keys)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    login_limiter.reset(keys)  # успешный вход — сбрасываем счётчик
    access_token_expires = timedelta(minutes=security.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = security.create_access_token(
        data=security.user_token_claims(user), expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/users/me", response_model=schemas.User)
def read_users_me(current_user: Annotated[User, Depends(security.get_current_user)]):
    """Проверяет токен и возвращает информацию о текущем пользователе."""
    return current_user