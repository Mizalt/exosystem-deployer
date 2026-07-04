"""Живой прогресс сборки/пулла образов (Ночь 14, ADR-082).

Боль: деплой большого приложения (долгий pull базового образа или тяжёлая
сборка) выглядел как зависание — pull-события docker-py вообще игнорировались,
а стадия сборки нигде не показывалась. Этот модуль разбирает поток событий
low-level `client.api.build(..., decode=True)` на человеческое состояние:

  {'stream': 'Step 3/9 : RUN pip install …'}  → стадия сборки «шаг 3/9»;
  {'status': 'Downloading', 'progressDetail': {'current': N, 'total': M},
   'id': '<layer>'}                            → пулл базового образа по слоям
                                                 (проценты по байтам);
  {'error': '…'}                               → ошибка сборки.

Реестр активных сборок (in-memory, ключ — тег образа, он content-addressed и
одинаков для всех реплик) отдаёт снимок состояния в `/api/services` — панель
рисует прогресс-бар на карточке сервиса. Завершение сборки пишет
`OperationMetric` (kind="build") — сырьё для ETA и аналитики «бутылочных
горлышек» (супер-админка ЛК видит агрегаты через зеркало op-stats).
"""
from __future__ import annotations

import re
import threading
import time

# Шаг pull-прогресса, при котором эмитим новую строку лога (проценты); без
# троттлинга Downloading-события шли бы сотнями в секунду и топили WS-лог.
PULL_EMIT_STEP_PCT = 10

_STEP_RE = re.compile(r"^Step (\d+)/(\d+)\s*:?\s*(.*)$")

# Статусы пулла, означающие «слой готов» (его байты больше не придут).
_LAYER_DONE = {"Pull complete", "Already exists", "Download complete"}


class BuildProgressParser:
    """Разбирает чанки docker build → состояние {stage, percent, detail}.

    Отдельный класс (а не функции реестра) — чтобы парсер был тестируем сам по
    себе и не тянул ни Docker, ни БД.
    """

    def __init__(self):
        self.stage = "prepare"          # prepare | pull | build
        self.step = None                # текущий шаг сборки (int)
        self.total_steps = None
        self.step_text = ""
        # id слоя → [downloaded, download_total, done]; байты только по Downloading.
        self._layers: dict[str, list] = {}
        self._extracting = False        # все слои скачаны, идёт распаковка
        self._last_emitted_pct = -PULL_EMIT_STEP_PCT
        # Замер фазы пулла (в meta метрики сборки): первый/последний pull-чанк.
        self._pull_first_ts: float | None = None
        self._pull_last_ts: float | None = None

    # --- Приём чанков -----------------------------------------------------

    def feed(self, chunk: dict) -> str | None:
        """Обрабатывает один chunk; возвращает строку для лога (или None).

        Build-строки возвращаются как есть (прежнее поведение WS-лога, ADR-023),
        pull-события агрегируются в редкие строки-проценты (троттлинг).
        """
        if not isinstance(chunk, dict):
            return None
        if "error" in chunk:
            return str(chunk["error"]).rstrip() or None
        if "stream" in chunk:
            return self._feed_stream(str(chunk["stream"]))
        if "status" in chunk:
            return self._feed_pull(chunk)
        return None

    def _feed_stream(self, text: str) -> str | None:
        line = text.rstrip()
        if not line:
            return None
        m = _STEP_RE.match(line)
        if m:
            self.stage = "build"
            self.step = int(m.group(1))
            self.total_steps = int(m.group(2))
            self.step_text = m.group(3).strip()
            # Шаг начался → пулл (если был) закончился.
            self._extracting = False
        return line

    def _feed_pull(self, chunk: dict) -> str | None:
        status = str(chunk.get("status") or "").strip()
        layer = chunk.get("id")
        now = time.time()
        if self._pull_first_ts is None:
            self._pull_first_ts = now
        self._pull_last_ts = now
        # Пулл идёт ВНУТРИ шага FROM — показываем его как отдельную стадию,
        # т.к. именно он «висит» дольше всего на большом базовом образе.
        self.stage = "pull"

        if status.startswith("Pulling from"):
            self._last_emitted_pct = -PULL_EMIT_STEP_PCT
            return f"Скачивание базового образа ({chunk.get('id') or '…'})…"

        if not layer:
            return None
        entry = self._layers.setdefault(str(layer), [0, 0, False])
        detail = chunk.get("progressDetail") or {}
        if status == "Downloading":
            entry[0] = int(detail.get("current") or 0)
            entry[1] = int(detail.get("total") or 0) or entry[1]
            self._extracting = False
        elif status == "Extracting":
            self._extracting = True
        if status in _LAYER_DONE:
            entry[2] = True
            if entry[1]:
                entry[0] = entry[1]

        pct = self.pull_percent()
        if pct is not None and pct - self._last_emitted_pct >= PULL_EMIT_STEP_PCT:
            self._last_emitted_pct = pct
            return f"Базовый образ: {pct}% ({self._bytes_line()})"
        return None

    # --- Снимок состояния ---------------------------------------------------

    def pull_percent(self) -> int | None:
        total = sum(e[1] for e in self._layers.values())
        if not total:
            return None
        current = sum(min(e[0], e[1]) if e[1] else e[0] for e in self._layers.values())
        return min(int(100 * current / total), 100)

    def _bytes_line(self) -> str:
        mb = 1024 * 1024
        total = sum(e[1] for e in self._layers.values())
        current = sum(min(e[0], e[1]) if e[1] else e[0] for e in self._layers.values())
        return f"{current // mb}/{total // mb} МБ"

    def pull_seconds(self) -> float | None:
        if self._pull_first_ts is None or self._pull_last_ts is None:
            return None
        return round(self._pull_last_ts - self._pull_first_ts, 1)

    def state(self) -> dict:
        """Текущее состояние для UI: стадия, процент (может быть None), описание."""
        if self.stage == "pull":
            pct = self.pull_percent()
            if self._extracting:
                detail = "Распаковка базового образа…"
            elif pct is not None:
                detail = f"Скачивание базового образа: {pct}% ({self._bytes_line()})"
            else:
                detail = "Скачивание базового образа…"
            return {"stage": "pull", "percent": pct, "detail": detail}
        if self.stage == "build" and self.step and self.total_steps:
            # Процент по шагам: выполняющийся шаг не завершён, поэтому потолок 99.
            pct = min(int(100 * self.step / max(self.total_steps, 1)), 99)
            text = (self.step_text or "").strip()
            detail = f"Сборка: шаг {self.step}/{self.total_steps}" + (
                f" — {text[:60]}" if text else "")
            return {"stage": "build", "percent": pct, "detail": detail}
        return {"stage": "prepare", "percent": None, "detail": "Подготовка сборки…"}


