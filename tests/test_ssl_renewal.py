"""Ночь 16 — автопродление SSL (ADR-085): чекер сроков + задача ssl_renew.

Инварианты:
  • следим за ВСЕМИ сертами в работе (приложения + панель), неиспользуемые не трогаем;
  • продление за ~30 дней; успех = срок реально сдвинулся (не «файл существует»);
  • не удаётся и осталось ≤14 дней → error-задача (алерт центра задач/зеркала ЛК);
  • загруженный вручную серт продлить нельзя — честный алерт заранее;
  • дедуп: одна активная задача на серт; после done/error — сутки тишины.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import config, models
from app.services import pending_actions as pa
from app.services import ssl_renewal


# --- Генерация тестовых сертификатов (самоподписанные, EC — быстрые) ---------- #

def _write_cert(base, name: str, days: float, le: bool = True) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            # not_before всегда за 90 дн. до истечения — корректно и для уже
            # просроченных сертов (days < 0), нужных тестам «истёк N дн. назад».
            .not_valid_before(now + timedelta(days=days) - timedelta(days=90))
            .not_valid_after(now + timedelta(days=days))
            .sign(key, hashes.SHA256()))
    cert_dir = (base / "live" / name) if le else (base / name)
    cert_dir.mkdir(parents=True, exist_ok=True)
    (cert_dir / "fullchain.pem").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture
def ssl_dir(tmp_path, monkeypatch):
    """Изолированный каталог сертификатов (вместо ssl_certs/ репозитория)."""
    monkeypatch.setattr(config, "SSL_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def panel_off(monkeypatch):
    """Панель без своего домена/серта (по умолчанию в тестах)."""
    from app import panel_config
    monkeypatch.setattr(ssl_renewal.panel_config, "load_settings",
                        lambda: panel_config.PanelSettings())


def _add_app(db, deployment, domain: str, cert: str | None):
    row = models.Application(name=f"app-{domain}", domain=domain,
                             ssl_cert_name=cert, deployment_id=deployment.id)
    db.add(row)
    db.commit()
    return row


class _NoCloseSession:
    """SessionLocal-заглушка: отдаёт живую тестовую сессию, close() глотает."""
    def __init__(self, real):
        self._real = real

    def __getattr__(self, item):
        return getattr(self._real, item)

    def close(self):
        pass


# --- cert_expiry / certificates_in_use / expiring_report ---------------------- #

def test_cert_expiry_parses_le_and_custom(ssl_dir):
    _write_cert(ssl_dir, "le.example.com", days=20, le=True)
    _write_cert(ssl_dir, "my-custom", days=10, le=False)
    for name, days in (("le.example.com", 20), ("my-custom", 10)):
        expiry = ssl_renewal.cert_expiry(name)
        assert expiry is not None
        assert abs(ssl_renewal.days_left(expiry) - days) < 1.1
    assert ssl_renewal.is_letsencrypt("le.example.com") is True
    assert ssl_renewal.is_letsencrypt("my-custom") is False
    assert ssl_renewal.cert_expiry("ghost") is None


def test_certificates_in_use_apps_and_panel(db, deployment, ssl_dir, monkeypatch):
    from app import panel_config
    _add_app(db, deployment, "a.example.com", "a.example.com")
    _add_app(db, deployment, "b.example.com", None)  # без SSL — не следим
    monkeypatch.setattr(ssl_renewal.panel_config, "load_settings",
                        lambda: panel_config.PanelSettings(
                            domain="panel.example.com", ssl_cert_name="panel.example.com"))
    in_use = {c["name"]: c["uses"] for c in ssl_renewal.certificates_in_use(db)}
    assert in_use == {"a.example.com": ["app:a.example.com"],
                      "panel.example.com": ["panel"]}


def test_expiring_report_statuses_and_sort(db, deployment, ssl_dir, panel_off):
    _write_cert(ssl_dir, "warn.example.com", days=20)
    _write_cert(ssl_dir, "alert.example.com", days=10)
    _write_cert(ssl_dir, "fine.example.com", days=60)
    for d in ("warn.example.com", "alert.example.com", "fine.example.com"):
        _add_app(db, deployment, d, d)
    report = ssl_renewal.expiring_report(db)
    assert [i["name"] for i in report] == ["alert.example.com", "warn.example.com"]
    assert report[0]["status"] == "alert" and report[0]["days_left"] <= 14
    assert report[1]["status"] == "warning"
    assert all(i["renewable"] is True for i in report)


# --- sweep_once: постановка задач + дедуп ------------------------------------- #

def test_sweep_creates_task_once(db, deployment, ssl_dir, panel_off, monkeypatch):
    monkeypatch.setattr(ssl_renewal, "SessionLocal", lambda: _NoCloseSession(db))
    _write_cert(ssl_dir, "soon.example.com", days=20)
    _write_cert(ssl_dir, "fine.example.com", days=60)
    _add_app(db, deployment, "soon.example.com", "soon.example.com")
    _add_app(db, deployment, "fine.example.com", "fine.example.com")

    assert ssl_renewal.sweep_once() == 1
    actions = db.query(models.PendingAction).all()
    assert len(actions) == 1 and actions[0].type == "ssl_renew"
    params = json.loads(actions[0].params)
    assert params["cert"] == "soon.example.com" and params["renewable"] is True

    # Повторный проход: активная задача уже есть — дубликат не ставим.
    assert ssl_renewal.sweep_once() == 0


def test_sweep_cooldown_after_finished_task(db, deployment, ssl_dir, panel_off, monkeypatch):
    monkeypatch.setattr(ssl_renewal, "SessionLocal", lambda: _NoCloseSession(db))
    _write_cert(ssl_dir, "soon.example.com", days=10)
    _add_app(db, deployment, "soon.example.com", "soon.example.com")

    # Недавно завершённая (error = видимый алерт) задача этого серта → сутки тишины.
    action = models.PendingAction(
        type="ssl_renew", status="error",
        params=json.dumps({"cert": "soon.example.com"}))
    db.add(action)
    db.commit()
    assert ssl_renewal.sweep_once() == 0

    # «Постаревшая» завершённая задача — пора ставить новую (попытки не сдаются).
    old = datetime.utcnow() - timedelta(seconds=ssl_renewal.RESWEEP_COOLDOWN + 60)
    action.created_at = old
    action.updated_at = old
    db.commit()
    assert ssl_renewal.sweep_once() == 1


def test_sweep_ignores_unused_certs(db, ssl_dir, panel_off, monkeypatch):
    monkeypatch.setattr(ssl_renewal, "SessionLocal", lambda: _NoCloseSession(db))
    _write_cert(ssl_dir, "orphan.example.com", days=5)  # никем не используется
    assert ssl_renewal.sweep_once() == 0


# --- Обработчик ssl_renew ------------------------------------------------------ #

def _make_renew_action(db, cert="soon.example.com", renewable=True):
    action = models.PendingAction(
        type="ssl_renew", status="pending", title=f"Продление SSL: {cert}",
        params=json.dumps({"cert": cert, "renewable": renewable}))
    db.add(action)
    db.commit()
    return action


def test_renew_success_measures_and_done(db, ssl_dir, monkeypatch):
    _write_cert(ssl_dir, "soon.example.com", days=20)
    action = _make_renew_action(db)

    def fake_issue(domain):
        _write_cert(ssl_dir, domain, days=90)  # certbot заменил файлы на месте
        return True, "renewed"

    monkeypatch.setattr(pa, "issue_certificate", fake_issue)
    pa._handle_ssl_renew(db, action)
    assert action.status == "done"
    assert "продлён" in action.result
    metrics = db.query(models.OperationMetric).filter_by(kind="ssl_renew").all()
    assert len(metrics) == 1 and metrics[0].outcome == "done"


def test_renew_failure_retries_with_backoff(db, ssl_dir, monkeypatch):
    _write_cert(ssl_dir, "soon.example.com", days=20)  # >14 дн. — ещё не алерт
    action = _make_renew_action(db)
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (False, "certbot упал"))
    pa._handle_ssl_renew(db, action)
    assert action.status == "pending"          # попытки продолжаются
    assert action.next_check_at is not None
    assert json.loads(action.params)["renew_attempts"] == 1


def test_renew_failure_alerts_at_14_days(db, ssl_dir, monkeypatch):
    _write_cert(ssl_dir, "soon.example.com", days=10)  # ≤14 дн. — алерт
    action = _make_renew_action(db)
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (False, "certbot упал"))
    pa._handle_ssl_renew(db, action)
    assert action.status == "error"
    assert "истекает" in action.result


def test_days_phrase_and_expiry_text():
    """Просроченный серт — «истёк N дн. назад», а не «через -N дн.» (фикс 2026-07-05)."""
    assert ssl_renewal.days_phrase(5.7) == "истекает через 5 дн."
    assert ssl_renewal.days_phrase(-104.3) == "истёк 104 дн. назад"
    expiry = datetime(2026, 3, 22, tzinfo=timezone.utc)
    assert ssl_renewal.expiry_text(expiry, 10.2) == "истекает 22.03.2026 (через 10 дн.)"
    assert ssl_renewal.expiry_text(expiry, -104.0) == "истёк 22.03.2026 (104 дн. назад)"


def test_renew_failure_expired_cert_says_ago(db, ssl_dir, monkeypatch):
    """Серт УЖЕ истёк: алерт говорит «истёк N дн. назад», а не «через -104 дн.»."""
    _write_cert(ssl_dir, "late.example.com", days=-104)
    action = _make_renew_action(db, cert="late.example.com")
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (False, "certbot упал"))
    pa._handle_ssl_renew(db, action)
    assert action.status == "error"
    assert "истёк" in action.result and "дн. назад" in action.result
    assert "через -" not in action.result and "через -" not in (action.log or "")


def test_renew_manual_expired_cert_says_ago(db, ssl_dir, monkeypatch):
    """Ручной серт уже истёк: честное «истёк N дн. назад» без отрицательных дней."""
    _write_cert(ssl_dir, "old-custom", days=-30, le=False)
    action = _make_renew_action(db, cert="old-custom", renewable=False)
    monkeypatch.setattr(pa, "issue_certificate", lambda d: (True, ""))
    pa._handle_ssl_renew(db, action)
    assert action.status == "error"
    assert "вручную" in action.result and "дн. назад" in action.result
    assert "через -" not in action.result


def test_renew_manual_cert_alerts_without_certbot(db, ssl_dir, monkeypatch):
    _write_cert(ssl_dir, "my-custom", days=10, le=False)
    action = _make_renew_action(db, cert="my-custom", renewable=False)
    called = {"n": 0}
    monkeypatch.setattr(pa, "issue_certificate",
                        lambda d: (called.__setitem__("n", called["n"] + 1), (True, ""))[1])
    pa._handle_ssl_renew(db, action)
    assert action.status == "error"
    assert "вручную" in action.result
    assert called["n"] == 0  # certbot не дёргали — продлить такой серт нельзя


def test_renew_missing_cert_fails(db, ssl_dir):
    action = _make_renew_action(db, cert="ghost.example.com")
    pa._handle_ssl_renew(db, action)
    assert action.status == "error"
    assert "исчез" in action.result


def test_renew_already_renewed_is_done(db, ssl_dir, monkeypatch):
    _write_cert(ssl_dir, "soon.example.com", days=80)  # уже продлён кем-то
    action = _make_renew_action(db)
    called = {"n": 0}
    monkeypatch.setattr(pa, "issue_certificate",
                        lambda d: (called.__setitem__("n", called["n"] + 1), (True, ""))[1])
    pa._handle_ssl_renew(db, action)
    assert action.status == "done"
    assert called["n"] == 0


def test_describe_stage_ssl_renew(db, ssl_dir):
    action = _make_renew_action(db)
    stage = pa.describe_stage(db, action)
    assert stage["stage"] == "ssl_renew"
    assert stage["unpredictable"] is False


# --- API + capability ---------------------------------------------------------- #

def test_capability_declared():
    from app import version
    assert "ssl_renewal" in version.capabilities()


def test_expiring_endpoint(auth_client, ssl_dir, panel_off, monkeypatch):
    client, Session = auth_client
    r = client.get("/api/ssl/expiring")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["renew_before_days"] == ssl_renewal.RENEW_BEFORE_DAYS
    assert body["alert_days"] == ssl_renewal.ALERT_DAYS


def test_expiring_endpoint_requires_auth(api_env, ssl_dir):
    _app, _Session, client = api_env
    assert client.get("/api/ssl/expiring").status_code == 401
