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
