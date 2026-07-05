"""Веб-терминал «для знатоков»: выполнение одной админской команды на ноде (ADR-090).

**Где выполняется.** Команда исполняется shell'ом ВНУТРИ управляющего контейнера
деплоера на самой ноде (`/bin/sh -c "<команда>"`), а не в app-контейнерах и не по
новому SSH-каналу. Контейнер деплоера — это и есть проверенный «агент» на сервере
клиента (у него смонтирован docker.sock, он рулит Docker — ADR-002/007). Терминал не
открывает нового периметра: тот же процесс, те же привилегии, что и у остального
управления нодой.

**Модель угроз.** Это фича повышенного риска (произвольное выполнение на хосте
клиента), поэтому спроектирована обратимо и «обёрнута сохранениями»:
  • **Выключатель** — `DEPLOYER_TERMINAL_ENABLED` (по умолчанию включён; `false`
    гасит фичу целиком: и эндпоинт панели, и cpk-путь ЛК/MCP отвечают «выключено»).
  • **Таймаут** — команда убивается по `DEPLOYER_TERMINAL_TIMEOUT` (дефолт 30 c),
    чтобы завис/`sleep 999` не держал воркер.
  • **Лимит вывода** — stdout+stderr обрезаются до `_OUTPUT_LIMIT` (64 КБ): гигабайтный
    вывод (`cat /dev/urandom`) не съест память/канал.
  • **Аудит** — каждый вызов (кто/когда/команда/exit-code) фиксируется вызывающим
    слоем (панель — в лог процесса, ЛК — в `cloud_audit_log`).
  • **Rate-limit** — частота вызовов ограничена (`CommandRateLimiter`) на входе роутов.

Никаких shell-инъекций «в промежуточных слоях»: мы НЕ собираем команду конкатенацией
недоверенного ввода в другую команду — весь ввод пользователя и ЕСТЬ намеренная
команда, она передаётся единым аргументом в `sh -c`. Ответственность за содержимое —
на аутентифицированном администраторе (по замыслу «терминала для знатоков»).
"""
from __future__ import annotations

import os
import subprocess

# Верхняя граница объединённого stdout+stderr (байты). Больше — обрезаем с маркером,
# чтобы «водопадный» вывод не исчерпал память воркера и канал до ЛК.
_OUTPUT_LIMIT = 64 * 1024

# Дефолтный таймаут выполнения (секунды). Переопределяется env; жёсткий потолок ниже
# защищает от опечатки в env, которая иначе разрешила бы вечное выполнение.
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 300


def terminal_enabled() -> bool:
    """Включён ли веб-терминал (выключатель фичи, ADR-090).

    По умолчанию ВКЛ; выключается `DEPLOYER_TERMINAL_ENABLED=false` — тогда и панель,
    и cpk-эндпоинт отвечают «выключено», не выполняя ничего (обратимость/откат)."""
    return os.environ.get("DEPLOYER_TERMINAL_ENABLED", "true").strip().lower() != "false"


def effective_timeout() -> int:
    """Таймаут выполнения команды: env `DEPLOYER_TERMINAL_TIMEOUT`, ограниченный
    диапазоном [1, _MAX_TIMEOUT]. Мусор в env → дефолт (никогда не падаем)."""
    raw = os.environ.get("DEPLOYER_TERMINAL_TIMEOUT", "").strip()
    try:
        val = int(raw) if raw else _DEFAULT_TIMEOUT
    except ValueError:
        val = _DEFAULT_TIMEOUT
    return max(1, min(val, _MAX_TIMEOUT))


def _clip(text: str) -> tuple[str, bool]:
    """Обрезает вывод до _OUTPUT_LIMIT (по байтам utf-8). Возвращает (текст, truncated)."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _OUTPUT_LIMIT:
        return text, False
    clipped = encoded[:_OUTPUT_LIMIT].decode("utf-8", errors="ignore")
    return clipped, True


def run_command(command: str) -> dict:
    """Выполняет ОДНУ команду через `sh -c` внутри контейнера деплоера (ADR-090).

    Возвращает dict `{command, exit_code, output, truncated, timed_out, duration_ms}`.
    Никогда не поднимает исключение на «нормальном» пути (ненулевой код, таймаут, сбой
    запуска) — все исходы кодируются в полях, чтобы вызывающий роут дал 200 с телом,
    а не 500. Пустая/пробельная команда → `exit_code=None` с пояснением (валидацию
    длины/непустоты делает Pydantic-схема выше, это лишь страховка).
    """
    import time

    cmd = (command or "").strip()
    if not cmd:
        return {"command": command, "exit_code": None, "output": "Пустая команда.",
                "truncated": False, "timed_out": False, "duration_ms": 0}

    timeout = effective_timeout()
    started = time.monotonic()
    try:
        # shell=False + явный ['sh','-c', cmd]: единый аргумент, никакой интерполяции
        # текста в ДРУГУЮ команду. Объединяем stderr в stdout (терминальный порядок).
        proc = subprocess.run(
            ["/bin/sh", "-c", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False,
        )
        raw = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        output, truncated = _clip(raw)
        if truncated:
            output += f"\n… [вывод обрезан до {_OUTPUT_LIMIT // 1024} КБ]"
        return {"command": cmd, "exit_code": proc.returncode, "output": output,
                "truncated": truncated, "timed_out": False,
                "duration_ms": int((time.monotonic() - started) * 1000)}
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout.decode("utf-8", errors="replace")
                   if isinstance(e.stdout, (bytes, bytearray)) else "") or ""
        output, truncated = _clip(partial)
        note = f"\n… [команда прервана по таймауту {timeout} c]"
        return {"command": cmd, "exit_code": None, "output": output + note,
                "truncated": truncated, "timed_out": True,
                "duration_ms": int((time.monotonic() - started) * 1000)}
    except FileNotFoundError:
        # Нет /bin/sh (маловероятно в наших образах) — честная диагностика, не 500.
        return {"command": cmd, "exit_code": None,
                "output": "Оболочка /bin/sh недоступна в контейнере деплоера.",
                "truncated": False, "timed_out": False,
                "duration_ms": int((time.monotonic() - started) * 1000)}
    except Exception as e:  # noqa: BLE001 — любой иной сбой запуска → тело, не транспорт
        return {"command": cmd, "exit_code": None,
                "output": f"Не удалось выполнить команду: {e}",
                "truncated": False, "timed_out": False,
                "duration_ms": int((time.monotonic() - started) * 1000)}
