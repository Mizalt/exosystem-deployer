# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/routers/panel.py ---

from fastapi import APIRouter, Depends, BackgroundTasks
from app import panel_config
from app.services import nginx_manager
# --- ИМПОРТЫ ДЛЯ АУТЕНТИФИКАЦИИ ---
from app import security, models
from typing import Annotated
CurrentUser = Annotated[models.User, Depends(security.get_current_user)]

router = APIRouter(prefix="/api/panel/settings", tags=["Panel Settings"])

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
    return settings