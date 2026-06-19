# --- ИЗМЕНЕННЫЙ ФАЙЛ: app/services/ssl_service.py ---

import asyncio
from app import config
from app import environment
from app.services import docker_manager
from app.services.ws_manager import manager


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
    ready_event = manager.register_task(task_id)
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        manager.disconnect(task_id)
        return

    # --- ИЗМЕНЕНИЕ: Получаем event loop ЗДЕСЬ, в основном потоке ---
    loop = asyncio.get_running_loop()

    try:
        action_word = "Обновление" if is_renew else "Выпуск"
        await manager.send_message(f"=== Начало процесса: {action_word} SSL для домена: {domain} ===", task_id)

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

    finally:
        await asyncio.sleep(2)
        await manager.send_message("CLOSE_CONNECTION", task_id)
        manager.disconnect(task_id)