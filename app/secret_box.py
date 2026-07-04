"""SecretBox: шифрование чужих кредов at-rest (envelope, Fernet/AES).

Контракт — `docs/13_SECURITY_MODEL.md` §4: `seal(plaintext) -> ciphertext`,
`open(ciphertext) -> plaintext`. Мастер-ключ — `DEPLOYER_MASTER_KEY` (env,
прод) или `data/master.key` (генерируется один раз на dev, как `secret.key`
в `app/security.py`). Версия ключа в префиксе шифротекста — задел на ротацию
без миграции уже зашифрованных данных.

Живёт в core (не в `app/cloud/`), т.к. нужен не только control-plane (BYOA-
креды), но и самому деплоеру — например, для хранения GitHub-токена при
подключении приватных репозиториев (`app/github_client.py`).
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_KEY_ENV = "DEPLOYER_MASTER_KEY"
_KEY_REQUIRED_ENV = "DEPLOYER_MASTER_KEY_REQUIRED"
_KEY_FILE = Path("data/master.key")
_VERSION = "v1"


def _load_or_create_master_key() -> bytes:
    env_key = os.environ.get(_KEY_ENV)
    if env_key:
        return env_key.strip().encode("utf-8")

    # V-04 (ADR-074): в проде мастер-ключ обязан приходить из окружения/секрет-
    # менеджера. Файл data/master.key лежит В ТОМ ЖЕ томе, что и БД с шифротекстами —
    # один утёкший бэкап раскрыл бы всё. Fail-fast понятнее, чем тихая деградация.
    if os.environ.get(_KEY_REQUIRED_ENV, "false").lower() == "true":
        raise RuntimeError(
            f"{_KEY_ENV} не задан, а {_KEY_REQUIRED_ENV}=true (прод-режим): "
            "мастер-ключ SecretBox должен приходить из секрет-менеджера/окружения, "
            "а не файлом рядом с БД. Задайте ключ и перезапустите.")

    if _KEY_FILE.exists():
        return _KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")

    new_key = Fernet.generate_key()
    try:
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_bytes(new_key)
        print(f"INFO: Сгенерирован новый мастер-ключ {_KEY_FILE}. "
              f"Для прода задайте {_KEY_ENV} в окружении.")
    except Exception as e:
        print(f"WARN: Не удалось сохранить {_KEY_FILE} ({e}); ключ только в "
              f"памяти — расшифровка не переживёт рестарт.")
    return new_key


class SecretBox:
    """Шифрует/расшифровывает секреты at-rest одним мастер-ключом."""

    def __init__(self, master_key: bytes | None = None):
        self._fernet = Fernet(master_key or _load_or_create_master_key())

    def seal(self, plaintext: str) -> str:
        if plaintext is None:
            raise ValueError("plaintext не может быть None")
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return f"{_VERSION}:{token.decode('utf-8')}"

    def open(self, ciphertext: str) -> str:
        if not ciphertext or ":" not in ciphertext:
            raise ValueError("неизвестный формат шифротекста SecretBox")
        version, token = ciphertext.split(":", 1)
        if version != _VERSION:
            raise ValueError(f"неизвестная версия ключа шифротекста: {version}")
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as e:
            raise ValueError(
                "не удалось расшифровать секрет (неверный мастер-ключ или "
                "повреждённые данные)"
            ) from e

    def mask(self, plaintext: str, visible: int = 4) -> str:
        """Для UI/логов: показывает максимум последние N символов, остальное — маска."""
        if not plaintext:
            return ""
        tail = plaintext[-visible:] if visible > 0 else ""
        return "•" * max(len(plaintext) - len(tail), 4) + tail


_default_box: SecretBox | None = None


def get_secret_box() -> SecretBox:
    """Синглтон по умолчанию (мастер-ключ из env/`data/master.key`)."""
    global _default_box
    if _default_box is None:
        _default_box = SecretBox()
    return _default_box
