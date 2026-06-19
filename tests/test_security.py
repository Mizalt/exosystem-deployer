"""Тесты безопасности: хеширование паролей, JWT, источник секретного ключа."""
from datetime import timedelta

from jose import jwt

from app import security


def test_password_hash_is_not_plaintext_and_verifies():
    h = security.get_password_hash("super-secret")
    assert h != "super-secret"
    assert security.verify_password("super-secret", h) is True
    assert security.verify_password("wrong", h) is False


def test_jwt_roundtrip():
    token = security.create_access_token({"sub": "admin"}, timedelta(minutes=5))
    payload = jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])
    assert payload["sub"] == "admin"
    assert "exp" in payload


def test_secret_key_prefers_env(monkeypatch):
    monkeypatch.setenv("DEPLOYER_SECRET_KEY", "env-secret-123")
    assert security._load_or_create_secret_key() == "env-secret-123"


def test_no_hardcoded_legacy_key():
    # Старый зашитый ключ не должен возвращаться (была дыра безопасности).
    assert security.SECRET_KEY != "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
