# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/services/ssl_service.py ---

import asyncio
import os
import secrets
from app import config
from app import environment
from app.services import docker_manager
from app.services import nginx_manager
from app.services.ws_manager import manager

# Сколько ждать подключения WebSocket-клиента (UI стримит логи). По таймауту выпуск
# НЕ прерывается — продолжается без стрима (ADR-053). Вынесено в константу для тестов.
WS_WAIT_TIMEOUT = 10.0


def acme_preflight(domain: str) -> tuple[bool, str]:
    """Пред-проверка достижимости ACME HTTP-01 challenge ДО запуска certbot.

    Пишет одноразовый проб-файл в webroot и запрашивает его изнутри nginx-
    контейнера с заголовком `Host: <domain>` — ровно то, что сделает Let's Encrypt,
    но НЕ тратя лимит LE и не требуя внешнего DNS. Ловит главный footgun — 403 от
    catchall (nginx не отдаёт challenge) и 404 (рассинхрон webroot) с понятным
    выводом. Никогда не бросает исключение: возвращает (ok, detail).

    Граница: проверка ВНУТРЕННЯЯ (через nginx), поэтому валидирует конфиг nginx, но
    НЕ внешний DNS/файрвол. Если она прошла, а certbot всё равно упал — причина
    снаружи (DNS ещё не указывает на этот IP либо закрыт 80-й порт).
    """
    token = "deployer-preflight-" + secrets.token_hex(12)
    probe_dir = config.ACME_CHALLENGE_DIR / ".well-known" / "acme-challenge"
    probe_file = probe_dir / token
    try:
        # Webroot + подкаталоги должны быть проходимы nginx-воркером (umask-077 footgun).
        nginx_manager.ensure_acme_webroot_traversable()
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_file.write_text(token, encoding="utf-8")
        os.chmod(probe_file, 0o644)  # nginx-воркер (uid != root) должен прочитать файл
        url = f"http://127.0.0.1/.well-known/acme-challenge/{token}"
        # busybox wget есть в nginx:alpine; --header задаёт виртуальный хост.
        code, out = docker_manager.exec_in_container(
            config.NGINX_CONTAINER_NAME,
            ["wget", "-q", "-O", "-", "--header", f"Host: {domain}", url],
        )
        if code == 0 and token in out:
            return True, "nginx отдаёт challenge (внутренняя проверка пройдена)"
        return False, f"nginx не отдал challenge (код {code}): {out.strip()[:200] or 'пустой ответ (вероятно 403/404)'}"
    except Exception as e:  # noqa: BLE001 — диагностика не должна ронять выпуск
        return False, f"проверка не выполнена ({e!r}) — продолжаю выпуск"
    finally:
        try:
            probe_file.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def run_command_sync_streamed(container_name: str, cmd: list[str], task_id: str,
                              loop: asyncio.AbstractEventLoop):
    """
    СИНХРОННО выполняет команду ВНУТРИ контейнера (docker-py exec) и стримит её
    вывод в WebSocket. Выполняется в отдельном потоке, требует передачи event loop.
    При ненулевом коде возврата exec_stream_in_container поднимает RuntimeError.
    """
    try:
        for line in docker_manager.exec_stream_in_container(container_name, cmd):
            # Отправляем сообщение в основной поток через переданный loop
            future = asyncio.run_coroutine_threadsafe(
                manager.send_message(line.strip(), task_id),
                loop
            )
            future.result()  # Ждем, пока сообщение будет отправлено

    except Exception as e:
        # Ловим любую ошибку внутри потока и отправляем ее в WebSocket
        error_message = f"--- !!! ОШИБКА ВНУТРИ ПОТОКА: {repr(e)} ---"
        asyncio.run_coroutine_threadsafe(manager.send_message(error_message, task_id), loop).result()
        # Также пробрасываем ошибку дальше, чтобы основной хендлер ее поймал
        raise


