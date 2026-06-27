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


# Единый экземпляр на процесс.
login_limiter = LoginRateLimiter()


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
