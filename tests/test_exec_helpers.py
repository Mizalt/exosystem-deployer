"""Тесты exec-хелперов docker_manager (docker-py exec, без docker-cli).

Реальный Docker не нужен — клиент подменяется лёгкими фейками.
"""
import pytest

from app.services import docker_manager


# --------------------------------------------------------------------------- #
#  Фейки
# --------------------------------------------------------------------------- #
class _FakeExecResult:
    def __init__(self, exit_code, output):
        self.exit_code = exit_code
        self.output = output


class _FakeContainer:
    def __init__(self, result):
        self._result = result
        self.called_with = None

    def exec_run(self, cmd, user=""):
        self.called_with = (cmd, user)
        return self._result


class _FakeContainers:
    def __init__(self, container):
        self._container = container

    def get(self, name):
        return self._container


class _FakeApi:
    def __init__(self, chunks, exit_code):
        self._chunks = chunks
        self._exit = exit_code
        self.created_with = None

    def exec_create(self, name, cmd, user="", tty=False):
        self.created_with = (name, cmd, user)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=False):
        return iter(self._chunks)

    def exec_inspect(self, exec_id):
        return {"ExitCode": self._exit}


class _FakeClient:
    def __init__(self, container=None, api=None):
        self.containers = _FakeContainers(container)
        self.api = api


# --------------------------------------------------------------------------- #
#  exec_in_container
# --------------------------------------------------------------------------- #
def test_exec_in_container_returns_code_and_decoded_output(monkeypatch):
    cont = _FakeContainer(_FakeExecResult(0, b"hello\n"))
    monkeypatch.setattr(docker_manager, "client", _FakeClient(container=cont))

    code, out = docker_manager.exec_in_container("nginx", ["nginx", "-t"], user="root")

    assert code == 0
    assert out == "hello\n"
    assert cont.called_with == (["nginx", "-t"], "root")


def test_exec_in_container_handles_empty_output(monkeypatch):
    cont = _FakeContainer(_FakeExecResult(0, None))
    monkeypatch.setattr(docker_manager, "client", _FakeClient(container=cont))

    code, out = docker_manager.exec_in_container("nginx", ["true"])
    assert (code, out) == (0, "")


# --------------------------------------------------------------------------- #
#  exec_stream_in_container
# --------------------------------------------------------------------------- #
def test_exec_stream_yields_lines(monkeypatch):
    api = _FakeApi(chunks=[b"line1\nline2\n", b"line3\n"], exit_code=0)
    monkeypatch.setattr(docker_manager, "client", _FakeClient(api=api))

    lines = list(docker_manager.exec_stream_in_container("certbot", ["certbot", "--version"]))

    assert lines == ["line1", "line2", "line3"]
    assert api.created_with == ("certbot", ["certbot", "--version"], "")


def test_exec_stream_reassembles_split_chunks(monkeypatch):
    # Строка разорвана между чанками — хелпер должен склеить её обратно.
    api = _FakeApi(chunks=[b"par", b"tial\nrest"], exit_code=0)
    monkeypatch.setattr(docker_manager, "client", _FakeClient(api=api))

    assert list(docker_manager.exec_stream_in_container("certbot", ["x"])) == ["partial", "rest"]


def test_exec_stream_raises_on_nonzero_exit(monkeypatch):
    api = _FakeApi(chunks=[b"boom\n"], exit_code=1)
    monkeypatch.setattr(docker_manager, "client", _FakeClient(api=api))

    gen = docker_manager.exec_stream_in_container("certbot", ["fail"])
    with pytest.raises(RuntimeError):
        list(gen)  # исключение поднимается после исчерпания вывода
