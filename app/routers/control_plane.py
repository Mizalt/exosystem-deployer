"""Привилегированные эндпоинты контрол-плейна (Ночь 3, ADR-067; 17_IDENTITY §3/§5).

Авторизация — ПОДПИСЬЮ ЛК (`cpk`, Ed25519), не паролем админа:
  • `POST /api/sso/redeem`  — SSO Phase 2: одноразовый подписанный токен → панельная
    сессия (JWT). Пароль админа не участвует вообще.
  • `POST /api/admin/recover` — восстановление доступа: сброс пароля админа на новый
    случайный НЕЗАВИСИМО от текущего (пароль потерян/сменён — не важно).
  • `POST /api/admin/update` / `POST /api/admin/rollback` — самообновление/откат ноды
    (Ночь 11, ADR-071): ставят фоновую задачу `self_update` (updater-джоба на хосте,
    build-first + авто-откат по health — app/services/self_update.py).

**Fail-safe OSS:** без `DEPLOYER_CONTROL_PLANE_KEY` все отвечают 404 (не палим
существование; self-host деплоер без ЛК не открывает лишней поверхности).
"""
from __future__ import annotations

import json
import secrets
from datetime import timedelta

from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from fastapi import Depends

from app import crud, models, schemas, security
from app.database import get_db
from app.rate_limit import client_keys, command_limiter
from app.services import control_plane

router = APIRouter(tags=["Control plane"])

CurrentUser = Annotated[models.User, Depends(security.get_current_user)]


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
        data=security.user_token_claims(user),
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
    # V-05: инвалидируем все ранее выданные токены (утёкший/украденный токен не
    # переживёт восстановление доступа).
    user.token_version = (user.token_version or 1) + 1
    db.commit()
    print(f"INFO: control-plane recover: пароль пользователя '{user.username}' перевыпущен по подписи ЛК.")
    return {"username": user.username, "password": new_password}


class UpdateIn(BaseModel):
    token: str
    ref: str | None = None  # тег/ветка/SHA; пусто = fast-forward текущей ветки


def _enqueue_self_update(db: Session, ref: str | None, title: str) -> dict:
    """Общий enqueue для update/rollback: одна активная задача, предусловия синхронно."""
    from app.services import self_update

    err = self_update.precheck()
    if err:
        raise HTTPException(status_code=400, detail=err)
    for a in crud.list_pending_actions(db, active_only=True):
        if a.type == "self_update":
            raise HTTPException(status_code=409, detail="Обновление уже выполняется.")
    action = crud.create_pending_action(
        db, "self_update", title, json.dumps({"ref": ref}, ensure_ascii=False))
    return {"task_id": action.id, "status": action.status}


@router.post("/api/admin/update")
def admin_update(data: UpdateIn, db: Session = Depends(get_db)):
    """Обновление ноды по подписи ЛК (Ночь 11, ADR-071). Ставит фоновую задачу
    `self_update` и сразу отвечает (инвариант №7: долгие операции не держат вызов).
    Сам своп делает updater-джоба: build-first, health-гейт, авто-откат."""
    _verified_payload(data.token, "update")
    ref = (data.ref or "").strip() or None
    return _enqueue_self_update(
        db, ref, "Обновление деплоера" + (f" → {ref}" if ref else ""))


@router.post("/api/admin/rollback")
def admin_rollback(data: CpkTokenIn, db: Session = Depends(get_db)):
    """Откат на предыдущую версию (из `data/update_state.json`, пишет updater).

    Ночь 16 (ADR-085): страж несовместимого отката — цель ниже
    `MIN_COMPATIBLE_VERSION` (forward-only миграции) отклоняется с объяснением.
    """
    _verified_payload(data.token, "rollback")
    from app.services import self_update

    allowed, _target, reason = self_update.rollback_guard()
    if not allowed:
        raise HTTPException(status_code=409, detail=reason)
    prev = self_update.read_update_state()["previous_ref"]
    return _enqueue_self_update(db, prev, f"Откат деплоера → {prev[:12]}")


