"""Тесты подключения GitHub-аккаунта к деплоеру (ADR-033).

Использует фикстуры `api_env`/`auth_client` из `test_api.py` (in-memory БД +
override auth). GitHub API мокается через `monkeypatch` на `app.github_client`
— без сети.
"""
from app import github_client

# Фикстуры api_env/auth_client — общие, tests/conftest.py.


def test_github_status_disconnected_by_default(auth_client):
    client, _ = auth_client
    r = client.get("/api/integrations/github")
    assert r.status_code == 200
    assert r.json() == {"connected": False, "login": None, "masked_token": None}


def test_connect_github_validates_token_and_stores_encrypted(auth_client, monkeypatch):
    client, Session = auth_client

    async def fake_validate(token):
        assert token == "ghp_realtoken12345"
        return "octocat"

    monkeypatch.setattr(github_client, "validate_token", fake_validate)

    r = client.post("/api/integrations/github", json={"token": "ghp_realtoken12345"})
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["login"] == "octocat"
    assert body["masked_token"].endswith("2345")
    assert "ghp_realtoken12345" not in body["masked_token"]

    # В БД лежит только шифротекст, не plaintext.
    s = Session()
    from app import models
    conn = s.query(models.GithubConnection).first()
    assert conn.token_secret != "ghp_realtoken12345"
    assert "ghp_realtoken12345" not in conn.token_secret
    s.close()

    status = client.get("/api/integrations/github").json()
    assert status == {"connected": True, "login": "octocat", "masked_token": body["masked_token"]}


def test_connect_github_rejects_invalid_token(auth_client, monkeypatch):
    client, _ = auth_client

    async def fake_validate(token):
        raise ValueError("GitHub-токен невалиден: HTTP 401")

    monkeypatch.setattr(github_client, "validate_token", fake_validate)

    r = client.post("/api/integrations/github", json={"token": "bad"})
    assert r.status_code == 400
    assert client.get("/api/integrations/github").json()["connected"] is False


def test_connect_github_rejects_empty_token(auth_client):
    client, _ = auth_client
    r = client.post("/api/integrations/github", json={"token": "   "})
    assert r.status_code == 400


def test_disconnect_github(auth_client, monkeypatch):
    client, _ = auth_client

    async def fake_validate(token):
        return "octocat"

    monkeypatch.setattr(github_client, "validate_token", fake_validate)
    client.post("/api/integrations/github", json={"token": "ghp_x"})
    assert client.get("/api/integrations/github").json()["connected"] is True

    r = client.delete("/api/integrations/github")
    assert r.status_code == 200
    assert client.get("/api/integrations/github").json()["connected"] is False


def test_list_repos_requires_connection(auth_client):
    client, _ = auth_client
    r = client.get("/api/integrations/github/repos")
    assert r.status_code == 400


def test_list_repos_returns_repos_when_connected(auth_client, monkeypatch):
    client, _ = auth_client

    async def fake_validate(token):
        return "octocat"

    async def fake_list_repos(token):
        assert token == "ghp_x"
        return [{"full_name": "octocat/Hello-World", "private": False},
                {"full_name": "octocat/secret-repo", "private": True}]

    monkeypatch.setattr(github_client, "validate_token", fake_validate)
    monkeypatch.setattr(github_client, "list_repos", fake_list_repos)

    client.post("/api/integrations/github", json={"token": "ghp_x"})
    r = client.get("/api/integrations/github/repos")
    assert r.status_code == 200
    assert r.json() == [
        {"full_name": "octocat/Hello-World", "private": False},
        {"full_name": "octocat/secret-repo", "private": True},
    ]


def test_integrations_require_auth(api_env):
    _, _, client = api_env
    assert client.get("/api/integrations/github").status_code == 401
    assert client.get("/api/integrations/github/repos").status_code == 401
    assert client.post("/api/integrations/github", json={"token": "x"}).status_code == 401
