# --- app/services/docker_manager.py ---

import docker
import zipfile
import tempfile
import hashlib
import socket
from pathlib import Path

from app import artifact_utils
from app.environment import get_docker_client
from app.services.nginx_service import DEPLOYER_NETWORK, ensure_network

# Единый docker-клиент окружения (см. app/environment.py).
client = get_docker_client()


def generate_dockerfile(base_image="python:3.12-slim", run_command=None, internal_port=80):
    """Генерирует Dockerfile под расширенный режим (Идея 2а, ADR-021).

    - База `python:*` (по умолчанию) → ставим uvicorn/fastapi + requirements.txt
      (прежнее удобство для питон-проектов). Иная база (node/go/…) → без pip:
      только COPY + CMD (зависимости — на совести базы/команды/своего Dockerfile).
    - `run_command` пусто → дефолтный uvicorn на `internal_port`; задано → CMD как
      есть (shell-форма: 'python bot.py', 'gunicorn app:app -b 0.0.0.0:80').
    - `internal_port` → EXPOSE и порт дефолтного uvicorn (убирает хардкод 80).
    """
    is_python = "python" in (base_image or "").lower()
    lines = [f"FROM {base_image}", "WORKDIR /app", "COPY . ."]
    if is_python:
        # Гарантируем uvicorn/fastapi (используются дефолтным CMD) + requirements.
        lines.append("RUN pip install --no-cache-dir uvicorn fastapi")
        lines.append("RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi")
    lines.append(f"EXPOSE {internal_port}")
    if run_command:
        lines.append(f"CMD {run_command}")
    else:
        lines.append(f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{internal_port}"]')
    return "\n".join(lines) + "\n"


# Версия стратегии сборки. Входит в ключ кэша образа, поэтому при изменении логики
# сборки (generate_dockerfile / правила выбора Dockerfile) — бампнуть, чтобы кэш
# инвалидировался и образы пересобрались. "v3" = база python:3.12 (3.9 ломала
# современные requirements, напр. anyio>=4.13 требует py>=3.10). "v4" = расширенный
# режим (база/команда/порт влияют на Dockerfile и входят в ключ кэша).
BUILD_STRATEGY = "v4-advanced"

# Репозиторий тегов образов, собираемых деплоером (content-addressed кэш).
IMAGE_REPO = "deployer-cache"

# Logging для app-контейнеров (ADR-076): json-file поддерживает чтение (`docker logs`
# и наша диагностика работают на любом хосте) + ротация защищает диск ноды. Задаём
# ЯВНО на каждом контейнере — не полагаемся на дефолтный драйвер хоста, который может
# быть syslog/journald/none без поддержки чтения.
APP_LOG_CONFIG = docker.types.LogConfig(
    type="json-file", config={"max-size": "10m", "max-file": "3"})


def compute_image_tag(base_key: str, build_config: dict = None) -> str:
    """Детерминированный тег образа по (контент артефакта + стратегия + конфиг).

    Единый источник правды формулы тега: используется и при сборке
    (build_image_if_needed), и при уборке неиспользуемых образов
    (prune_deployer_images) — чтобы «нужный» набор тегов точно совпадал с тем, что
    реально собирается. См. ADR-021 (что входит в ключ) и ADR-025 (prune).
    """
    cfg = build_config or {}
    cfg_sig = f"{cfg.get('base_image')}|{cfg.get('run_command')}|{cfg.get('internal_port')}"
    cache_key = hashlib.sha256(f"{base_key}:{BUILD_STRATEGY}:{cfg_sig}".encode()).hexdigest()
    return f"{IMAGE_REPO}:{cache_key[:32]}"


def build_image_if_needed(zip_path: Path, image_cache_key: str = None, build_config: dict = None,
                          on_line=None) -> str:
    """Собирает образ из ZIP с КЭШЕМ по контенту (идемпотентность).

    Образ адресуется по содержимому артефакта + версии стратегии сборки +
    параметрам расширенного режима (`deployer-cache:<hash>`), а не по имени деплоя.
    Если образ для этого контента уже собран — пропускаем сборку (раньше
    `docker build` гонялся на каждый reconcile/реплику — лишняя работа).

    Если в архиве есть СВОЙ `Dockerfile` — собираем по нему (поддержка не-Python
    приложений, реальных GitHub-проектов); иначе генерируем по умолчанию с учётом
    `build_config` (base_image/run_command/internal_port — Идея 2а, ADR-021).

    `on_line` (опц.) — колбэк на каждую строку лога сборки: при его передаче сборка
    идёт через low-level API со стримингом (живые WS-логи, ADR-023); иначе обычный
    блокирующий build. Возвращает тег готового образа. При ошибке сборки поднимает
    RuntimeError с логом (оркестратор сохранит его на Deployment для UI-диагностики).
    """
    cfg = build_config or {}
    # Content-addressed ключ: (zip_hash | хэш файла) + версия стратегии + параметры
    # расширенного режима (влияют на генерируемый Dockerfile → должны инвалидировать
    # кэш при изменении). Для своего Dockerfile из репо параметры в ключе безвредны
    # (их смена вызовет редкую лишнюю пересборку — приемлемо).
    base_key = image_cache_key or hashlib.sha256(zip_path.read_bytes()).hexdigest()
    image_tag = compute_image_tag(base_key, cfg)

    try:
        client.images.get(image_tag)
        print(f"INFO: Image {image_tag} already built — skipping build (idempotent).")
        return image_tag
    except docker.errors.ImageNotFound:
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        build_context = Path(tmpdir)

        # 1. Распаковываем архив (безопасно: анти zip-slip, V-03). Вредоносная запись
        # с `../` могла бы писать вне build_context — в ФС контейнера деплоера, где
        # смонтированы docker.sock/data/ssl_certs. safe_extract_zip отклоняет такой архив.
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            artifact_utils.safe_extract_zip(zip_ref, build_context)

        # 2. Dockerfile: уважаем свой из репо, иначе генерируем питоновский дефолт.
        dockerfile = build_context / "Dockerfile"
        if dockerfile.exists():
            print("INFO: Using Dockerfile from repository/archive.")
        else:
            dockerfile.write_text(generate_dockerfile(
                base_image=cfg.get("base_image") or "python:3.12-slim",
                run_command=cfg.get("run_command") or None,
                internal_port=cfg.get("internal_port") or 80,
            ))
            print("INFO: No Dockerfile in archive — using generated default (advanced-mode aware).")

        # 3. Билдим образ. forcerm=True — удалять промежуточные контейнеры ДАЖЕ при
        # неудачной сборке: иначе легаси-билдер оставляет контейнер упавшего шага,
        # и при повторных попытках они копятся («флуд» остановленных контейнеров).
        print(f"INFO: Building image {image_tag}...")
        if on_line is not None:
            _build_image_streaming(build_context, image_tag, on_line)
        else:
            try:
                client.images.build(path=str(build_context), tag=image_tag, rm=True, forcerm=True)
            except docker.errors.BuildError as e:
                print(f"ERROR: Docker build failed: {e}")
                lines = []
                for chunk in e.build_log:
                    if 'stream' in chunk:
                        s = chunk['stream'].rstrip()
                        if s:
                            print(s)
                            lines.append(s)
                # Поднимаем ошибку с логом сборки, чтобы оркестратор сохранил её на
                # Deployment и UI показал причину, почему сервис «не запускается».
                detail = "\n".join(lines[-60:]) if lines else str(e)
                raise RuntimeError(f"Ошибка сборки образа:\n{detail}")

    return image_tag


def _build_image_streaming(build_context: Path, image_tag: str, on_line):
    """Сборка через low-level Docker API со стримингом лога построчно в `on_line`
    (живые WS-логи, ADR-023). low-level build НЕ кидает BuildError — ошибку отдаёт
    как chunk {'error': ...}; ловим её и поднимаем RuntimeError с хвостом лога."""
    log_lines: list[str] = []
    error_text = None
    for chunk in client.api.build(path=str(build_context), tag=image_tag, rm=True, forcerm=True, decode=True):
        if 'stream' in chunk:
            text = chunk['stream'].rstrip()
            if text:
                print(text)
                log_lines.append(text)
                on_line(text)
        elif 'error' in chunk:
            error_text = chunk['error'].rstrip()
            print(f"ERROR: Docker build failed: {error_text}")
            log_lines.append(error_text)
            on_line(error_text)
    if error_text is not None:
        detail = "\n".join(log_lines[-60:])
        raise RuntimeError(f"Ошибка сборки образа:\n{detail}")


def is_app_responding(container_name: str, port: int = 80, timeout: float = 1.0) -> bool:
    """Health-gate: проверяет, что приложение РЕАЛЬНО слушает порт (а не просто
    контейнер 'running'). TCP-connect к контейнеру по имени в сети deployer-net.

    TCP-коннект, а не HTTP-2xx: приложение может легитимно отвечать 404/401 на `/`,
    но если порт открыт — процесс жив и принимает соединения. Используется
    оркестратором перед пометкой реплики 'online'.
    """
    try:
        with socket.create_connection((container_name, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_exposed_ports(exposed: dict | None) -> int | None:
    """Из `Config.ExposedPorts` ({'3000/tcp': {}, ...}) → наименьший TCP-порт или None.

    TCP приоритетно (веб-сервисы); если только udp — берём его. Наименьший —
    детерминированный выбор при нескольких EXPOSE (обычно основной порт ниже)."""
    if not exposed:
        return None
    tcp, other = [], []
    for spec in exposed:
        try:
            num_s, _, proto = str(spec).partition("/")
            num = int(num_s)
        except (ValueError, AttributeError):
            continue
        (tcp if proto in ("tcp", "") else other).append(num)
    ports = sorted(tcp) or sorted(other)
    return ports[0] if ports else None


def container_exposed_port(container_id: str) -> int | None:
    """Порт, объявленный `EXPOSE` в образе контейнера (авто-подхват порта приложения).

    Читаем `Config.ExposedPorts` — это то, что разработчик написал в своём Dockerfile
    (`EXPOSE 3000`). Best-effort: любой сбой инспекции → None (детект необязателен)."""
    try:
        container = client.containers.get(container_id)
        cfg = (container.attrs.get("Config", {}) or {})
        return _parse_exposed_ports(cfg.get("ExposedPorts"))
    except Exception:  # noqa: BLE001 — детект не критичен, не роняем деплой
        return None


def deploy_service(zip_path: Path, deployment_name: str, port: int, image_cache_key: str = None,
                   build_config: dict = None, env_vars: dict = None):
    """
    Основная функция деплоя: строит образ (с кэшем) и запускает контейнер.
    `build_config` — параметры расширенного режима сборки (база/команда/порт);
    `env_vars` — env-переменные рантайма, инжектятся в контейнер (Идея 2а, ADR-021).
    Возвращает (container_id, container_name) или (None, None) в случае ошибки.
    """
    container_name = f"deployer-{deployment_name}"

    # Идемпотентная сборка: образ переиспользуется между репликами/редеплоями.
    image_tag = build_image_if_needed(zip_path, image_cache_key, build_config)

    # Останавливаем и удаляем старый контейнер с таким же именем, если он есть
    try:
        old_container = client.containers.get(container_name)
        print(f"INFO: Stopping and removing old container {container_name}...")
        old_container.stop()
        old_container.remove(v=True)  # v=True удаляет анонимные тома
    except docker.errors.NotFound:
        pass  # Контейнера не было, это нормально

    # Запускаем новый контейнер в общей docker-сети.
    # Host-порт НЕ публикуем: деплоер и nginx обращаются к контейнеру по имени
    # на внутренний порт 80 внутри сети deployer-net. Это убирает исчерпание
    # host-портов и делает модель кроссплатформенной (Linux/Windows).
    ensure_network()
    print(f"INFO: Running new container {container_name} in network {DEPLOYER_NETWORK} (logical port {port})...")
    try:
        container = client.containers.run(
            image=image_tag,
            name=container_name,
            network=DEPLOYER_NETWORK,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            environment=env_vars or None,  # env-переменные рантайма (Идея 2а)
            # Явный json-file c ротацией на КАЖДОМ app-контейнере (ADR-076):
            # 1) `docker logs`/наша диагностика читаются ВСЕГДА, даже если у хоста
            #    дефолтный logging-драйвер без чтения (была «configured logging driver
            #    does not support reading» — панель не показывала логи упавшего сервиса);
            # 2) ротация (10 МБ×3) — логи приложения не заполняют диск ноды.
            log_config=APP_LOG_CONFIG,
            labels={"manager": "cloud-deployer"}  # Метка для легкого поиска
        )
        print(f"SUCCESS: Container {container.short_id} started.")
        return container.id, container_name
    except Exception as e:
        print(f"ERROR: Failed to run container: {e}")
        return None, None


def exec_in_container(container_name: str, cmd, user: str = "") -> tuple[int, str]:
    """Выполняет команду ВНУТРИ существующего системного контейнера через
    docker-py (без docker-cli в образе деплоера). Возвращает (exit_code, output).

    Заменяет прежние subprocess-вызовы docker-cli (nginx reload/test, генерация
    self-signed SSL в certbot-контейнере).
    """
    container = client.containers.get(container_name)
    result = container.exec_run(cmd, user=user)
    output = result.output.decode("utf-8", errors="replace") if result.output else ""
    return result.exit_code, output


def exec_stream_in_container(container_name: str, cmd, user: str = ""):
    """Генератор: построчно отдаёт объединённый stdout/stderr команды, выполняемой
    ВНУТРИ контейнера (docker-py low-level API — стрим + код возврата, без
    docker-cli). По завершении при ненулевом коде поднимает RuntimeError.

    Используется для стриминга вывода Certbot в WebSocket в реальном времени.
    """
    api = client.api
    exec_id = api.exec_create(container_name, cmd, user=user, tty=False)["Id"]
    pending = b""
    for chunk in api.exec_start(exec_id, stream=True):
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")
    if pending:
        yield pending.decode("utf-8", errors="replace")

    exit_code = api.exec_inspect(exec_id).get("ExitCode")
    if exit_code not in (0, None):
        raise RuntimeError(
            f"Команда {cmd} в контейнере '{container_name}' завершилась с кодом {exit_code}"
        )


def remove_service_container(container_name: str):
    """Останавливает и удаляет контейнер сервиса."""
    try:
        container = client.containers.get(container_name)
        print(f"INFO: Stopping container {container_name}...")
        container.stop()
        print(f"INFO: Removing container {container_name}...")
        container.remove(v=True)
        print(f"SUCCESS: Container {container_name} removed.")
        return True
    except docker.errors.NotFound:
        print(f"WARN: Container {container_name} not found, nothing to remove.")
        return True  # Считаем успехом, если его и так нет
    except Exception as e:
        print(f"ERROR: Failed to remove container {container_name}: {e}")
        return False


def cleanup_orphan_containers(known_container_names) -> int:
    """
    Удаляет контейнеры с меткой manager=cloud-deployer, которых нет среди
    known_container_names (т.е. не отслеживаемых в БД как Instance).

    Защищает от «сирот» — контейнеров, оставшихся от прошлых запусков/сбоев,
    которые иначе бесконечно крутятся с restart_policy=unless-stopped.
    Возвращает количество удалённых контейнеров.
    """
    known = set(known_container_names)
    removed = 0
    try:
        containers = client.containers.list(all=True, filters={"label": "manager=cloud-deployer"})
    except Exception as e:
        print(f"ERROR: Could not list managed containers for orphan cleanup: {e}")
        return 0

    for container in containers:
        if container.name in known:
            continue
        try:
            print(f"INFO: Removing orphan container {container.name} (status={container.status})...")
            container.remove(force=True)  # force останавливает и удаляет крутящийся контейнер
            removed += 1
        except Exception as e:
            print(f"ERROR: Failed to remove orphan container {container.name}: {e}")

    if removed:
        print(f"SUCCESS: Removed {removed} orphan container(s).")
    return removed


def prune_deployer_images(wanted_tags) -> int:
    """Удаляет образы `deployer-cache:*`, которые больше не нужны.

    «Нужные» (`wanted_tags`) — теги текущих версий ВСЕХ деплоев (считаются вызывающим
    через `compute_image_tag`). Остальные `deployer-cache`-образы — мусор от старых
    версий/конфигов (редеплой/смена конфига оставляет прежний образ висеть) и
    занимают диск. Без уборки за недели работы диск боевого сервера заполняется.

    Безопасно: образ, занятый работающим контейнером, Docker удалить не даст
    (conflict) — такой пропускаем. Если случайно удалить нужный образ, следующий
    reconcile пересоберёт его (build идемпотентен) — потеря лишь в одной пересборке.
    Возвращает число удалённых тегов. ADR-025.
    """
    wanted = set(wanted_tags or [])
    removed = 0
    try:
        images = client.images.list(name=IMAGE_REPO)
    except Exception as e:
        print(f"ERROR: image prune: could not list images: {e}")
        return 0

    for image in images:
        ours = [t for t in (image.tags or []) if t.startswith(f"{IMAGE_REPO}:")]
        if not ours:
            continue
        # Если хотя бы один тег образа ещё нужен — образ целиком оставляем.
        if any(t in wanted for t in ours):
            continue
        for tag in ours:
            try:
                client.images.remove(tag)
                removed += 1
            except docker.errors.APIError:
                # Образ занят контейнером (conflict) или уже удалён — пропускаем.
                pass
            except Exception as e:
                print(f"ERROR: image prune: could not remove {tag}: {e}")

    if removed:
        print(f"INFO: image prune: removed {removed} unused {IMAGE_REPO} image tag(s).")
    return removed


def prune_dangling_images(prune_build_cache: bool = True) -> dict:
    """Удаляет dangling-образы (и, опц., build-кэш) — защита диска ноды (ADR-078).

    Многостадийные сборки (`FROM … AS deps/build/runner`, типично для Node/Next.js)
    оставляют промежуточные СТАДИИ как dangling-образы. `prune_deployer_images` чистит
    только теги `deployer-cache:*`, а dangling — нет. За серию пересборок (напр. смена
    конфига/порта → build_first_swap) они забивают диск в ноль → сборки падают, Docker
    ломается, нода в thrash. Живой инцидент 2026-07-04: 6.7 ГБ dangling → диск 100%.

    Безопасно: `filters={'dangling': True}` не трогает теговые/используемые образы.
    Best-effort — возвращает {'images_deleted', 'space_reclaimed'} (0 при сбое)."""
    out = {"images_deleted": 0, "space_reclaimed": 0}
    try:
        res = client.images.prune(filters={"dangling": True})
        out["images_deleted"] = len(res.get("ImagesDeleted") or [])
        out["space_reclaimed"] = res.get("SpaceReclaimed", 0) or 0
    except Exception as e:
        print(f"ERROR: dangling prune: {e}")
    if prune_build_cache:
        try:
            bc = client.api.prune_builds()  # build cache (низкоуровневый API)
            out["space_reclaimed"] += bc.get("SpaceReclaimed", 0) or 0
        except Exception as e:  # noqa: BLE001 — не критично
            print(f"ERROR: build cache prune: {e}")
    if out["images_deleted"] or out["space_reclaimed"]:
        mb = out["space_reclaimed"] / (1024 * 1024)
        print(f"INFO: dangling prune: удалено {out['images_deleted']} образов, "
              f"освобождено ~{mb:.0f} МБ.")
    return out


def get_running_deployment_containers():
    """Возвращает список ID контейнеров, управляемых нашим деплоером."""
    try:
        containers = client.containers.list(filters={"label": "manager=cloud-deployer"})
        return [c.id for c in containers]
    except Exception as e:
        print(f"ERROR: Could not get containers from Docker: {e}")
        return []


def get_container_by_name(container_name: str):
    """Находит контейнер по имени."""
    try:
        return client.containers.get(container_name)
    except docker.errors.NotFound:
        return None


def start_container(container):
    """Запускает контейнер."""
    container.start()


def stop_container(container):
    """Останавливает контейнер."""
    container.stop()


def restart_container(container):
    """Перезапускает контейнер."""
    container.restart()


# Сообщение Docker при попытке прочитать логи, когда на хосте выбран
# logging-драйвер без поддержки чтения (syslog/journald-конфиг/gelf/fluentd/none и т.п.).
# Это НЕ ошибка приложения — это конфигурация хоста (см. docs/21_HOST_OPS.md).
_LOG_DRIVER_UNSUPPORTED = "configured logging driver does not support reading"

LOG_DRIVER_HELP = (
    "⚠️ Логи контейнера недоступны для чтения: на этом сервере Docker настроен с "
    "logging-драйвером, который не поддерживает `docker logs` (нужен json-file или "
    "local). Это конфигурация ХОСТА, а не ошибка приложения. Как починить: задать в "
    "/etc/docker/daemon.json  \"log-driver\": \"json-file\" (с ротацией max-size), "
    "перезапустить Docker и пересоздать контейнер. Подробности — docs/21_HOST_OPS.md."
)


def log_driver_unsupported(exc: Exception) -> bool:
    """True, если ошибка — это «logging-драйвер хоста не поддерживает чтение логов»."""
    return _LOG_DRIVER_UNSUPPORTED in str(exc)


def get_container_logs(container, tail=100) -> str:
    """Последние N строк логов контейнера — с человекочитаемой классификацией сбоя.

    Если хост настроен с logging-драйвером без чтения (`docker logs` не работает),
    возвращаем понятное объяснение вместо сырого `500 Server Error…`, чтобы панель
    показывала ПРИЧИНУ, а не «Could not retrieve logs» (запрос диагностируемости)."""
    try:
        return container.logs(tail=tail).decode('utf-8', errors='ignore')
    except Exception as e:
        if log_driver_unsupported(e):
            return LOG_DRIVER_HELP
        return f"Не удалось получить логи контейнера: {e}"


def get_container_diagnostics(container) -> dict:
    """Разбор состояния мёртвого/зависшего контейнера для панели (диагностируемость).

    Достаёт из `State`/`HostConfig` человекочитаемые факты, которых раньше UI не видел:
    код выхода, OOM-kill (нехватка памяти), текст ошибки рантайма Docker, тип
    logging-драйвера и признак «logs недоступны». Best-effort — любой сбой не роняет
    вызывающего (возвращаем пустые поля)."""
    out = {"exit_code": None, "oom_killed": False, "state_error": None,
           "log_driver": None, "logs_readable": True, "restarting": False}
    try:
        container.reload()
        state = container.attrs.get("State", {}) or {}
        out["exit_code"] = state.get("ExitCode")
        out["oom_killed"] = bool(state.get("OOMKilled"))
        out["state_error"] = (state.get("Error") or "").strip() or None
        out["restarting"] = bool(state.get("Restarting"))
        log_cfg = (container.attrs.get("HostConfig", {}) or {}).get("LogConfig", {}) or {}
        driver = log_cfg.get("Type")
        out["log_driver"] = driver
        # json-file/local поддерживают чтение; прочие (syslog/journald-cfg/gelf/…) — нет.
        out["logs_readable"] = driver in (None, "json-file", "local", "")
    except Exception:  # noqa: BLE001 — диагностика не должна падать
        pass
    return out


def get_container_stats(container) -> dict:
    """Получает текущую статистику использования ресурсов контейнера.

    Ключи cpu_percent/memory_usage_mb/memory_limit_mb сохранены для обратной
    совместимости (их читает фронт в деталях сервиса); добавлены сетевые
    счётчики net_rx_mb/net_tx_mb (суммарно по всем интерфейсам).
    """
    try:
        stats = container.stats(stream=False)
        cpu_stats = stats['cpu_stats']
        cpu_delta = cpu_stats['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
        system_cpu_delta = cpu_stats['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
        # Число CPU: online_cpus (cgroup v2), иначе длина percpu_usage (cgroup v1).
        # ВАЖНО: фолбэк считаем ЛЕНИВО — dict.get(k, default) вычисляет default всегда,
        # а 'percpu_usage' в cgroup v2 отсутствует → раньше падало с KeyError.
        number_cpus = cpu_stats.get('online_cpus')
        if not number_cpus:
            number_cpus = len(cpu_stats.get('cpu_usage', {}).get('percpu_usage') or []) or 1
        cpu_percent = (cpu_delta / system_cpu_delta) * number_cpus * 100.0 if system_cpu_delta > 0 else 0
        memory_usage_bytes = stats['memory_stats']['usage']
        memory_limit_bytes = stats['memory_stats']['limit']
        memory_usage_mb = round(memory_usage_bytes / (1024 * 1024), 2)
        memory_limit_mb = round(memory_limit_bytes / (1024 * 1024), 2)
        rx_bytes = tx_bytes = 0
        for iface in (stats.get('networks') or {}).values():
            rx_bytes += iface.get('rx_bytes', 0)
            tx_bytes += iface.get('tx_bytes', 0)
        return {
            "cpu_percent": round(cpu_percent, 2),
            "memory_usage_mb": memory_usage_mb,
            "memory_limit_mb": memory_limit_mb,
            "net_rx_mb": round(rx_bytes / (1024 * 1024), 2),
            "net_tx_mb": round(tx_bytes / (1024 * 1024), 2),
        }
    except Exception as e:
        print(f"ERROR: Could not get stats for container {container.name}: {e}")
        return {"cpu_percent": 0, "memory_usage_mb": 0, "memory_limit_mb": 0, "net_rx_mb": 0, "net_tx_mb": 0}


def get_system_metrics() -> dict:
    """Сводные системные метрики для дашборда — только через Docker API
    (без новых зависимостей, см. ADR-011).

    Состоит из трёх блоков; каждый изолирован try/except, чтобы частичный сбой
    (напр. медленный/недоступный df) не ронял весь ответ:
    - host: факты хоста из client.info() (всего CPU/RAM, счётчики контейнеров/образов);
    - disk: размеры из client.df() (образы/контейнеры/тома/build-cache);
    - load: живая нагрузка — агрегат stats по managed-контейнерам
      (label manager=cloud-deployer): суммарные CPU%/RAM/сеть.
    """
    host = {"ncpu": None, "mem_total_mb": None, "containers": None,
            "containers_running": None, "containers_stopped": None,
            "images": None, "server_version": None, "operating_system": None}
    try:
        info = client.info()
        mem_total = info.get("MemTotal")
        host.update({
            "ncpu": info.get("NCPU"),
            "mem_total_mb": round(mem_total / (1024 * 1024)) if mem_total else None,
            "containers": info.get("Containers"),
            "containers_running": info.get("ContainersRunning"),
            "containers_stopped": info.get("ContainersStopped"),
            "images": info.get("Images"),
            "server_version": info.get("ServerVersion"),
            "operating_system": info.get("OperatingSystem"),
        })
    except Exception as e:
        print(f"WARNING: get_system_metrics: client.info() failed: {e}")

    disk = {"images_mb": None, "containers_mb": None, "volumes_mb": None,
            "build_cache_mb": None}
    try:
        df = client.df()
        to_mb = lambda b: round((b or 0) / (1024 * 1024), 1)
        disk["images_mb"] = to_mb(df.get("LayersSize"))
        disk["containers_mb"] = to_mb(sum(c.get("SizeRw", 0) or 0 for c in (df.get("Containers") or [])))
        disk["volumes_mb"] = to_mb(sum((v.get("UsageData") or {}).get("Size", 0) or 0 for v in (df.get("Volumes") or [])))
        disk["build_cache_mb"] = to_mb(sum(bc.get("Size", 0) or 0 for bc in (df.get("BuildCache") or [])))
    except Exception as e:
        print(f"WARNING: get_system_metrics: client.df() failed: {e}")

    load = {"cpu_percent": 0.0, "memory_usage_mb": 0.0,
            "net_rx_mb": 0.0, "net_tx_mb": 0.0, "managed_running": 0}
    try:
        managed = client.containers.list(filters={"label": "manager=cloud-deployer"})
        for container in managed:
            s = get_container_stats(container)
            load["cpu_percent"] += s.get("cpu_percent", 0) or 0
            load["memory_usage_mb"] += s.get("memory_usage_mb", 0) or 0
            load["net_rx_mb"] += s.get("net_rx_mb", 0) or 0
            load["net_tx_mb"] += s.get("net_tx_mb", 0) or 0
        load["managed_running"] = len(managed)
        load["cpu_percent"] = round(load["cpu_percent"], 2)
        load["memory_usage_mb"] = round(load["memory_usage_mb"], 2)
        load["net_rx_mb"] = round(load["net_rx_mb"], 2)
        load["net_tx_mb"] = round(load["net_tx_mb"], 2)
    except Exception as e:
        print(f"WARNING: get_system_metrics: managed-container load failed: {e}")

    return {"host": host, "disk": disk, "load": load}