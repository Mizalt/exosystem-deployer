"""Тесты генерации конфигов Nginx (без обращения к реальному Docker/FS проекта)."""
from app import config as app_config
from app.services import nginx_manager


def test_app_config_uses_resolver_and_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config(
        app_name="myapp", domain="my.example.com", ssl_cert_name="my.example.com"
    )
    content = (tmp_path / "myapp.conf").read_text(encoding="utf-8")

    # доводка прокси: resolver + переменная (анти-502 при рестарте деплоера)
    assert "resolver 127.0.0.11" in content
    assert "$deployer_upstream" in content
    assert "/api/proxy/myapp" in content
    assert "server_name my.example.com" in content
    # при ssl присутствует https-блок
    assert "listen 443 ssl" in content
    assert "fullchain.pem" in content


def test_app_config_http_only_without_ssl(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config(
        app_name="plainapp", domain="plain.example.com", ssl_cert_name=None
    )
    content = (tmp_path / "plainapp.conf").read_text(encoding="utf-8")

    assert "listen 80" in content
    assert "listen 443" not in content          # без SSL нет https-блока
    assert "proxy_pass" in content              # трафик идёт на деплоер, не redirect


def test_remove_application_config(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "NGINX_SITES_DIR", tmp_path)

    nginx_manager.update_application_nginx_config("tmpapp", "t.example.com")
    assert (tmp_path / "tmpapp.conf").exists()

    nginx_manager.remove_application_nginx_config("tmpapp")
    assert not (tmp_path / "tmpapp.conf").exists()
