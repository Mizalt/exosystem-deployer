"""cpk-эндпоинты деплоера (Ночь 3, ADR-067): /api/sso/redeem + /api/admin/recover.

Токены подписываются НАСТОЯЩИМ генератором ЛК (`app.cloud.services.trust`) — тесты
заодно проверяют совместимость формата подписи ЛК ↔ проверки деплоера.
Инварианты: fail-safe без env-ключа (404), подпись/срок/тип/одноразовость (401),
recover сбрасывает пароль независимо от старого.
"""
import base64
import json
import time

import pytest

from app import models, security
from app.cloud.services import trust
from app.services import control_plane


@pytest.fixture(autouse=True)
def _clean_jti_store():
    """Anti-replay-стор процессный — чистим между тестами."""
    control_plane._used_jti.clear()
    yield
    control_plane._used_jti.clear()


@pytest.fixture
def keypair(monkeypatch):
    priv, pub = trust.generate_keypair()
    monkeypatch.setenv("DEPLOYER_CONTROL_PLANE_KEY", pub)
    return priv, pub


@pytest.fixture
def admin_user(api_env):
    app, Session, client = api_env
    s = Session()
    user = models.User(username="admin",
                       hashed_password=security.get_password_hash("old-pass"))
    s.add(user)
    s.commit()
    s.close()
    return client, Session


def test_endpoints_disabled_without_cpk(api_env, monkeypatch):
    """OSS fail-safe: без DEPLOYER_CONTROL_PLANE_KEY эндпоинты «не существуют» (404)."""
    _, _, client = api_env
    monkeypatch.delenv("DEPLOYER_CONTROL_PLANE_KEY", raising=False)
    assert client.post("/api/sso/redeem", json={"token": "x.y"}).status_code == 404
    assert client.post("/api/admin/recover", json={"token": "x.y"}).status_code == 404


def test_sso_redeem_returns_working_jwt(admin_user, keypair):
    client, _ = admin_user
    priv, _ = keypair
    token = trust.sign_token(priv, typ="sso", sub="admin", aud=1)
    r = client.post("/api/sso/redeem", json={"token": token})
    assert r.status_code == 200
    jwt = r.json()["access_token"]
    # Выданный JWT — обычная панельная сессия (пускает в защищённый эндпоинт).
    me = client.get("/api/auth/users/me", headers={"Authorization": f"Bearer {jwt}"})
    assert me.status_code == 200 and me.json()["username"] == "admin"


def test_sso_redeem_is_one_time(admin_user, keypair):
    client, _ = admin_user
    priv, _ = keypair
    token = trust.sign_token(priv, typ="sso")
    assert client.post("/api/sso/redeem", json={"token": token}).status_code == 200
    r2 = client.post("/api/sso/redeem", json={"token": token})  # replay
    assert r2.status_code == 401
    assert "использован" in r2.json()["detail"]


def test_sso_redeem_rejects_wrong_typ_bad_sig_expired(admin_user, keypair):
    client, _ = admin_user
    priv, pub = keypair
    # recover-токен не годится для SSO
    assert client.post("/api/sso/redeem",
                       json={"token": trust.sign_token(priv, typ="recover")}).status_code == 401
    # подпись ЧУЖИМ ключом
    other_priv, _ = trust.generate_keypair()
    assert client.post("/api/sso/redeem",
                       json={"token": trust.sign_token(other_priv, typ="sso")}).status_code == 401
    # истёкший
    assert client.post("/api/sso/redeem",
                       json={"token": trust.sign_token(priv, typ="sso", ttl=-10)}).status_code == 401
    # мусорный формат
    assert client.post("/api/sso/redeem", json={"token": "not-a-token"}).status_code == 401


def test_sso_redeem_rejects_over_ttl_ceiling(admin_user, keypair):
    """exp дальше жёсткого потолка (TOKEN_TTL_MAX) отклоняется — ЛК не может
    выписать «вечный» токен даже своим ключом."""
    client, _ = admin_user
    priv, _ = keypair
    payload = {"typ": "sso", "sub": "admin", "jti": "j1",
               "exp": int(time.time()) + control_plane.TOKEN_TTL_MAX + 3600}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv))
    b64u = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    token = f"{b64u(raw)}.{b64u(key.sign(raw))}"
    assert client.post("/api/sso/redeem", json={"token": token}).status_code == 401


def test_admin_recover_resets_password(admin_user, keypair):
    """recover перевыпускает пароль НЕЗАВИСИМО от старого: старый перестаёт работать,
    новый (из ответа) — работает."""
    client, _ = admin_user
    priv, _ = keypair
    r = client.post("/api/admin/recover",
                    json={"token": trust.sign_token(priv, typ="recover", sub="admin")})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "admin" and body["password"]
    # старый пароль мёртв
    old = client.post("/api/auth/token", data={"username": "admin", "password": "old-pass"})
    assert old.status_code == 401
    # новый пускает
    new = client.post("/api/auth/token", data={"username": "admin", "password": body["password"]})
    assert new.status_code == 200 and new.json()["access_token"]


def test_admin_recover_unknown_user_401(admin_user, keypair):
    client, _ = admin_user
    priv, _ = keypair
    r = client.post("/api/admin/recover",
                    json={"token": trust.sign_token(priv, typ="recover", sub="ghost")})
    assert r.status_code == 401


def test_recover_invalidates_existing_tokens(admin_user, keypair):
    """V-05: admin-recover бампает версию токенов → ранее выданный панельный JWT
    (через SSO) перестаёт пускать. Утёкший токен не переживает восстановление доступа."""
    client, _ = admin_user
    priv, _ = keypair
    jwt_tok = client.post(
        "/api/sso/redeem",
        json={"token": trust.sign_token(priv, typ="sso", sub="admin")}
    ).json()["access_token"]
    ok = client.get("/api/auth/users/me", headers={"Authorization": f"Bearer {jwt_tok}"})
    assert ok.status_code == 200
    # recover
    assert client.post(
        "/api/admin/recover",
        json={"token": trust.sign_token(priv, typ="recover", sub="admin")}
    ).status_code == 200
    # старый токен больше не действителен
    dead = client.get("/api/auth/users/me", headers={"Authorization": f"Bearer {jwt_tok}"})
    assert dead.status_code == 401
