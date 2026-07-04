"""V-07: лимит размера прямой загрузки артефакта (защита от OOM/DoS)."""
from app import models, security


def _seed_blueprint(Session) -> int:
    s = Session()
    bp = models.AppBlueprint(name="app")
    s.add(bp)
    s.commit()
    bp_id = bp.id
    s.close()
    return bp_id


def test_upload_artifact_rejects_oversized(api_env, monkeypatch):
    app, Session, client = api_env
    bp_id = _seed_blueprint(Session)
    fake = models.User(id=1, username="tester", hashed_password="x", token_version=1)
    app.dependency_overrides[security.get_current_user] = lambda: fake

    import main
    monkeypatch.setattr(main, "MAX_ARTIFACT_BYTES", 1024)  # 1 КБ потолок для теста

    big = b"x" * 5000
    r = client.post(
        f"/api/blueprints/{bp_id}/artifacts",
        files={"zip_file": ("a.zip", big, "application/zip")},
        data={"version_tag": "v1"},
    )
    assert r.status_code == 413


def test_upload_artifact_allows_within_limit(api_env, monkeypatch):
    app, Session, client = api_env
    bp_id = _seed_blueprint(Session)
    fake = models.User(id=1, username="tester", hashed_password="x", token_version=1)
    app.dependency_overrides[security.get_current_user] = lambda: fake

    import main
    monkeypatch.setattr(main, "MAX_ARTIFACT_BYTES", 1024 * 1024)  # 1 МБ — с запасом

    small = b"print('hi')\n"
    r = client.post(
        f"/api/blueprints/{bp_id}/artifacts",
        files={"zip_file": ("a.zip", small, "application/zip")},
        data={"version_tag": "v1", "description": "t"},
    )
    assert r.status_code == 200
    assert r.json()["version_tag"] == "v1"
