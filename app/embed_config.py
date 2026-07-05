"""Origin, которому разрешено встраивать панель в iframe (ADR-092, «панель внутри ЛК»).

По умолчанию панель ЗАПРЕЩАЕТ фрейминг (`X-Frame-Options: DENY` +
`frame-ancestors 'none'`, ADR-027) — анти-clickjacking. Чтобы ЛК мог показать
панель в iframe на своём домене, ноде нужен ровно ОДИН доверенный origin:

  • env `DEPLOYER_EMBED_ORIGIN` — статическая настройка (self-host руками);
  • `data/embed_origin.json` — origin, который ЛК пушит по каналу управления
    (`POST /api/panel/settings/embed-origin`) перед открытием встроенной панели.

Приоритет: env → файл. **Fail-closed:** origin не задан или невалиден →
встраивание запрещено, как раньше (никакой «звёздочки» и никаких списков —
только один конкретный origin контрол-плейна).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_FILE = Path("data/embed_origin.json")

# Кэш файла по mtime: заголовки строятся на КАЖДЫЙ ответ панели — не парсим JSON
# каждый раз. env читается всегда свежим (дёшево, удобно тестам).
_cache: dict = {"mtime": None, "value": None}


def normalize_origin(value) -> str | None:
    """Нормализует origin (`https://lk.example[:port]`) или бросает ValueError.

    Пусто/None → None (означает «очистить»). Допускается только scheme://host[:port]
    без пути/query/fragment/юзеринфо — это ЗНАЧЕНИЕ директивы CSP, мусор в ней
    равносилен отключению защиты.
    """
    if value is None:
        return None
    s = str(value).strip().rstrip("/")
    if not s:
        return None
    parts = urlsplit(s)
    if (parts.scheme not in ("http", "https") or not parts.netloc
            or parts.path or parts.query or parts.fragment or "@" in parts.netloc):
        raise ValueError(
            "Origin должен иметь вид https://host[:port] — без пути и параметров.")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _file_origin() -> str | None:
    try:
        mtime = CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return None
    if _cache["mtime"] != mtime:
        value = None
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            value = normalize_origin(data.get("origin") if isinstance(data, dict) else None)
        except (OSError, ValueError, TypeError):
            value = None  # битый файл/мусор → fail-closed (встраивание запрещено)
        _cache["mtime"], _cache["value"] = mtime, value
    return _cache["value"]


def get_embed_origin() -> str | None:
    """Действующий доверенный origin для frame-ancestors (env → файл) или None."""
    env = os.environ.get("DEPLOYER_EMBED_ORIGIN", "").strip()
    if env:
        try:
            return normalize_origin(env)
        except ValueError:
            return None  # кривой env → fail-closed, а не «пропустить всё»
    return _file_origin()


def save_origin(origin: str | None) -> None:
    """Сохраняет origin, пришедший от контрол-плейна (None = очистить)."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"origin": origin}, ensure_ascii=False),
                           encoding="utf-8")
    _cache["mtime"], _cache["value"] = None, None  # инвалидация кэша
