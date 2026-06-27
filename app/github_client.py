"""Подключение GitHub-аккаунта к деплоеру (core/OSS, ADR-033).

Лёгкий клиент GitHub REST API для **деплоерской** (per-server) интеграции:
пользователь привязывает свой PAT, чтобы видеть и импортировать приватные
репозитории в Библиотеку (раньше — только публичные по URL, ADR-016). Токен
хранится зашифрованным (`app/secret_box.py`) в `GithubConnection`.

Отдельно от `app/cloud/providers/github.py` (`GitHubSource`) — та версия
живёт в control-plane (`app/cloud/`, вырезается из публичного OSS-среза,
`AGENTS.md`) и обслуживает мульти-юзерный BYOA-сценарий. Здесь — однопользовательский
сценарий самого деплоера; код похож, но дублирование намеренное: ядро (этот
модуль) не должно зависеть от `app/cloud/`.
"""
from __future__ import annotations

import httpx

API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


async def validate_token(token: str) -> str:
    """Возвращает логин владельца токена; бросает `ValueError`, если токен невалиден."""
    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.get(f"{API}/user", headers=_headers(token))
    if r.status_code != 200:
        raise ValueError(f"GitHub-токен невалиден: HTTP {r.status_code}")
    return r.json()["login"]


async def list_repos(token: str) -> list[dict]:
    """Список репозиториев владельца токена (включая приватные): [{full_name, private}]."""
    async with httpx.AsyncClient(timeout=20.0) as http:
        r = await http.get(f"{API}/user/repos", headers=_headers(token),
                            params={"per_page": 100, "visibility": "all",
                                    "affiliation": "owner", "sort": "updated"})
    r.raise_for_status()
    return [{"full_name": x["full_name"], "private": bool(x.get("private"))}
            for x in r.json()]
