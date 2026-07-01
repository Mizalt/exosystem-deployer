"""WS-гейт выпуска SSL не должен блокировать сам выпуск (ADR-053).

Регрессия: `perform_ssl_issuance` ждал подключения WebSocket-клиента и по таймауту
выходил, НЕ запуская certbot. Из-за этого авто-SSL из ЛК/реконсайлера (он дёргает
/api/ssl/issue, но WS не открывает) всегда «падал» пустышкой, хотя ручной выпуск из
панели (UI открывает WS) работал. Фикс: по таймауту продолжаем выпуск без стрима.
"""
import asyncio

from app.services import ssl_service


def test_ssl_issuance_runs_certbot_without_ws_client(monkeypatch):
    """Без подключённого WebSocket certbot всё равно запускается (ADR-053)."""
    # Быстрый таймаут, чтобы не ждать реальные 10 c в тесте.
    monkeypatch.setattr(ssl_service, "WS_WAIT_TIMEOUT", 0.05)
    # Изолируем от docker/nginx/файловой системы.
    monkeypatch.setattr(ssl_service.nginx_manager, "ensure_acme_challenge_ready", lambda: None)
    monkeypatch.setattr(ssl_service, "acme_preflight", lambda d: (True, "ok"))

    captured = {}

    def fake_stream(container, cmd):
        captured["cmd"] = cmd
        yield "certbot: ok"

    monkeypatch.setattr(ssl_service.docker_manager, "exec_stream_in_container", fake_stream)
    monkeypatch.setattr(ssl_service.docker_manager, "exec_in_container", lambda c, cmd: (0, "ok"))

    # Никакого WS-клиента не подключаем — имитируем вызов из реконсайлера/ЛК.
    asyncio.run(ssl_service.perform_ssl_issuance("task-no-ws", "panel.example.com"))

    assert "cmd" in captured, "certbot не запущен без WS — регрессия ADR-053"
    assert "certonly" in captured["cmd"]
    assert "panel.example.com" in captured["cmd"]
