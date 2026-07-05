"""In-memory rate-limit для входа в панель (анти-брутфорс).

Без внешних зависимостей и без хранилища: состояние — в процессе деплоера
(единственный uvicorn-процесс, как round-robin счётчик в proxy.py). bcrypt и так
тормозит перебор, но lockout/throttle не было — для панели, торчащей в интернет,
это реальный риск. Лимит по двум ключам сразу: по IP клиента и по имени пользователя
(одна учётка admin → перебор с ротацией IP ловится по user-ключу).
"""
import threading
import time


class LoginRateLimiter:
    """Скользящее окно: max_fails неудач за window секунд → блок до истечения окна.

    Успешный вход сбрасывает счётчик. Состояние чистится лениво (старые ключи
    удаляются при обращении), память не растёт неограниченно.
    """

    def __init__(self, max_fails: int = 10, window: int = 300):
        self.max_fails = max_fails
        self.window = window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, keys) -> int:
        """Сколько секунд ждать, если какой-то ключ заблокирован (0 — можно входить)."""
        now = time.time()
        wait = 0
        with self._lock:
            for key in keys:
                ts = [t for t in self._fails.get(key, []) if t > now - self.window]
                if ts:
                    self._fails[key] = ts
                else:
                    self._fails.pop(key, None)
                if len(ts) >= self.max_fails:
                    # Блок снимется, когда самая ранняя из последних max_fails неудач
                    # выйдет за окно.
                    oldest_relevant = ts[-self.max_fails]
                    wait = max(wait, int(oldest_relevant + self.window - now) + 1)
        return wait

    def record_failure(self, keys) -> None:
        """Фиксирует неудачную попытку входа по всем ключам."""
        now = time.time()
        with self._lock:
            for key in keys:
                self._fails.setdefault(key, []).append(now)

    def reset(self, keys) -> None:
        """Сбрасывает счётчик (вызывается при успешном входе)."""
        with self._lock:
            for key in keys:
                self._fails.pop(key, None)

    def clear(self) -> None:
        """Полный сброс (используется тестами)."""
        with self._lock:
            self._fails.clear()


class CommandRateLimiter:
    """Скользящее окно частоты команд веб-терминала (ADR-090): max_calls за window c.

    В отличие от `LoginRateLimiter` (лимит по НЕУДАЧАМ, сброс при успехе) — здесь
    лимит по ЛЮБОМУ вызову: терминал исполняет произвольные команды на хосте, поэтому
    ограничиваем частоту как таковую (анти-флуд/анти-DoS), успех не «прощает» лимит.
    Состояние — в памяти процесса, чистится лениво.
    """

    def __init__(self, max_calls: int = 30, window: int = 60):
        self.max_calls = max_calls
        self.window = window
        self._calls: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, keys) -> int:
        """Регистрирует вызов и возвращает 0 (разрешено) или сек до освобождения слота.

        Атомарно: если хоть один ключ уже на лимите — вызов НЕ записывается и
        возвращается retry_after (иначе флуд продлевал бы блок бесконечно)."""
        now = time.time()
        with self._lock:
            windows = {}
            wait = 0
            for key in keys:
                ts = [t for t in self._calls.get(key, []) if t > now - self.window]
                windows[key] = ts
                if len(ts) >= self.max_calls:
                    wait = max(wait, int(ts[-self.max_calls] + self.window - now) + 1)
            if wait > 0:
                # Сохраняем подчищенные окна, но НЕ добавляем текущий вызов.
                for key, ts in windows.items():
                    if ts:
                        self._calls[key] = ts
                    else:
                        self._calls.pop(key, None)
                return wait
            for key, ts in windows.items():
                ts.append(now)
                self._calls[key] = ts
            return 0

    def clear(self) -> None:
        """Полный сброс (используется тестами)."""
        with self._lock:
            self._calls.clear()


# Единые экземпляры на процесс.
login_limiter = LoginRateLimiter()
command_limiter = CommandRateLimiter()


def client_keys(request, username: str) -> list[str]:
    """Ключи лимита для запроса: IP клиента + имя пользователя.

    За nginx реальный клиент — ПОСЛЕДНИЙ адрес в X-Forwarded-For (nginx добавляет
    свой $remote_addr в конец цепочки; клиентский спуфинг XFF этот хвост не
    подменяет). При прямом доступе (первичный доступ по IP:7999) XFF нет — берём
    request.client.host.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        ip = xff.split(",")[-1].strip()
    else:
        ip = request.client.host if request.client else "unknown"
    return [f"ip:{ip}", f"user:{(username or '').strip().lower()}"]
