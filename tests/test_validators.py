"""Тесты серверной валидации домена/имени сертификата (app/validators.py).

Антирегресс на инъекцию nginx-директив и обход пути: домен/cert_name попадают в
генерируемые nginx-конфиги и пути сертификатов без экранирования, поэтому формат
обязан отбраковываться на уровне схемы.
"""
import pytest
from pydantic import ValidationError

from app.validators import validate_domain, validate_cert_name
from app import schemas, panel_config


# --- Прямые юнит-тесты функций ---------------------------------------------- #

@pytest.mark.parametrize("value", [
    "example.com", "sub.example.com", "a.b.c.example.org",
    "localhost", "my-app.example.io", "EXAMPLE.com",
])
def test_validate_domain_accepts_valid(value):
    assert validate_domain(value) == value


@pytest.mark.parametrize("value", [None, "", "   "])
def test_validate_domain_blank_to_none(value):
    # Пустое/None для опционального поля → None («очистить домен»).
    assert validate_domain(value) is None


@pytest.mark.parametrize("value", [
    "example.com; return 200",        # инъекция nginx-директивы
    "foo {\n  proxy_pass http://x;",  # перевод строки + блок
    "a b.com",                         # пробел
    "-leading.com", "trailing-.com",  # дефис по краям метки
    "foo..bar.com",                    # пустая метка
    "пример.рф",                       # не-ASCII (нужен punycode)
    "x" * 254,                          # слишком длинный
])
def test_validate_domain_rejects_bad(value):
    with pytest.raises(ValueError):
        validate_domain(value)


@pytest.mark.parametrize("value", ["mydomain.com", "le-cert_1", "a.b.c"])
def test_validate_cert_name_accepts_valid(value):
    assert validate_cert_name(value) == value


@pytest.mark.parametrize("value", [
    "../etc/passwd", "..", ".hidden", "a/b", "name;rm",
])
def test_validate_cert_name_rejects_traversal(value):
    with pytest.raises(ValueError):
        validate_cert_name(value)


# --- Интеграция со схемами (именно через них приходит API-ввод) -------------- #

def test_application_create_rejects_injection_domain():
    with pytest.raises(ValidationError):
        schemas.ApplicationCreate(name="app", domain="evil.com; }", deployment_id=1)


def test_application_create_accepts_good_domain():
    a = schemas.ApplicationCreate(name="app", domain="good.example.com", deployment_id=1)
    assert a.domain == "good.example.com"


def test_panel_settings_clears_blank_domain():
    s = panel_config.PanelSettings(domain="", ssl_cert_name="")
    assert s.domain is None and s.ssl_cert_name is None


def test_panel_settings_rejects_bad_domain():
    with pytest.raises(ValidationError):
        panel_config.PanelSettings(domain="bad domain.com")


def test_issue_ssl_request_rejects_bad_domain():
    with pytest.raises(ValidationError):
        schemas.IssueSSLRequest(domain="x.com\n}")
