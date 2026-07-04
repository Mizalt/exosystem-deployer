# --- ПОЛНЫЙ И ИСПРАВЛЕННЫЙ ФАЙЛ: app/routers/ssl.py ---

import re
import shutil
import socket
import asyncio
import uuid
from typing import List, Annotated

import httpx
from fastapi import APIRouter, HTTPException, Query, Form, File, UploadFile, Depends, WebSocket
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import SSL_DIR
from pydantic import BaseModel
from app.database import get_db
from app.schemas import IssueSSLRequest
from app.services import nginx_manager, ssl_renewal
from app.services.ws_manager import manager
from app.services.ssl_service import perform_ssl_issuance
# --- ИМПОРТЫ ДЛЯ АУТЕНТИФИКАЦИИ ---
from app import security, models
CurrentUser = Annotated[models.User, Depends(security.get_current_user)]

router = APIRouter(prefix="/api/ssl", tags=["ssl"])
# verify=True: обращаемся к внешнему HTTPS-API (api.ipify.org) — проверку TLS не
# отключаем (раньше было verify=False без причины).
http_client = httpx.AsyncClient()

class SSLCertificateInfo(BaseModel):
    name: str
    subject: str
    not_after: datetime
    # Ночь 16 (ADR-085): сколько дней осталось (UI подсвечивает ≤30/≤14) и
    # продлеваем ли сами (LE — да; загруженный вручную — только предупреждаем).
    days_left: int | None = None
    auto_renew: bool = True
class DnsCheckResponse(BaseModel): domain: str; server_ip: str | None; domain_ip: str | None; domain_ips: list[str] = []; matches: bool; warning: str | None = None; error: str | None


def evaluate_dns_match(server_ip: str | None, domain_ips: list[str]) -> tuple[bool, str | None]:
    """Чистая логика DNS-чека (тестируется без сети).

    Сервер указан, если его IP ЕСТЬ среди A-записей домена — детерминированно, не зависит
    от порядка резолвера (иначе при нескольких записях чек «мигает»). Если кроме нужной есть
    лишние записи — предупреждаем: они ломают выпуск SSL (LE может проверить не тот сервер).
    """
    matches = bool(server_ip) and server_ip in domain_ips
    warning = None
    if matches and len(domain_ips) > 1:
        stale = [ip for ip in domain_ips if ip != server_ip]
        warning = ("Домен указывает сюда, но есть лишние A-записи: " + ", ".join(stale)
                   + ". Удали их (оставь только " + server_ip
                   + ") — иначе Let's Encrypt может проверить не тот сервер и выпуск SSL упадёт.")
    return matches, warning

# --- ЗАЩИЩАЕМ ЭНДПОИНТЫ ---
@router.post("/issue")
async def issue_ssl_certificate(request_data: IssueSSLRequest, current_user: CurrentUser):
    domain = request_data.domain
    if not re.match(r"^[a-zA-Z0-9.-]+$", domain): raise HTTPException(status_code=400, detail="Некорректный формат домена.")
    task_id = str(uuid.uuid4())
    asyncio.create_task(perform_ssl_issuance(task_id, domain))
    return {"message": "Процесс выпуска сертификата запущен.", "task_id": task_id}