async def perform_ssl_issuance(task_id: str, domain: str, is_renew: bool = False):
    """
    Асинхронно получает или обновляет SSL-сертификат, запуская Certbot ВНУТРИ
    certbot-контейнера через docker-py exec в отдельном потоке (вывод стримится
    в WebSocket).
    """
    # Ждём подключения WebSocket-клиента — чтобы UI получил логи С САМОГО НАЧАЛА.
    # 🔴 КЛЮЧЕВОЕ (ADR-053): по таймауту НЕ выходим, а ПРОДОЛЖАЕМ выпуск. Раньше здесь
    # был `return` → если WS не подключился за 10 c, certbot НЕ запускался ВООБЩЕ. Это
    # ломало любой программный вызов без UI: авто-SSL из ЛК/реконсайлера (он дёргает
    # /api/ssl/issue, но WebSocket не открывает) всегда «падал» пустышкой, хотя ручной
    # выпуск из панели (UI открывает WS) работал идеально. Выпуск НЕ должен зависеть от
    # наличия слушателя логов: `manager.send_message` без подключения — безопасный no-op.
    ready_event = manager.register_task(task_id)
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=WS_WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        # Нет WS-клиента (вызов из реконсайлера/ЛК или CLI) — стримить логи некому,
        # но сам выпуск SSL запускаем. Задачу НЕ снимаем: WS может подключиться позже
        # (тогда получит остаток логов); финальная очистка — в `finally`.
        pass

    # --- ИЗМЕНЕНИЕ: Получаем event loop ЗДЕСЬ, в основном потоке ---
    loop = asyncio.get_running_loop()

    preflight_ok = False
    try:
        action_word = "Обновление" if is_renew else "Выпуск"
        await manager.send_message(f"=== Начало процесса: {action_word} SSL для домена: {domain} ===", task_id)

        # [0/2] Самоизлечение: гарантируем, что nginx отдаёт ACME-challenge для
        # ЛЮБОГО домена (устаревший/битый catchall — частая причина 403). Делаем
        # ДО certbot. panel/app-конфиги не трогаются.
        await manager.send_message("\n[0/2] Подготовка ACME-челленджа (обновляю catchall, перезагружаю Nginx)...", task_id)
        try:
            await loop.run_in_executor(None, nginx_manager.ensure_acme_challenge_ready)
        except Exception as e:
            await manager.send_message(f" -> ПРЕДУПРЕЖДЕНИЕ: не удалось обновить catchall: {repr(e)}", task_id)

        # Пред-проверка пути challenge изнутри (ловит 403/404 ДО траты попытки LE).
        preflight_ok, detail = await loop.run_in_executor(None, acme_preflight, domain)
        if preflight_ok:
            await manager.send_message(f" -> Пред-проверка ACME: OK — {detail}", task_id)
        else:
            await manager.send_message(
                f" -> Пред-проверка ACME: ВНИМАНИЕ — {detail}\n"
                f"    (nginx не отдаёт challenge локально; certbot, скорее всего, тоже получит 403/404)",
                task_id)

        # Пути внутри контейнеров и контактный email берём из слоя абстракции
        # окружения (email — из env DEPLOYER_ACME_EMAIL, без хардкода домена).
        webroot_path = environment.ACME_WEBROOT
        certs_path = environment.ACME_CERTS_DIR

        certbot_command = config.CERTBOT_CMD_BASE + [
            "certonly", "--webroot", "-w", webroot_path, "-d", domain,
            *environment.acme_email_args(), "--agree-tos", "--non-interactive",
            "--rsa-key-size", "4096", "--config-dir", certs_path,
            "--work-dir", f"{certs_path}/lib", "--logs-dir", f"{certs_path}/logs",
        ]
        if is_renew: certbot_command.append("--force-renewal")

        await manager.send_message(f"\n[1/2] Запуск Certbot... Команда: {' '.join(certbot_command)}", task_id)

        # Certbot выполняется ВНУТРИ certbot-контейнера через docker-py exec
        # (стриминг вывода в WebSocket). 'loop' передаём в executor.
        await loop.run_in_executor(
            None,
            run_command_sync_streamed,
            config.CERTBOT_CONTAINER_NAME,
            certbot_command,
            task_id,
            loop  # <--- Передаем event loop в функцию
        )

        await manager.send_message("\n[2/2] Certbot успешно завершил работу.", task_id)

        await manager.send_message("\nПерезагрузка Nginx...", task_id)
        reload_code, reload_out = await loop.run_in_executor(
            None,
            lambda: docker_manager.exec_in_container(
                config.NGINX_CONTAINER_NAME, config.NGINX_RELOAD_CMD
            )
        )

        if reload_code == 0:
            await manager.send_message(" -> Nginx успешно перезагружен.", task_id)
        else:
            await manager.send_message(f"[ОШИБКА] Не удалось перезагрузить Nginx: {reload_out.strip()}",
                                       task_id)

        await manager.send_message("\n=== ПРОЦЕСС УСПЕШНО ЗАВЕРШЕН! ===", task_id)

    except Exception as e:
        error_message = f"\n--- !!! КРИТИЧЕСКАЯ ОШИБКА: {repr(e)} ---"
        await manager.send_message(error_message, task_id)
        # Дизамбигуация для пользователя: внутренняя проверка отделяет «битый nginx»
        # от «не настроен DNS снаружи».
        if preflight_ok:
            await manager.send_message(
                "ПОДСКАЗКА: nginx локально отдаёт ACME-challenge (пред-проверка прошла), "
                "значит проблема СНАРУЖИ: A-запись домена ещё не указывает на этот сервер, "
                "DNS не распространился, либо закрыт 80-й порт. Проверь A-запись и повтори "
                "через несколько минут.", task_id)
        else:
            await manager.send_message(
                "ПОДСКАЗКА: nginx локально НЕ отдал ACME-challenge — проблема в конфигурации "
                "(битый/устаревший nginx-конфиг). Открой «Настройки → Панель», сохрани домен "
                "заново (это перегенерирует конфиг) и повтори выпуск.", task_id)

    finally:
        await asyncio.sleep(2)
        await manager.send_message("CLOSE_CONNECTION", task_id)
        manager.disconnect(task_id)