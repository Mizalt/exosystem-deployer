"""Тесты стабильности ноды при публикации (ADR-137).

Боевой инцидент: параллельные docker build душили единственное ядро ноды и
исчерпывали пул БД. Проверяем митигацию: сериализация сборок семафором
(с деградацией по таймауту очереди), расширенный пул БД, best-effort троттл
CPU сборки (cpushares). Реальный docker build НЕ запускается — клиент мокается.
"""
import threading
import time
import zipfile

import docker
import pytest

from app.services import docker_manager


def _make_zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("main.py", "x = 1")
    return path


class _NoImage:
    def get(self, tag):
        raise docker.errors.ImageNotFound("no")


# --------------------------------------------------------------------------- #
#  (а) Семафор: реально ограничивает параллельность и всегда возвращает слот
# --------------------------------------------------------------------------- #

def test_build_semaphore_limits_concurrency(monkeypatch, tmp_path):
    """Две «параллельные» сборки при лимите 1 идут строго по очереди
    (вторая ЖДЁТ слот, одновременно активна максимум одна)."""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)

    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}
    gate = threading.Event()  # держит первую сборку «в работе»

    class _Api:
        def build(self, **kwargs):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            gate.wait(timeout=5)
            with lock:
                state["active"] -= 1
            yield {"stream": "done\n"}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())

    threads = [
        threading.Thread(
            target=docker_manager._build_image_streaming,
            args=(tmp_path, f"deployer-cache:conc{i}"),
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    time.sleep(0.3)  # вторая сборка должна стоять в очереди, а не работать
    assert state["max_active"] == 1
    gate.set()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    assert state["max_active"] == 1  # ни в один момент не было двух сборок
    # После обеих сборок слот свободен (release отработал).
    assert sem.acquire(blocking=False) is True
    sem.release()


def test_build_semaphore_released_on_error(monkeypatch, tmp_path):
    """Ошибка сборки (chunk {'error'}) → RuntimeError, но слот ВОЗВРАЩЁН."""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)

    class _Api:
        def build(self, **kwargs):
            yield {"stream": "Step 1/2\n"}
            yield {"error": "boom failed"}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    with pytest.raises(RuntimeError):
        docker_manager._build_image_streaming(tmp_path, "deployer-cache:err")
    assert sem.acquire(blocking=False) is True  # слот вернулся несмотря на ошибку
    sem.release()


def test_build_semaphore_released_on_stream_exception(monkeypatch, tmp_path):
    """Исключение самого стрима (демон оборвал сборку) → слот тоже ВОЗВРАЩЁН."""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)

    class _Api:
        def build(self, **kwargs):
            yield {"stream": "Step 1/2\n"}
            raise ConnectionError("daemon gone")

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    with pytest.raises(ConnectionError):
        docker_manager._build_image_streaming(tmp_path, "deployer-cache:crash")
    assert sem.acquire(blocking=False) is True
    sem.release()


# --------------------------------------------------------------------------- #
#  (б) Таймаут очереди: деградация вместо вечного ожидания
# --------------------------------------------------------------------------- #

def test_build_degrades_when_queue_timeout(monkeypatch, tmp_path):
    """Слот занят «зависшей» чужой сборкой + маленький таймаут → сборка
    ДЕГРАДИРУЕТ (выполняется без слота), не висит и НЕ отпускает чужой слот."""
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)  # чужая «зависшая» сборка держит слот
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)
    monkeypatch.setenv("DEPLOYER_BUILD_QUEUE_TIMEOUT", "0.05")

    lines = []

    class _Api:
        def build(self, **kwargs):
            yield {"stream": "Step 1/1\n"}
            yield {"aux": {"ID": "sha256:abc"}}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    docker_manager._build_image_streaming(
        tmp_path, "deployer-cache:degraded", on_line=lines.append)
    assert any("Step 1/1" in ln for ln in lines)  # сборка прошла, не зависла
    # Деградация НЕ освободила чужой слот (release только если сами захватили).
    assert sem.acquire(blocking=False) is False


def test_build_concurrency_env_sanitized(monkeypatch):
    """Лимит одновременных сборок: дефолт 1, санация в 1..8, мусор → дефолт."""
    monkeypatch.delenv("DEPLOYER_MAX_CONCURRENT_BUILDS", raising=False)
    assert docker_manager._env_build_concurrency() == 1
    monkeypatch.setenv("DEPLOYER_MAX_CONCURRENT_BUILDS", "3")
    assert docker_manager._env_build_concurrency() == 3
    monkeypatch.setenv("DEPLOYER_MAX_CONCURRENT_BUILDS", "99")
    assert docker_manager._env_build_concurrency() == 8
    monkeypatch.setenv("DEPLOYER_MAX_CONCURRENT_BUILDS", "0")
    assert docker_manager._env_build_concurrency() == 1
    monkeypatch.setenv("DEPLOYER_MAX_CONCURRENT_BUILDS", "мусор")
    assert docker_manager._env_build_concurrency() == 1


