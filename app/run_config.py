"""Хелперы расширенного режима сборки/рантайма (Идея 2а, ADR-021).

Парсинг env-переменных и нормализация внутреннего порта. Вынесено отдельным
модулем без зависимостей от `app`, чтобы было легко тестировать и переиспользовать
(main.py парсит ввод API, оркестратор/прокси читают эффективный порт).
"""
import json

# Порт по умолчанию внутри контейнера приложения. Историческое значение (раньше
# было захардкожено в proxy/health-gate/Dockerfile). internal_port=0 означает
# «воркер без сетевого порта» (бот, очередь) — health-gate пропускается.
DEFAULT_INTERNAL_PORT = 80


def parse_env_input(value) -> dict:
    """Нормализует ввод env-переменных в dict[str, str].

    Принимает dict (из JSON API) или строку 'KEY=VALUE' по строкам (из textarea
    UI). Пустые строки и '#'-комментарии игнорируются; пробелы по краям срезаются.
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k).strip(): str(v) for k, v in value.items() if str(k).strip()}
    result: dict[str, str] = {}
    if isinstance(value, str):
        for raw in value.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if key:
                result[key] = val.strip()
    return result


def env_to_json(value) -> str | None:
    """Сериализует ввод env в JSON-строку для хранения (или None, если пусто)."""
    parsed = parse_env_input(value)
    return json.dumps(parsed, ensure_ascii=False) if parsed else None


def env_from_json(value) -> dict:
    """Разбирает хранимую JSON-строку env обратно в dict (терпимо к мусору)."""
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def effective_port(internal_port) -> int:
    """None → дефолт 80 (старые деплои без колонки после ADD COLUMN); 0 сохраняется
    (worker без сетевого порта)."""
    return DEFAULT_INTERNAL_PORT if internal_port is None else int(internal_port)