# Websocket не защищаем, т.к. доступ к нему идет по уникальному task_id
@router.websocket("/ws/issue-ssl/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    await manager.connect(websocket, task_id)
    try:
        while True: await websocket.receive_text()
    except Exception:
        manager.disconnect(task_id)

@router.get("/check-dns", response_model=DnsCheckResponse)
# ИСПРАВЛЕНИЕ: Перемещаем current_user перед domain=Query(...)
async def check_domain_dns(current_user: CurrentUser, domain: str = Query(...)):
    if not re.match(r"^[a-zA-Z0-9.-]+$", domain):
        raise HTTPException(status_code=400, detail="Некорректный формат домена.")
    server_ip=None; domain_ip=None; domain_ips=[]; matches=False; warning=None; error_message=None
    try:
        response = await http_client.get("https://api.ipify.org"); response.raise_for_status(); server_ip = response.text.strip()
        loop = asyncio.get_running_loop()
        # ВАЖНО: собираем ВСЕ A-записи (не addr_info[0]) — иначе при нескольких записях
        # резолвер отдаёт случайную и чек «мигает» ✅/❌. Совпадение детерминировано:
        # сервер указан, если его IP ЕСТЬ среди A-записей.
        addr_info = await loop.getaddrinfo(domain, None, family=socket.AF_INET)
        domain_ips = sorted({info[4][0] for info in addr_info})
        domain_ip = ", ".join(domain_ips) if domain_ips else None
        matches, warning = evaluate_dns_match(server_ip, domain_ips)
    except httpx.RequestError as e: error_message = f"Не удалось получить публичный IP сервера. Ошибка сети: {e}"
    except socket.gaierror: error_message = f"Не удалось найти A-запись для домена '{domain}'."
    except Exception as e: error_message = f"Произошла ошибка при проверке DNS: {str(e)}"
    return {"domain": domain, "server_ip": server_ip, "domain_ip": domain_ip, "domain_ips": domain_ips,
            "matches": matches, "warning": warning, "error": error_message}

@router.get("/certificates", response_model=List[SSLCertificateInfo])
async def list_ssl_certificates(current_user: CurrentUser):
    certs_archive_dir = SSL_DIR / "archive"
    if not certs_archive_dir.exists(): return []
    certs = []
    for item in certs_archive_dir.iterdir():
        if item.is_dir():
            try:
                chain_files = sorted(list(item.glob("fullchain*.pem")), key=lambda p: int(re.search(r'(\d+)', p.name).group(1)))
                if not chain_files: continue
                cert_path = chain_files[-1]; cert_name = item.name
                cert = x509.load_pem_x509_certificate(cert_path.read_bytes(), default_backend())
                subject_cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
                certs.append(SSLCertificateInfo(
                    name=cert_name, subject=subject_cn, not_after=cert.not_valid_after_utc,
                    days_left=int(ssl_renewal.days_left(cert.not_valid_after_utc)),
                    auto_renew=ssl_renewal.is_letsencrypt(cert_name)))
            except Exception as e: print(f"ERROR: Failed to parse certificate in archive {item.name}: {e}")
    return sorted(certs, key=lambda c: c.name)


@router.get("/expiring")
async def list_expiring_certificates(current_user: CurrentUser,
                                     db: Session = Depends(get_db)):
    """Сертификаты В РАБОТЕ (приложения+панель), истекающие ≤30 дней (Ночь 16).

    Зеркалится в ЛК (capability `ssl_renewal` → `Deployer.ssl_alerts`): status
    `alert` (≤14 дн.) триггерит там письмо владельцу. Пустой список — всё в порядке.
    """
    return {"items": ssl_renewal.expiring_report(db),
            "renew_before_days": ssl_renewal.RENEW_BEFORE_DAYS,
            "alert_days": ssl_renewal.ALERT_DAYS}

@router.post("/certificates")
# ИСПРАВЛЕНИЕ: Перемещаем current_user перед параметрами Form/File
async def upload_ssl_certificate(current_user: CurrentUser, name: Annotated[str, Form()], cert_file: Annotated[UploadFile, File()], key_file: Annotated[UploadFile, File()]):
    if not re.match(r"^[a-zA-Z0-9._-]+$", name): raise HTTPException(400, "Некорректное имя.")
    cert_dir = SSL_DIR / name
    if cert_dir.exists(): raise HTTPException(409, f"Сертификат '{name}' уже существует.")
    try:
        cert_dir.mkdir(parents=True)
        (cert_dir / "fullchain.pem").write_bytes(await cert_file.read())
        (cert_dir / "privkey.pem").write_bytes(await key_file.read())
        return {"message": f"Сертификат '{name}' успешно загружен."}
    except Exception as e:
        if cert_dir.exists(): shutil.rmtree(cert_dir)
        raise HTTPException(500, f"Ошибка сохранения: {e}")

@router.delete("/certificates/{cert_name}")
async def delete_ssl_certificate(cert_name: str, current_user: CurrentUser):
    # Здесь нет конфликта, так как cert_name (Path parameter) не является дефолтным
    if not re.match(r"^[a-zA-Z0-9._-]+$", cert_name): raise HTTPException(400, "Некорректное имя.")
    cert_dir_live = SSL_DIR / "live" / cert_name
    cert_dir_archive = SSL_DIR / "archive" / cert_name
    cert_dir_renewal = SSL_DIR / "renewal" / f"{cert_name}.conf"
    if not cert_dir_live.is_dir(): raise HTTPException(404, f"Сертификат '{cert_name}' не найден.")
    try:
        if cert_dir_live.exists(): shutil.rmtree(cert_dir_live)
        if cert_dir_archive.exists(): shutil.rmtree(cert_dir_archive)
        if cert_dir_renewal.exists(): cert_dir_renewal.unlink()
        nginx_manager.reload_nginx()
        return {"message": f"Сертификат '{cert_name}' удален."}
    except Exception as e:
        raise HTTPException(500, f"Ошибка удаления: {e}")