def test_build_queue_timeout_env_sanitized(monkeypatch):
    """Таймаут очереди: дефолт 1200, мусор/≤0 → дефолт."""
    monkeypatch.delenv("DEPLOYER_BUILD_QUEUE_TIMEOUT", raising=False)
    assert docker_manager._env_build_queue_timeout() == 1200.0
    monkeypatch.setenv("DEPLOYER_BUILD_QUEUE_TIMEOUT", "600")
    assert docker_manager._env_build_queue_timeout() == 600.0
    monkeypatch.setenv("DEPLOYER_BUILD_QUEUE_TIMEOUT", "-5")
    assert docker_manager._env_build_queue_timeout() == 1200.0
    monkeypatch.setenv("DEPLOYER_BUILD_QUEUE_TIMEOUT", "abc")
    assert docker_manager._env_build_queue_timeout() == 1200.0


# --------------------------------------------------------------------------- #
#  (б2) Дофикс по ревью: слот не течёт при сбое учёта прогресса;
#       после очереди кэш перепроверяется (не пересобираем готовый тег)
# --------------------------------------------------------------------------- #

def test_build_slot_released_when_begin_raises(monkeypatch, tmp_path):
    """Исключение из build_progress.begin (после захвата, до стрима) НЕ теряет
    слот — захват под общим try, release во внешнем finally."""
    from app.services import build_progress
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)

    def _boom(tag):
        raise RuntimeError("begin failed")

    monkeypatch.setattr(build_progress, "begin", _boom)

    class _Api:
        def build(self, **kwargs):
            yield {"stream": "не должно дойти\n"}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    with pytest.raises(RuntimeError, match="begin failed"):
        docker_manager._build_image_streaming(tmp_path, "deployer-cache:beginboom")
    assert sem.acquire(blocking=False) is True  # слот вернулся
    sem.release()


def test_build_slot_released_when_finish_raises(monkeypatch, tmp_path):
    """Исключение из build_progress.finish (во внутреннем finally) НЕ теряет
    слот — release стоит во ВНЕШНЕМ finally и выполняется в любом случае."""
    from app.services import build_progress
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)

    def _boom(tag, ok):
        raise RuntimeError("finish failed")

    monkeypatch.setattr(build_progress, "finish", _boom)

    class _Api:
        def build(self, **kwargs):
            yield {"stream": "Step 1/1\n"}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    try:
        with pytest.raises(RuntimeError, match="finish failed"):
            docker_manager._build_image_streaming(tmp_path, "deployer-cache:finishboom")
    finally:
        # finish был замокан и не снял сборку с учёта — чистим реестр за собой.
        with build_progress._lock:
            build_progress._active.pop("deployer-cache:finishboom", None)
    assert sem.acquire(blocking=False) is True
    sem.release()


class _ImageReady:
    def get(self, tag):
        return object()  # образ уже существует


def test_build_skips_rebuild_when_image_appeared_after_wait(monkeypatch, tmp_path):
    """Пока сборка ждала слот, параллельная публикация собрала ЭТОТ ЖЕ тег →
    после очереди пересборка пропускается (client.api.build не вызывается),
    чужой слот не трогается (деградация)."""
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)  # чужая «зависшая» сборка держит слот
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)
    monkeypatch.setenv("DEPLOYER_BUILD_QUEUE_TIMEOUT", "0.05")

    calls = {"build": 0}

    class _Api:
        def build(self, **kwargs):
            calls["build"] += 1
            yield {"stream": "не должно выполниться\n"}

    class _Client:
        api = _Api()
        images = _ImageReady()

    monkeypatch.setattr(docker_manager, "client", _Client())
    lines = []
    docker_manager._build_image_streaming(
        tmp_path, "deployer-cache:ready", on_line=lines.append)
    assert calls["build"] == 0                       # пересборки не было
    assert any("уже собран" in ln for ln in lines)   # юзеру объяснили причину
    assert sem.acquire(blocking=False) is False      # чужой слот не отпущен


