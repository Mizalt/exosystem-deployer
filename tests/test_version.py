"""Версия деплоера + capability-negotiation (Ночь 11, ADR-071).

`GET /api/version` — публичный (без авторизации), отдаёт версию/SHA/издание/
capabilities. ЛК согласует совместимость по capabilities, а не по номеру версии.
"""
from app import version


def test_describe_shape_and_capabilities():
    d = version.describe()
    assert d["version"] == version.get_version()
    assert isinstance(d["capabilities"], list)
    # Ключевые фичи текущей сборки объявлены как capabilities.
    for cap in ("version", "pending_actions", "apex_publish", "sso_redeem", "admin_recover",
                "rate_limit", "pro_license"):
        assert cap in d["capabilities"]
    # Отсортировано и без дублей — детерминированно для UI/тестов.
    assert d["capabilities"] == sorted(set(d["capabilities"]))


def test_supports():
    assert version.supports("pending_actions") is True
    assert version.supports("nonexistent_cap") is False


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("DEPLOYER_VERSION", "9.9.9")
    monkeypatch.setenv("DEPLOYER_GIT_SHA", "abc1234")
    monkeypatch.setenv("DEPLOYER_EDITION", "pro")
    d = version.describe()
    assert d["version"] == "9.9.9"
    assert d["git_sha"] == "abc1234"
    assert d["edition"] == "pro"


def test_git_sha_none_by_default(monkeypatch):
    monkeypatch.delenv("DEPLOYER_GIT_SHA", raising=False)
    assert version.git_sha() is None


def test_version_endpoint_is_public(api_env):
    """Без авторизации — 200 (в отличие от /api/edition, требующего вход)."""
    _, _, client = api_env
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body and "capabilities" in body and "edition" in body
    assert "pending_actions" in body["capabilities"]
