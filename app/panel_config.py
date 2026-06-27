# --- НОВЫЙ ФАЙЛ: app/panel_config.py ---
import json
from pathlib import Path
from pydantic import BaseModel

from app.validators import OptionalDomainStr, OptionalCertName

CONFIG_FILE = Path("data/panel_settings.json")

class PanelSettings(BaseModel):
    # Домен/сертификат панели идут в server_name и пути nginx-конфига — валидируем
    # (см. app/validators.py). Пустая строка из UI («очистить домен») → None.
    domain: OptionalDomainStr = None
    ssl_cert_name: OptionalCertName = None

def load_settings() -> PanelSettings:
    """Загружает настройки панели из JSON-файла."""
    if not CONFIG_FILE.exists():
        return PanelSettings()
    try:
        with CONFIG_FILE.open("r") as f:
            data = json.load(f)
            return PanelSettings(**data)
    except (json.JSONDecodeError, TypeError):
        return PanelSettings()

def save_settings(settings: PanelSettings):
    """Сохраняет настройки панели в JSON-файл."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump(settings.model_dump(), f, indent=4)