"""Роуты «центра фоновых задач» панели (Ночь 10, ADR-069).

Enqueue-эндпоинты валидируют вход СИНХРОННО (сразу дать понятную ошибку 400/404),
затем ставят `PendingAction` и мгновенно отпускают UI. Довод до конца (ждать DNS →
опубликовать → выпустить SSL) — фоновый чекер `app/services/pending_actions.py`.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app import crud, models, schemas, security
from app.database import get_db

router = APIRouter(prefix="/api/pending-actions", tags=["Pending actions"])
CurrentUser = Annotated[models.User, Depends(security.get_current_user)]


def _slug(value: str | None) -> str:
    """Имя приложения ограничено `^[a-z0-9-]+$` — приводим автоген к этому алфавиту."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (value or "").lower()).strip("-")
    return slug or "app"


def _unique_app_name(db: Session, base: str) -> str:
    name = _slug(base)
    if not crud.get_application_by_name(db, name):
        return name
    stem, i = name, 2
    while crud.get_application_by_name(db, name):
        name = f"{stem}-{i}"
        i += 1
    return name


@router.get("", response_model=List[schemas.PendingActionOut])
def list_actions(current_user: CurrentUser, db: Session = Depends(get_db),
                 active_only: bool = False):
    return crud.list_pending_actions(db, active_only=active_only)


@router.post("/publish", response_model=schemas.PendingActionOut, status_code=201)
def enqueue_publish(data: schemas.PublishAsyncRequest, current_user: CurrentUser,
                    db: Session = Depends(get_db)):
    """Фоновая публикация сервиса: не держит модалку, пока распространяется DNS/SSL."""
    dep = crud.get_deployment(db, data.service_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Сервис для публикации не найден.")
    if crud.get_application_by_domain(db, data.domain):
        raise HTTPException(status_code=400, detail="Домен уже используется другим приложением.")
    if data.ssl_mode == "existing" and not data.existing_cert:
        raise HTTPException(status_code=400, detail="Выберите существующий сертификат.")

    name = _unique_app_name(db, data.name or dep.name or dep.blueprint.name)

    # Пикер «домен из готового» (ADR-057): заявку на A-запись создаём сразу, её
    # исполнит реконсайлер ЛК, а фоновая задача дождётся распространения DNS.
    # Пустой субдомен при выбранной зоне → apex («@»): публикация на ПОЛНЫЙ домен (Задача 2).
    if data.zone:
        known = {z.domain for z in crud.list_dns_zones(db)}
        if data.zone not in known:
            raise HTTPException(status_code=400,
                                detail="Зона не подключена (список зон задаёт контрол-плейн).")
        sub = (data.subdomain or "").strip() or "@"
        fqdn = data.zone if sub == "@" else f"{sub}.{data.zone}"
        existing = crud.get_dns_request_by_fqdn(db, fqdn)
        if not existing:
            crud.create_dns_request(db, zone=data.zone, subdomain=sub, fqdn=fqdn)
        elif existing.status == "error":
            crud.complete_dns_request(db, existing.id, "pending", note=None)

    params = {
        "domain": data.domain, "name": name, "service_id": data.service_id,
        "ssl_mode": data.ssl_mode, "existing_cert": data.existing_cert,
    }
    return crud.create_pending_action(
        db, type="publish_on_dns", title=f"Публикация {data.domain}",
        params=json.dumps(params, ensure_ascii=False))


@router.post("/issue-ssl", response_model=schemas.PendingActionOut, status_code=201)
def enqueue_issue_ssl(data: schemas.IssueSslAsyncRequest, current_user: CurrentUser,
                      db: Session = Depends(get_db)):
    """Фоновый выпуск SSL для домена (опц. с привязкой к приложению)."""
    if data.app_id is not None and not crud.get_application(db, data.app_id):
        raise HTTPException(status_code=404, detail="Приложение не найдено.")
    params = {"domain": data.domain, "app_id": data.app_id}
    return crud.create_pending_action(
        db, type="issue_ssl", title=f"Выпуск SSL: {data.domain}",
        params=json.dumps(params, ensure_ascii=False))


@router.post("/panel-ssl", response_model=schemas.PendingActionOut, status_code=201)
def enqueue_panel_ssl(data: schemas.PanelSslAsyncRequest, current_user: CurrentUser,
                      db: Session = Depends(get_db)):
    """Фоновый выпуск SSL для домена самой панели (сохранит домен и привяжет сертификат)."""
    params = {"domain": data.domain}
    return crud.create_pending_action(
        db, type="panel_ssl", title=f"SSL панели: {data.domain}",
        params=json.dumps(params, ensure_ascii=False))


@router.post("/{action_id}/retry", response_model=schemas.PendingActionOut)
def retry_action(action_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Повторить провалившуюся (или зависшую) задачу с начала бэкоффа."""
    action = crud.get_pending_action(db, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    action.status = "pending"
    action.result = None
    action.attempts = 0
    action.next_check_at = None
    # Сбрасываем счётчики бэкоффа/окна ожидания, но сохраняем стадию (app_id и т.п.).
    params = {}
    try:
        params = json.loads(action.params) if action.params else {}
    except (ValueError, TypeError):
        params = {}
    params.pop("ssl_attempts", None)
    params.pop("wait_since", None)
    action.params = json.dumps(params, ensure_ascii=False)
    db.commit()
    db.refresh(action)
    return action


@router.delete("/{action_id}", status_code=204)
def dismiss_action(action_id: int, current_user: CurrentUser, db: Session = Depends(get_db)):
    """Убрать задачу из центра (для завершённых/ошибочных; активную — отменяет)."""
    if not crud.get_pending_action(db, action_id):
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    crud.delete_pending_action(db, action_id)
    return Response(status_code=204)
