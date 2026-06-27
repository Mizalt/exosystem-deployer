"""Тесты анти-брутфорс лимита на логин (app/rate_limit.py + /api/auth/token)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import models, security
from app.rate_limit import LoginRateLimiter, login_limiter, client_keys


# --- Юнит-тесты лимитера ---------------------------------------------------- #

def test_limiter_blocks_after_max_fails():
    rl = LoginRateLimiter(max_fails=3, window=300)
    keys = ["ip:1.2.3.4"]
    for _ in range(3):
        assert rl.retry_after(keys) == 0
        rl.record_failure(keys)
    assert rl.retry_after(keys) > 0  # 3 неудачи → блок


def test_limiter_reset_clears_block():
    rl = LoginRateLimiter(max_fails=2, window=300)
    keys = ["ip:5.6.7.8", "user:admin"]
    rl.record_failure(keys)
    rl.record_failure(keys)
    assert rl.retry_after(keys) > 0
    rl.reset(keys)
    assert rl.retry_after(keys) == 0  # успешный вход снял блок


def test_limiter_keys_are_independent():
    rl = LoginRateLimiter(max_fails=2, window=300)
    rl.record_failure(["ip:a"])
    rl.record_failure(["ip:a"])
    assert rl.retry_after(["ip:a"]) > 0
    assert rl.retry_after(["ip:b"]) == 0  # другой IP не затронут


def test_client_keys_prefers_xff_last_hop():
    class _Req:
        headers = {"x-forwarded-for": "9.9.9.9, 10.0.0.5"}
        client = None
    keys = client_keys(_Req(), "Admin")
    assert keys[0] == "ip:10.0.0.5"      # последний хоп (добавлен nginx)
    assert keys[1] == "user:admin"        # нормализация регистра


# --- API: 429 после серии неудач ------------------------------------------- #

@pytest.fixture
def client_with_user():
    login_limiter.clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add(models.User(username="admin", hashed_password=security.get_password_hash("correct-horse")))
    s.commit()

    import main
    app = main.app

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        login_limiter.clear()


def test_login_locks_out_after_repeated_failures(client_with_user):
    client = client_with_user
    # 10 неудач (дефолтный max_fails) → 401, далее блок.
    for _ in range(10):
        r = client.post("/api/auth/token", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/auth/token", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_successful_login_resets_counter(client_with_user):
    client = client_with_user
    for _ in range(3):
        client.post("/api/auth/token", data={"username": "admin", "password": "wrong"})
    ok = client.post("/api/auth/token", data={"username": "admin", "password": "correct-horse"})
    assert ok.status_code == 200
    # После успеха счётчик сброшен — снова можно ошибаться без мгновенного 429.
    r = client.post("/api/auth/token", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
