"""Серверная валидация пользовательского ввода, который попадает в чувствительные
контексты (nginx-конфиги, пути сертификатов, имена контейнеров).

Зачем отдельный модуль: `domain` и `ssl_cert_name` подставляются БЕЗ экранирования
в генерируемые nginx-конфиги (`server_name {domain};`, путь
`/etc/letsencrypt/live/{ssl_cert_name}/...`). Без валидации:
  - инъекция nginx-директив через домен с `;`/`{`/`}`/переводом строки;
  - «заклинивание» reload: битый домен делает `nginx -t` неуспешным — конфиг
    остаётся на диске, и ВСЕ последующие reload (новые приложения, выпуск SSL)
    падают, пока битый файл не удалят вручную;
  - path traversal в имени сертификата (`..`, `/`).

Валидаторы используются на уровне Pydantic-схем (BeforeValidator), поэтому
некорректный ввод отбраковывается до того, как дойдёт до генерации конфигов.
Пустая строка для опциональных полей нормализуется в None («очистить домен»).
"""
import re
from typing import Annotated, Optional

from pydantic import BeforeValidator

# FQDN: метки 1..63 символа из [A-Za-z0-9-], не начинаются/не заканчиваются дефисом,
# разделены точками; суммарно ≤253. Допускаются одиночные метки (localhost).
# Важно: запрещает пробелы, перевод строки, `;{}` и прочие метасимволы nginx —
# именно то, что нужно для безопасной подстановки в server_name.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)

# Имя сертификата = имя каталога в ssl_certs/{live,archive}. Разрешаем только
# безопасный набор и явно отсекаем обход пути.
_CERT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_domain(value: Optional[str]) -> Optional[str]:
    """Проверяет доменное имя. None/пустая строка → None (для опциональных полей)."""
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    if not _DOMAIN_RE.match(v):
        raise ValueError("Некорректный формат домена.")
    return v


def validate_cert_name(value: Optional[str]) -> Optional[str]:
    """Проверяет имя сертификата (= имя каталога). Запрещает обход пути."""
    if value is None:
        return None
    v = value.strip()
    if v == "":
        return None
    if not _CERT_NAME_RE.match(v) or ".." in v or v.startswith("."):
        raise ValueError("Некорректное имя сертификата.")
    return v


# Аннотированные типы для использования в схемах. BeforeValidator нормализует и
# валидирует значение до проверки типа Pydantic.
DomainStr = Annotated[str, BeforeValidator(validate_domain)]
OptionalDomainStr = Annotated[Optional[str], BeforeValidator(validate_domain)]
OptionalCertName = Annotated[Optional[str], BeforeValidator(validate_cert_name)]