@router.get("/api/admin/update-info")
def update_info(current_user: CurrentUser):
    """История версий ноды + состояние отката (Ночь 16, ADR-085) — сырьё модалки
    «Версии» в ЛК (capability `update_info`). Read-only, под панельной авторизацией
    (M2M-креды ЛК; мутаций нет — cpk-подпись не требуется)."""
    from app import version as version_mod
    from app.services import self_update

    state = self_update.read_update_state()
    allowed, target, reason = self_update.rollback_guard()
    return {
        "version": version_mod.get_version(),
        "git_sha": version_mod.git_sha(),
        "min_compatible_version": version_mod.MIN_COMPATIBLE_VERSION,
        "update_state": state,
        # Новые записи первыми — модалка показывает журнал сверху вниз по времени.
        "history": list(reversed(self_update.read_update_history()))[:20],
        "rollback": {"available": bool(state.get("previous_ref")),
                     "target_version": target,
                     "allowed": allowed,
                     "reason": reason},
    }


# --- Веб-терминал «для знатоков» (ADR-090): одна команда → вывод -------------------

def _exec_actor(request: Request, data: schemas.TerminalCommandIn, db: Session) -> str:
    """Аутентифицирует вызов /api/admin/exec и возвращает метку актора для лога.

    Два пути (как у остальной поверхности ноды):
      • **cpk-подпись** (`token`, typ="exec") — машинный вызов из ЛК/MCP. Работает
        даже без панельного пароля, авторизация = сама подпись контрол-плейна.
      • **Панельный JWT** — человек в веб-терминале панели (тот же bearer, что у
        любого защищённого роута). Без токена cpk идём этим путём.
    Любой сбой авторизации → 401/404, команда НЕ выполняется."""
    if data.token:
        payload = _verified_payload(data.token, "exec")   # 404 если cpk выключен, 401 если плохой
        return f"cp:{payload.get('sub') or 'lk'}"
    # Панельный путь: требуем валидный JWT (иначе 401). Ошибку get_current_user
    # переиспользуем «вручную», т.к. эндпоинт принимает и cpk-вариант без bearer.
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Требуется вход в панель.",
                            headers={"WWW-Authenticate": "Bearer"})
    user = security.get_current_user(token=token, db=db)  # бросит 401 при плохом токене
    return f"panel:{user.username}"


@router.post("/api/admin/exec")
def admin_exec(data: schemas.TerminalCommandIn, request: Request,
               db: Session = Depends(get_db)):
    """Выполнить ОДНУ админскую команду на ноде и вернуть вывод (ADR-090).

    🔒 Фича повышенного риска, поэтому «обёрнута сохранениями»:
      • **выключатель** `DEPLOYER_TERMINAL_ENABLED=false` → 403 (ничего не выполняем);
      • **аутентификация** — cpk-подпись (ЛК/MCP) ИЛИ панельный JWT (человек);
      • **rate-limit** — частота вызовов по IP+актору (анти-флуд/DoS);
      • **таймаут + лимит вывода** — в сервисе (`terminal.run_command`);
      • **аудит** — строка в лог процесса ноды (ЛК дополнительно пишет `cloud_audit_log`).
    Возвращает 200 с телом даже при ненулевом коде/таймауте (это НЕ транспортный сбой)."""
    from app.services import terminal

    if not terminal.terminal_enabled():
        raise HTTPException(
            status_code=403,
            detail="Веб-терминал отключён на этой ноде (DEPLOYER_TERMINAL_ENABLED=false).")
    actor = _exec_actor(request, data, db)
    # Rate-limit ПОСЛЕ аутентификации: ключи по IP клиента и актору (одна учётка →
    # флуд с ротацией IP ловится по актору).
    wait = command_limiter.check_and_record(client_keys(request, actor))
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много команд подряд. Повторите через {wait} с.",
            headers={"Retry-After": str(wait)})
    result = terminal.run_command(data.command)
    # Аудит ноды: у ядра нет БД-журнала (ADR-090) — пишем в stdout процесса, куда
    # смотрит владелец сервера (docker logs). Команду НЕ обрезаем — знать, что
    # выполнялось, важнее краткости; секретов в команде мы не добавляем.
    print(f"AUDIT: terminal exec by {actor}: exit={result['exit_code']} "
          f"timed_out={result['timed_out']} cmd={result['command']!r}")
    return result