def test_build_recheck_after_wait_releases_own_slot(monkeypatch, tmp_path):
    """Скип после успешного ЗАХВАТА слота (дождались очереди, образ уже есть) —
    свой слот возвращается: ранний return проходит через общий finally."""
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)  # слот занят «чужой» сборкой
    monkeypatch.setattr(docker_manager, "_build_semaphore", sem)
    monkeypatch.setenv("DEPLOYER_BUILD_QUEUE_TIMEOUT", "10")

    class _Api:
        def build(self, **kwargs):
            raise AssertionError("build не должен вызываться")

    class _Client:
        api = _Api()
        images = _ImageReady()

    monkeypatch.setattr(docker_manager, "client", _Client())
    releaser = threading.Timer(0.1, sem.release)  # «чужая» сборка завершается
    releaser.start()
    try:
        docker_manager._build_image_streaming(tmp_path, "deployer-cache:readyown")
    finally:
        releaser.cancel()
    assert sem.acquire(blocking=False) is True  # свой слот вернули
    sem.release()


# --------------------------------------------------------------------------- #
#  (в) Пул БД: параметры проставлены в engine
# --------------------------------------------------------------------------- #

def test_db_engine_pool_parameters():
    """Пул БД расширен (ADR-137): дефолт QueuePool 5+10 исчерпывался в инциденте
    (сессия держится всю сборку). Проверяем реальные параметры engine."""
    from app.database import engine
    pool = engine.pool
    assert pool.size() == 10                 # DEPLOYER_DB_POOL_SIZE дефолт
    assert pool._max_overflow == 20          # DEPLOYER_DB_MAX_OVERFLOW дефолт
    assert pool._timeout == 30
    assert pool._recycle == 1800
    assert pool._pre_ping is True


def test_db_pool_env_sanitized(monkeypatch):
    """Санация env пула: пусто/мусор → дефолт, клампы работают."""
    from app import database
    monkeypatch.delenv("DEPLOYER_DB_POOL_SIZE", raising=False)
    assert database._env_pool_int("DEPLOYER_DB_POOL_SIZE", 10, 1, 64) == 10
    monkeypatch.setenv("DEPLOYER_DB_POOL_SIZE", "24")
    assert database._env_pool_int("DEPLOYER_DB_POOL_SIZE", 10, 1, 64) == 24
    monkeypatch.setenv("DEPLOYER_DB_POOL_SIZE", "9999")
    assert database._env_pool_int("DEPLOYER_DB_POOL_SIZE", 10, 1, 64) == 64
    monkeypatch.setenv("DEPLOYER_DB_POOL_SIZE", "junk")
    assert database._env_pool_int("DEPLOYER_DB_POOL_SIZE", 10, 1, 64) == 10


# --------------------------------------------------------------------------- #
#  (г) Троттл CPU сборки: container_limits с cpushares
# --------------------------------------------------------------------------- #

def _capture_build_kwargs(monkeypatch):
    captured = {}

    class _Api:
        def build(self, **kwargs):
            captured.update(kwargs)
            yield {"stream": "ok\n"}

    class _Client:
        api = _Api()
        images = _NoImage()

    monkeypatch.setattr(docker_manager, "client", _Client())
    return captured


def test_build_cpu_shares_default_passed(monkeypatch, tmp_path):
    """Env не задан → дефолт 512 уходит в client.api.build как container_limits."""
    monkeypatch.delenv("DEPLOYER_BUILD_CPU_SHARES", raising=False)
    captured = _capture_build_kwargs(monkeypatch)
    docker_manager.build_image_if_needed(
        _make_zip(tmp_path / "a.zip"), image_cache_key="cpu1")
    assert captured["container_limits"] == {"cpushares": 512}


def test_build_cpu_shares_custom_value(monkeypatch, tmp_path):
    """Явное значение из env прокидывается как есть."""
    monkeypatch.setenv("DEPLOYER_BUILD_CPU_SHARES", "256")
    captured = _capture_build_kwargs(monkeypatch)
    docker_manager.build_image_if_needed(
        _make_zip(tmp_path / "b.zip"), image_cache_key="cpu2")
    assert captured["container_limits"] == {"cpushares": 256}


def test_build_cpu_shares_zero_disables(monkeypatch, tmp_path):
    """«0» → троттл выключен: container_limits НЕ передаётся вовсе."""
    monkeypatch.setenv("DEPLOYER_BUILD_CPU_SHARES", "0")
    captured = _capture_build_kwargs(monkeypatch)
    docker_manager.build_image_if_needed(
        _make_zip(tmp_path / "c.zip"), image_cache_key="cpu3")
    assert "container_limits" not in captured


def test_build_cpu_shares_empty_disables(monkeypatch, tmp_path):
    """Пустая строка → тоже выключен (осознанный opt-out через env)."""
    monkeypatch.setenv("DEPLOYER_BUILD_CPU_SHARES", "")
    captured = _capture_build_kwargs(monkeypatch)
    docker_manager.build_image_if_needed(
        _make_zip(tmp_path / "d.zip"), image_cache_key="cpu4")
    assert "container_limits" not in captured
