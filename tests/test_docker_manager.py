"""Тесты docker_manager: генерация Dockerfile и сборка сирот."""
import zipfile

import docker
import pytest

from app.services import docker_manager
from tests.conftest import FakeContainer, FakeDockerClient


def _make_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("main.py", "x = 1")
    return path


class _NoImage:
    def get(self, tag):
        raise docker.errors.ImageNotFound("no")


def test_build_streaming_calls_on_line(monkeypatch, tmp_path):
    """on_line получает строки лога сборки (живые WS-логи, ADR-023)."""
    lines = []

    class _Api:
        def build(self, **kwargs):
            yield {"stream": "Step 1/3\n"}
            yield {"stream": "Step 2/3\n"}
            yield {"aux": {"ID": "sha256:abc"}}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    tag = docker_manager.build_image_if_needed(_make_zip(tmp_path / "a.zip"), image_cache_key="h", on_line=lines.append)
    assert tag.startswith("deployer-cache:")
    assert "Step 1/3" in lines and "Step 2/3" in lines


def test_build_streaming_raises_on_error(monkeypatch, tmp_path):
    """Ошибка сборки в потоковом режиме (chunk {'error'}) → RuntimeError с логом."""
    class _Api:
        def build(self, **kwargs):
            yield {"stream": "Step 1/3\n"}
            yield {"error": "boom failed"}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    with pytest.raises(RuntimeError) as ei:
        docker_manager.build_image_if_needed(_make_zip(tmp_path / "b.zip"), image_cache_key="h2", on_line=lambda s: None)
    assert "boom failed" in str(ei.value)


def test_generate_dockerfile_has_uvicorn_cmd():
    df = docker_manager.generate_dockerfile()
    assert "uvicorn" in df
    assert "CMD" in df
    assert "--port" in df
    assert "EXPOSE 80" in df
    assert "pip install" in df  # питон-база ставит зависимости


def test_generate_dockerfile_custom_port():
    df = docker_manager.generate_dockerfile(internal_port=8080)
    assert "EXPOSE 8080" in df
    assert "--port" in df and "8080" in df


def test_generate_dockerfile_custom_command_and_nonpython_base():
    # Иная база (node) → без pip; своя команда → CMD как есть.
    df = docker_manager.generate_dockerfile(base_image="node:20-alpine", run_command="node index.js", internal_port=3000)
    assert "FROM node:20-alpine" in df
    assert "pip install" not in df
    assert "CMD node index.js" in df
    assert "EXPOSE 3000" in df


def test_generate_dockerfile_python_with_custom_command_keeps_pip():
    # Питон + своя команда (бот) → pip ставится, но запускается команда пользователя.
    df = docker_manager.generate_dockerfile(run_command="python bot.py")
    assert "pip install" in df
    assert "CMD python bot.py" in df


def test_build_cache_key_includes_build_config(monkeypatch, tmp_path):
    """Смена build_config меняет тег образа (инвалидирует кэш)."""
    zip_path = tmp_path / "a.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake")
    captured = {}

    class _Imgs:
        def get(self, tag):
            captured["tag"] = tag
            return object()  # образ «уже есть» → сборка пропускается

    class _Client:
        images = _Imgs()

    monkeypatch.setattr(docker_manager, "client", _Client())
    tag_a = docker_manager.build_image_if_needed(zip_path, image_cache_key="h", build_config={"internal_port": 80})
    tag_b = docker_manager.build_image_if_needed(zip_path, image_cache_key="h", build_config={"internal_port": 9000})
    assert tag_a != tag_b  # разный порт → разный образ


def test_cleanup_orphans_removes_only_unknown(monkeypatch):
    fake = FakeDockerClient()
    keep = FakeContainer("deployer-keep")
    orphan = FakeContainer("deployer-orphan", status="restarting")
    fake.containers.add(keep)
    fake.containers.add(orphan)
    monkeypatch.setattr(docker_manager, "client", fake)

    removed = docker_manager.cleanup_orphan_containers({"deployer-keep"})

    assert removed == 1
    assert orphan.removed is True
    assert keep.removed is False


def test_cleanup_orphans_empty_known_removes_all(monkeypatch):
    fake = FakeDockerClient()
    a = FakeContainer("deployer-a")
    b = FakeContainer("deployer-b")
    fake.containers.add(a)
    fake.containers.add(b)
    monkeypatch.setattr(docker_manager, "client", fake)

    removed = docker_manager.cleanup_orphan_containers(set())

    assert removed == 2
    assert a.removed and b.removed
