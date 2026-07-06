# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/routers/panel.py ---

import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app import embed_config, panel_config
from app.services import nginx_manager
# --- ИМПОРТЫ ДЛЯ АУТЕНТИФИКАЦИИ ---
from app import security, models
from typing import Annotated
CurrentUser = Annotated[models.User, Depends(security.get_current_user)]

router = APIRouter(prefix="/api/panel/settings", tags=["Panel Settings"])

# Отдельный публичный (в пределах панели) роут ИИ-помощника панели (ADR-103).
# Абстрагирует ИСТОЧНИК ИИ для фронта панели: нода САМА ключа/провайдера не
# касается и ИИ не проксирует — лишь отвечает, доступен ли ИИ и откуда (сегодня —
# только «cloud», когда панель открыта внутри ЛК). Держит дверь для будущего
# mode="self_host_key" без переписывания фронта.
ai_router = APIRouter(prefix="/api/panel", tags=["Panel AI"])

# Файлы docker-compose, которые нужно удалить при закрытии первичного доступа.
_OVERRIDE_FILES = [
    Path("/app/docker-compose.override.yml"),    # provisioned servers (cloud-init)
    Path("/app/docker-compose.firstrun.yml"),      # manual install (install.sh)
]


@router.get("", response_model=panel_config.PanelSettings)
def get_panel_settings(current_user: CurrentUser):
    return panel_config.load_settings()


@router.post("", response_model=panel_config.PanelSettings)
def update_panel_settings(
        settings: panel_config.PanelSettings,
        background_tasks: BackgroundTasks,
        current_user: CurrentUser
):
    panel_config.save_settings(settings)
    nginx_manager.update_panel_nginx_config(domain=settings.domain, ssl_cert_name=settings.ssl_cert_name)
    background_tasks.add_task(nginx_manager.reload_nginx)
    # Если переводим панель на HTTPS (и домен, и SSL-сертификат заданы) — публикация
    # порта 7999 на уровне compose больше не нужна (снимаем override, чтобы он не
    # переопубликовался). Контейнер НЕ пересоздаём (ADR-054); внешний доступ к :7999
    # закрывается сетевым правилом (SG в cloud / файрвол в self-host).
    if settings.domain and settings.ssl_cert_name:
        threading.Thread(target=_close_port_background, daemon=True).start()
    return settings


def _close_port_background() -> None:
    """Фоновая задача: удаляет docker-compose override-файлы, публикующие порт 7999.

    🔴 НЕ пересоздаёт контейнер деплоера (ADR-054). Прежний docker-py recreate из
    скопированных attrs оказался хрупким и ронял доступ к панели (502: nginx-proxy не
    достукивался до пересозданного app-контейнера; провижиненную ноду без сохранённого
    SSH-ключа было не починить). Внешний доступ к :7999 закрывается на СЕТЕВОМ уровне:
      • cloud — ЛК удаляет ingress-правило 7999 в Selectel security-group (Б4.5/ADR-054);
      • self-host — host-скрипт `close-initial-access.sh` (docker compose recreate ВНЕ
        контейнера, безопасно).
    Удаление override-файла здесь лишь не даёт повторно опубликовать порт при будущем
    `docker compose up` — сам контейнер не трогаем.
    """
    time.sleep(2)  # Даём HTTP-ответу уйти
    for path in _OVERRIDE_FILES:
        try:
            if path.exists():
                path.unlink()
                print(f"INFO: close-initial-access: удалён {path} "
                      f"(порт 7999 не будет переопубликован при compose up)")
        except Exception as e:
            print(f"WARN: close-initial-access: не удалось удалить {path}: {e}")


class EmbedOriginIn(BaseModel):
    origin: str | None = None


@router.post("/embed-origin")
def set_embed_origin(data: EmbedOriginIn, current_user: CurrentUser):
    """Разрешает встраивание панели в iframe РОВНО одному origin (ADR-092).

    Зовёт контрол-плейн по каналу управления перед открытием «панели внутри ЛК»:
    нода начинает отдавать `CSP frame-ancestors <origin>` вместо полного запрета.
    `origin: null` — вернуть fail-closed запрет. env `DEPLOYER_EMBED_ORIGIN`
    (если задан) имеет приоритет над пушем. Идемпотентно.
    """
    try:
        origin = embed_config.normalize_origin(data.origin)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    embed_config.save_origin(origin)
    return {"origin": origin, "effective_origin": embed_config.get_embed_origin()}


@router.post("/close-initial-access")
def close_initial_access(current_user: CurrentUser):
    """Снимает публикацию порта 7999 на уровне docker-compose (override-файлы).

    🔴 НЕ пересоздаёт контейнер деплоера (ADR-054 — это ломало доступ к панели).
    Реальное закрытие внешнего доступа к :7999 делается на сетевом уровне (SG в
    cloud силами ЛК / host-скрипт в self-host). Здесь — только удаление override,
    чтобы порт не переопубликовался при будущем `docker compose up`. Идемпотентно.
    """
    any_override = any(p.exists() for p in _OVERRIDE_FILES)
    if not any_override:
        return {"message": "Override-файлы порта 7999 отсутствуют — публикация уже снята "
                           "на уровне compose (внешний доступ закрывается сетевым правилом).",
                "action": "none"}

    threading.Thread(target=_close_port_background, daemon=True).start()
    return {"message": "Снимаю публикацию порта 7999 на уровне compose (override удаляется). "
                       "Внешний доступ закрывается сетевым правилом (SG/файрвол).",
            "action": "scheduled"}


@ai_router.get("/ai-availability")
def ai_availability(current_user: CurrentUser):
    """Доступен ли ИИ-помощник панели и ОТКУДА (ADR-103) — гейт показа виджета.

    Нода ключа/провайдера НЕ касается и ИИ не проксирует. Сегодня ИИ доступен
    ТОЛЬКО когда панель открыта ВНУТРИ ЛК (embedded): у ноды есть pushed
    `embed_origin` контрол-плейна (ADR-092) — это и есть origin ЛК, куда браузер
    (уже с ЛК-сессией) шлёт вопрос напрямую. Standalone-панель → `available:false`
    (браузер не имеет ЛК-сессии, cross-origin к ЛК не пройдёт) → виджета нет вовсе
    (fail-safe, как cpk_enabled=false прячет cpk-поверхность).

    `mode` абстрагирует источник ИИ: сегодня только "cloud"; поле оставлено, чтобы
    в будущем добавить "self_host_key" без переписывания фронта. `ai_origin` —
    origin ЛК (родитель iframe), фронт шлёт туда вопрос (postMessage к родителю).
    Финальный энфорс гейтов (ИИ включён/консент/лимит) — на ЛК; здесь лишь показ.
    """
    embed_origin = embed_config.get_embed_origin()
    if embed_origin:
        return {"available": True, "mode": "cloud", "ai_origin": embed_origin}
    return {"available": False, "mode": None, "ai_origin": None}