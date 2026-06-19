"""Тесты онбординга: автосоздание администратора при первом запуске."""
from app import bootstrap, models, security


def test_creates_admin_with_generated_password(db, monkeypatch, capsys):
    monkeypatch.delenv("DEPLOYER_ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("DEPLOYER_ADMIN_PASSWORD", raising=False)

    bootstrap.ensure_admin_exists(db)

    users = db.query(models.User).all()
    assert len(users) == 1
    assert users[0].username == "admin"
    assert users[0].hashed_password and users[0].hashed_password != ""
    # сгенерированный пароль печатается один раз
    assert "СОЗДАН АДМИНИСТРАТОР" in capsys.readouterr().out


def test_uses_env_credentials(db, monkeypatch):
    monkeypatch.setenv("DEPLOYER_ADMIN_USERNAME", "boss")
    monkeypatch.setenv("DEPLOYER_ADMIN_PASSWORD", "s3cret-pass")

    bootstrap.ensure_admin_exists(db)

    u = db.query(models.User).filter(models.User.username == "boss").first()
    assert u is not None
    assert security.verify_password("s3cret-pass", u.hashed_password)


def test_idempotent_when_admin_already_exists(db):
    db.add(models.User(username="existing", hashed_password="hash"))
    db.commit()

    bootstrap.ensure_admin_exists(db)

    assert db.query(models.User).count() == 1
