"""Корень доверия контрол-плейн → деплоер: `cpk` (Ночь 3, ADR-067; дизайн — 17_IDENTITY §2).

`DEPLOYER_CONTROL_PLANE_KEY` — base64 **публичного** Ed25519-ключа ЛК (per-node пара,
закладывается при провиженинге через cloud-init). Деплоер хранит только публичный
ключ: компрометация сервера клиента НЕ даёт подделывать токены ЛК (в отличие от HMAC).

Формат токена: `base64url(json payload).base64url(signature)`. Payload обязан нести
`typ` (назначение), `exp` (unix, потолок TTL ниже) и `jti` (одноразовость, anti-replay).

**Fail-safe OSS:** без env-ключа все cpk-возможности выключены — обычный self-host
деплоер «глух» к контрол-плейну, никакой лишней поверхности (17_IDENTITY §6).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Жёсткий потолок жизни cpk-токена (сек): ЛК подписывает на ≤60 c, потолок с запасом
# на рассинхрон часов. Всё, что живёт дольше, отклоняем независимо от exp.
TOKEN_TTL_MAX = 300

# Использованные jti (anti-replay). In-memory: токены короткоживущие, потеря стора
# при рестарте даёт окно ≤ TTL — приемлемо (подпись+exp всё ещё проверяются).
_used_jti: dict[str, float] = {}
_jti_lock = threading.Lock()


class ControlPlaneError(Exception):
    """Невалидный/повторно использованный cpk-токен."""


def cpk_enabled() -> bool:
    return bool(os.environ.get("DEPLOYER_CONTROL_PLANE_KEY", "").strip())


def _public_key() -> Ed25519PublicKey:
    raw = base64.b64decode(os.environ["DEPLOYER_CONTROL_PLANE_KEY"].strip())
    return Ed25519PublicKey.from_public_bytes(raw)


def _b64url_decode(part: str) -> bytes:
    pad = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + pad)


def _consume_jti(jti: str, now: float) -> bool:
    """True — jti свежий (помечаем использованным); False — повтор (replay)."""
    with _jti_lock:
        # чистим протухшие записи, чтобы стор не рос бесконечно
        for k in [k for k, exp in _used_jti.items() if exp < now]:
            _used_jti.pop(k, None)
        if jti in _used_jti:
            return False
        _used_jti[jti] = now + TOKEN_TTL_MAX
        return True


def verify_token(token: str, expected_typ: str) -> dict:
    """Проверяет подпись/срок/тип/одноразовость cpk-токена. Возвращает payload.

    Бросает `ControlPlaneError` с человекочитаемой причиной (без секретов).
    """
    if not cpk_enabled():
        raise ControlPlaneError("контрол-плейн не подключён (нет DEPLOYER_CONTROL_PLANE_KEY)")
    try:
        payload_b64, sig_b64 = token.strip().split(".", 1)
        payload_raw = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)
    except (ValueError, TypeError):
        raise ControlPlaneError("неверный формат токена")
    try:
        _public_key().verify(signature, payload_raw)
    except (InvalidSignature, ValueError):
        raise ControlPlaneError("подпись не прошла проверку")
    try:
        payload = json.loads(payload_raw)
    except ValueError:
        raise ControlPlaneError("payload не является JSON")
    if payload.get("typ") != expected_typ:
        raise ControlPlaneError("неверное назначение токена")
    now = time.time()
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < now or exp > now + TOKEN_TTL_MAX:
        raise ControlPlaneError("токен истёк или срок вне допустимого окна")
    jti = payload.get("jti")
    if not jti or not isinstance(jti, str):
        raise ControlPlaneError("токен без jti")
    if not _consume_jti(jti, now):
        raise ControlPlaneError("токен уже использован (одноразовый)")
    return payload
