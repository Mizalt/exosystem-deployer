"""Анти-регресс упаковки: образ и код не должны зависеть от docker-cli.

Управление Docker идёт только через docker-py (см. docker_manager.exec_*),
поэтому ни docker-cli в образе, ни subprocess `docker exec` в коде быть не должно.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_has_no_docker_cli():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "docker-ce-cli" not in df
    # slim-образ без apt-слоя установки пакетов
    assert "apt-get install" not in df


def test_app_code_has_no_subprocess_or_docker_exec():
    app_dir = ROOT / "app"
    for py in app_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "import subprocess" not in text, f"{py.name} снова тащит subprocess"
        assert '"docker", "exec"' not in text, f"{py.name} снова вызывает docker exec через CLI"
