"""Catchall nginx должен отдавать ACME HTTP-01 challenge (анти-403 при выпуске SSL).

Регрессия из живого теста: панельный/приложенческий SSL ловил 403, т.к. challenge
для домена без своего server-блока попадал в catchall с `return 403`. Catchall теперь
обслуживает `/.well-known/acme-challenge/` из webroot ДО возврата 403.
"""
from app import config as app_config
from app.services import nginx_manager
from app.services.nginx_manager import CATCHALL_CONFIG_TEMPLATE


def test_catchall_serves_acme_challenge():
    t = CATCHALL_CONFIG_TEMPLATE
    assert "location /.well-known/acme-challenge/" in t
    assert "root /var/www/acme_challenge;" in t


def test_catchall_acme_before_403():
    t = CATCHALL_CONFIG_TEMPLATE
    # ACME-локация должна идти раньше `return 403`, иначе challenge не отдаётся.
    assert t.index("acme-challenge") < t.index("return 403")


def test_write_catchall_creates_file_with_acme(monkeypatch, tmp_path):
    """_write_catchall_if_changed создаёт catchall с ACME-локацией и сигналит reload."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    monkeypatch.setattr(nginx_manager, "_ensure_default_ssl_files", lambda: None)

    changed = nginx_manager._write_catchall_if_changed()

    assert changed is True
    content = (tmp_path / "00-catchall.conf").read_text(encoding="utf-8")
    assert "location /.well-known/acme-challenge/" in content


def test_write_catchall_is_idempotent(monkeypatch, tmp_path):
    """Повторный вызов без изменений шаблона не перезаписывает (changed=False)."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    monkeypatch.setattr(nginx_manager, "_ensure_default_ssl_files", lambda: None)

    assert nginx_manager._write_catchall_if_changed() is True
    assert nginx_manager._write_catchall_if_changed() is False


def test_ensure_acme_reloads_only_on_change(monkeypatch, tmp_path):
    """ensure_acme_challenge_ready перезагружает Nginx только когда catchall изменился."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    monkeypatch.setattr(nginx_manager, "_ensure_default_ssl_files", lambda: None)
    reloads = []
    monkeypatch.setattr(nginx_manager, "reload_nginx", lambda: reloads.append(1))

    nginx_manager.ensure_acme_challenge_ready()   # первый раз пишет → reload
    nginx_manager.ensure_acme_challenge_ready()   # без изменений → без reload

    assert len(reloads) == 1


def test_ensure_webroot_traversable_sets_0755(monkeypatch, tmp_path):
    """Webroot делается проходимым (0755) — иначе nginx-воркер 403'ит ACME (umask-077 footgun)."""
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path / "acme")
    chmods = []
    monkeypatch.setattr(nginx_manager.os, "chmod", lambda p, m: chmods.append((str(p), m)))

    nginx_manager.ensure_acme_webroot_traversable()

    assert (tmp_path / "acme").exists()                  # webroot создан
    assert (str(tmp_path / "acme"), 0o755) in chmods      # и chmod 0755


def test_ensure_webroot_chmods_existing_challenge_subdirs(monkeypatch, tmp_path):
    """Если challenge-подкаталоги уже есть — их тоже делаем проходимыми."""
    webroot = tmp_path / "acme"
    (webroot / ".well-known" / "acme-challenge").mkdir(parents=True)
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", webroot)
    chmods = []
    monkeypatch.setattr(nginx_manager.os, "chmod", lambda p, m: chmods.append((str(p), m)))

    nginx_manager.ensure_acme_webroot_traversable()

    paths = {p for p, _ in chmods}
    assert str(webroot) in paths
    assert str(webroot / ".well-known") in paths
    assert str(webroot / ".well-known" / "acme-challenge") in paths


def test_ensure_webroot_never_raises(monkeypatch, tmp_path):
    """Сбой chmod (напр. чужая ФС) не должен ронять выпуск SSL."""
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path / "acme")

    def boom(p, m):
        raise OSError("read-only fs")

    monkeypatch.setattr(nginx_manager.os, "chmod", boom)
    nginx_manager.ensure_acme_webroot_traversable()  # не бросает


def test_ensure_acme_ready_fixes_webroot_and_catchall(monkeypatch, tmp_path):
    """ensure_acme_challenge_ready чинит И права webroot, И catchall (две причины 403)."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path / "acme")
    monkeypatch.setattr(nginx_manager, "_ensure_default_ssl_files", lambda: None)
    monkeypatch.setattr(nginx_manager, "reload_nginx", lambda: None)
    chmods = []
    monkeypatch.setattr(nginx_manager.os, "chmod", lambda p, m: chmods.append((str(p), m)))

    nginx_manager.ensure_acme_challenge_ready()

    assert (str(tmp_path / "acme"), 0o755) in chmods            # webroot починен
    assert (tmp_path / "00-catchall.conf").exists()             # catchall записан


def test_ensure_acme_does_not_touch_panel_config(monkeypatch, tmp_path):
    """Самоизлечение catchall НЕ трогает существующий panel-конфиг (в отличие от
    update_panel_nginx_config(domain=None), который его удаляет)."""
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path / "acme")
    monkeypatch.setattr(nginx_manager, "_ensure_default_ssl_files", lambda: None)
    monkeypatch.setattr(nginx_manager, "reload_nginx", lambda: None)
    panel = tmp_path / "10-panel.conf"
    panel.write_text("server { listen 80; server_name example.com; }", encoding="utf-8")

    nginx_manager.ensure_acme_challenge_ready()

    assert panel.exists()  # panel-конфиг не тронут
