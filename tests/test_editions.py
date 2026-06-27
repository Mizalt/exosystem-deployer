"""Тесты слоя редакций (open-core): резолвинг издания и гейтинг фич.

См. app/editions.py, docs/10_EDITIONS.md, ADR-019.
"""

import pytest

from app import editions


@pytest.fixture(autouse=True)
def _clear_edition_env(monkeypatch):
    """Каждый тест стартует без DEPLOYER_EDITION (дефолт = oss)."""
    monkeypatch.delenv("DEPLOYER_EDITION", raising=False)


def test_default_edition_is_oss():
    assert editions.get_edition() == editions.OSS


@pytest.mark.parametrize("value,expected", [
    ("oss", editions.OSS),
    ("pro", editions.PRO),
    ("cloud", editions.CLOUD),
    ("PRO", editions.PRO),          # регистронезависимо
    ("  cloud  ", editions.CLOUD),  # обрезаем пробелы
    ("", editions.OSS),             # пусто → oss
    ("nonsense", editions.OSS),     # мусор → oss (fail-safe)
])
def test_edition_resolution(monkeypatch, value, expected):
    monkeypatch.setenv("DEPLOYER_EDITION", value)
    assert editions.get_edition() == expected


def test_oss_has_no_paid_features(monkeypatch):
    monkeypatch.setenv("DEPLOYER_EDITION", "oss")
    assert editions.is_feature_enabled("roles") is False
    assert editions.is_feature_enabled("multi_tenancy") is False
    # Неизвестная (ядровая) фича доступна всем — fail-open для ядра.
    assert editions.is_feature_enabled("some_core_feature") is True


def test_pro_has_pro_but_not_cloud(monkeypatch):
    monkeypatch.setenv("DEPLOYER_EDITION", "pro")
    assert editions.is_feature_enabled("roles") is True
    assert editions.is_feature_enabled("protected_mode") is True
    assert editions.is_feature_enabled("multi_tenancy") is False
    assert editions.is_feature_enabled("billing") is False


def test_cloud_includes_everything(monkeypatch):
    monkeypatch.setenv("DEPLOYER_EDITION", "cloud")
    assert editions.is_feature_enabled("roles") is True          # унаследовано от pro
    assert editions.is_feature_enabled("multi_tenancy") is True
    assert editions.is_feature_enabled("billing") is True


def test_hierarchy_includes():
    assert editions.edition_includes(editions.OSS) == frozenset({editions.OSS})
    assert editions.edition_includes(editions.PRO) == frozenset({editions.OSS, editions.PRO})
    assert editions.edition_includes(editions.CLOUD) == frozenset(
        {editions.OSS, editions.PRO, editions.CLOUD}
    )


def test_describe_shape(monkeypatch):
    monkeypatch.setenv("DEPLOYER_EDITION", "pro")
    d = editions.describe()
    assert d["edition"] == "pro"
    assert "oss" in d["tiers"] and "pro" in d["tiers"]
    assert d["features"]["roles"] is True
    assert d["features"]["billing"] is False


def test_explicit_edition_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("DEPLOYER_EDITION", "oss")
    # Явный аргумент издания важнее текущего окружения.
    assert editions.is_feature_enabled("roles", edition="pro") is True
    assert editions.is_feature_enabled("roles", edition="oss") is False