# --------------------------------------------------------------------------
#  Реестр активных сборок (ключ — тег образа; content-addressed, ADR-015/021)
# --------------------------------------------------------------------------

_lock = threading.Lock()
_active: dict[str, dict] = {}  # tag → {"parser": BuildProgressParser, "started": ts}


def begin(image_tag: str) -> None:
    """Регистрирует начало реальной сборки (кэш-хит сюда не попадает)."""
    with _lock:
        _active[image_tag] = {"parser": BuildProgressParser(), "started": time.time()}


def feed(image_tag: str, chunk: dict) -> str | None:
    """Скармливает chunk парсеру сборки; возвращает строку для лога (или None)."""
    with _lock:
        entry = _active.get(image_tag)
    if not entry:
        return None
    return entry["parser"].feed(chunk)


def finish(image_tag: str, ok: bool) -> None:
    """Снимает сборку с учёта и пишет замер OperationMetric (best-effort)."""
    with _lock:
        entry = _active.pop(image_tag, None)
    if not entry:
        return
    parser: BuildProgressParser = entry["parser"]
    duration = round(time.time() - entry["started"], 1)
    meta: dict = {}
    if parser.pull_seconds() is not None:
        meta["pull_seconds"] = parser.pull_seconds()
    if parser.total_steps:
        meta["steps"] = parser.total_steps
    from app.services import op_metrics
    op_metrics.record("build", subject=image_tag.split(":")[-1][:16],
                      duration_seconds=duration,
                      outcome="done" if ok else "error", meta=meta or None)


def get(image_tag: str) -> dict | None:
    """Снимок живого прогресса сборки для UI (None — сборка не идёт)."""
    with _lock:
        entry = _active.get(image_tag)
        if not entry:
            return None
        snap = entry["parser"].state()
        snap["elapsed_seconds"] = round(time.time() - entry["started"])
        return snap
