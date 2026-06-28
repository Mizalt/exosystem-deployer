"""Пред-проверка ACME HTTP-01 challenge перед выпуском SSL (анти-403, диагностика).

Проверяет, что `acme_preflight` пишет одноразовый проб-файл в webroot и валидирует
выдачу challenge через nginx-контейнер, отделяя «битый nginx-конфиг» (403/404
локально) от «не настроен внешний DNS». Никогда не бросает исключение.
"""
from app import config as app_config
from app.services import ssl_service


def _token_from_cmd(cmd):
    """Достаёт ACME-токен из последнего аргумента wget-команды (URL .../<token>)."""
    return cmd[-1].rsplit("/", 1)[-1]


def test_preflight_ok_when_nginx_serves_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path)

    def fake_exec(container, cmd, user=""):
        # nginx отдал ровно тот токен, что лежит в webroot → 200 + тело = токен
        return 0, _token_from_cmd(cmd)

    monkeypatch.setattr(ssl_service.docker_manager, "exec_in_container", fake_exec)

    ok, detail = ssl_service.acme_preflight("panel.example.com")

    assert ok is True
    assert "проверка пройдена" in detail


def test_preflight_detects_403(monkeypatch, tmp_path):
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path)

    def fake_exec(container, cmd, user=""):
        # busybox wget на 403 выходит ненулевым кодом и печатает ошибку
        return 1, "wget: server returned error: HTTP/1.1 403 Forbidden"

    monkeypatch.setattr(ssl_service.docker_manager, "exec_in_container", fake_exec)

    ok, detail = ssl_service.acme_preflight("panel.example.com")

    assert ok is False
    assert "403" in detail


def test_preflight_cleans_up_probe_file(monkeypatch, tmp_path):
    """Проб-файл удаляется после проверки (не копится мусор в webroot)."""
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path)
    monkeypatch.setattr(
        ssl_service.docker_manager, "exec_in_container",
        lambda container, cmd, user="": (0, _token_from_cmd(cmd)),
    )

    ssl_service.acme_preflight("panel.example.com")

    challenge_dir = tmp_path / ".well-known" / "acme-challenge"
    leftovers = list(challenge_dir.glob("deployer-preflight-*")) if challenge_dir.exists() else []
    assert leftovers == []


def test_preflight_makes_probe_world_readable(monkeypatch, tmp_path):
    """Проб-файл получает 0644 — иначе nginx-воркер (uid != root) его не прочитает."""
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path)
    chmods = []
    monkeypatch.setattr(ssl_service.os, "chmod", lambda p, m: chmods.append((str(p), m)))
    monkeypatch.setattr(
        ssl_service.docker_manager, "exec_in_container",
        lambda c, cmd, user="": (0, _token_from_cmd(cmd)),
    )

    ok, _ = ssl_service.acme_preflight("panel.example.com")

    assert ok is True
    assert any(m == 0o644 for _, m in chmods)


def test_preflight_never_raises_on_docker_error(monkeypatch, tmp_path):
    """Сбой docker-exec не роняет выпуск — возвращается (False, detail)."""
    monkeypatch.setattr(app_config, "ACME_CHALLENGE_DIR", tmp_path)

    def boom(container, cmd, user=""):
        raise RuntimeError("docker daemon unreachable")

    monkeypatch.setattr(ssl_service.docker_manager, "exec_in_container", boom)

    ok, detail = ssl_service.acme_preflight("panel.example.com")

    assert ok is False
    assert "продолжаю выпуск" in detail
