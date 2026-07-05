"""Заголовки безопасности для ответов ПАНЕЛИ.

Важно: не навешиваются на проксируемые приложения (`/api/proxy/...`) — иначе
анти-фрейминг/CSP панели «протекли» бы в чужие приложения (их можно встраивать,
у них своя политика). CSP подобран под текущую статику: внешних скриптов нет
(`script-src 'self'`), но есть инлайн-стили (`style-src 'unsafe-inline'`) и Google
Fonts. После выката на тест-сервер UI стоит проверить в браузере (DevTools → нет
ли CSP-блокировок) — заголовки легко ослабить, изменение обратимо (ADR-027).
"""

# Content-Security-Policy. `frame-ancestors 'none'` = анти-clickjacking (дублирует
# X-Frame-Options для старых браузеров). WebSocket (issue-ssl/redeploy) — same-origin,
# покрывается `connect-src 'self'` (CSP3 трактует 'self' как разрешение ws/wss на тот
# же origin).
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

# HSTS (V-10): после первого визита по HTTPS браузер запрещает downgrade на HTTP
# (анти SSL-strip). Заголовок, полученный по обычному HTTP, браузеры игнорируют по
# спецификации — поэтому его безопасно слать всегда, доступ по IP/HTTP до выпуска
# SSL не ломается. `includeSubDomains`/`preload` НЕ ставим осознанно: приложения
# пользователя живут на субдоменах домена панели и получают собственный SSL/политику
# отдельно — не форсируем HTTPS на них с уровня панели.
HSTS = "max-age=31536000"  # 1 год

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
    "Strict-Transport-Security": HSTS,
}

# Префиксы путей, для которых заголовки панели НЕ применяем.
_SKIP_PREFIXES = ("/api/proxy/",)


def should_apply(path: str) -> bool:
    """True, если к ответу по этому пути нужно добавить заголовки безопасности панели."""
    return not any(path.startswith(p) for p in _SKIP_PREFIXES)


def build_headers(embed_origin: str | None) -> dict:
    """Заголовки безопасности с учётом доверенного embed-origin (ADR-092).

    Без origin — прежний fail-closed набор (`SECURITY_HEADERS`: DENY +
    `frame-ancestors 'none'`). С origin — фрейминг разрешён РОВНО одному origin
    ЛК: `frame-ancestors <origin>`, а `X-Frame-Options` не шлём вовсе (у него
    нет «разрешить конкретный origin» — устаревший ALLOW-FROM не поддерживается,
    а DENY противоречил бы CSP; браузеры с поддержкой frame-ancestors всё равно
    обязаны игнорировать XFO при его наличии).
    """
    if not embed_origin:
        return SECURITY_HEADERS
    headers = dict(SECURITY_HEADERS)
    headers.pop("X-Frame-Options", None)
    headers["Content-Security-Policy"] = CSP.replace(
        "frame-ancestors 'none'", f"frame-ancestors {embed_origin}")
    return headers


def current_headers() -> dict:
    """Заголовки для ЭТОГО ответа: учитывают текущий embed-origin (env/файл)."""
    from app import embed_config

    return build_headers(embed_config.get_embed_origin())
