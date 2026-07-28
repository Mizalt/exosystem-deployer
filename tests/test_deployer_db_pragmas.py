"""WAL + `busy_timeout` для `deployer.db` (журнальная половина ADR-137).

ADR-137 после боевого инцидента поправил только ПУЛ (`test_build_stability.py`,
блок «в»), а журнал остался дефолтным — при этом именно у деплоера самый долгий
писатель: сессия держится всю docker-сборку.

Почему тесты выглядят именно так:
  * фактические значения PRAGMA проверяются на СВОЁМ ФАЙЛОВОМ движке (на
    `:memory:` WAL не включается — journal_mode остаётся `memory`), а доставка
    фикса до боевого `app.database.engine` — отдельно, через реестр слушателей
    события `connect`. Иначе тест был бы зелёным при неприменённом фиксе;
  * каждый поведенческий тест устроен как A/B: один и тот же сценарий гоняется на
    BARE-движке (без PRAGMA, `journal_mode=delete`) и на TUNED (с фиксом). Так
    видно, что тест действительно различает режимы. Здесь раньше жил тест
    «читатель работает, пока писатель держит открытую транзакцию» — он был
    ВАКУУМНЫМ: проходил и без WAL (см.
    `test_open_write_transaction_alone_does_not_block_readers_even_without_wal`).

Калька с `tests/test_cloud_db_pragmas.py`.
"""
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from app import database


def _pragma(conn, name: str):
    return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def _bare_engine(db_file):
    """Движок БЕЗ нашего фикса — состояние «до» (rollback-журнал `delete`)."""
    return create_engine(f"sqlite:///{db_file}",
                         connect_args={"check_same_thread": False})


def _tuned_engine(db_file):
    eng = _bare_engine(db_file)
    event.listen(eng, "connect",
                 lambda dbapi_conn, _rec: database.apply_sqlite_pragmas(dbapi_conn))
    return eng


def _engine(db_file, tuned: bool):
    return _tuned_engine(db_file) if tuned else _bare_engine(db_file)


def test_pragmas_put_deployer_file_db_into_wal_with_busy_timeout(tmp_path):
    """Факт PRAGMA: без фикса файловая БД остаётся на rollback-журнале (`delete`),
    с фиксом — `wal`, и `busy_timeout` поднят до нашего значения.

    Заодно фиксируем факт, поправляющий гейт T7 (`28` говорит «ни busy_timeout»):
    драйвер pysqlite и без нас ставит 5 000 мс, а не ноль — мы лишь удваиваем
    терпение. Что даёт сам WAL — проверяют поведенческие тесты ниже."""
    db_file = tmp_path / "probe.db"
    bare = _bare_engine(db_file)
    with bare.connect() as conn:                          # состояние ДО фикса
        assert _pragma(conn, "journal_mode") == "delete"
        assert int(_pragma(conn, "busy_timeout")) == 5000
    bare.dispose()

    tuned = _tuned_engine(db_file)
    with tuned.connect() as conn:
        assert _pragma(conn, "journal_mode") == "wal"
        assert int(_pragma(conn, "busy_timeout")) == database.BUSY_TIMEOUT_MS
    tuned.dispose()


def test_wal_survives_reconnect_and_applies_to_every_connection(tmp_path):
    """WAL — свойство файла, `busy_timeout` — соединения: после реконнекта должны
    быть на месте ОБА (поэтому PRAGMA вешаем на `connect`, а не разово на старте)."""
    db_file = tmp_path / "reconnect.db"
    first = _tuned_engine(db_file)
    with first.connect() as conn:
        assert _pragma(conn, "journal_mode") == "wal"
    first.dispose()

    second = _tuned_engine(db_file)
    with second.connect() as conn:
        assert _pragma(conn, "journal_mode") == "wal"
        assert int(_pragma(conn, "busy_timeout")) == database.BUSY_TIMEOUT_MS
    second.dispose()


def _read_while_writer_holds_open_transaction(db_file, tuned: bool, rows: int,
                                              tiny_cache: bool):
    """Писатель держит открытую write-транзакцию, читатель пытается читать.

    Возвращает `"ok"` либо `"locked"`. `tiny_cache` + много строк = заставляем
    транзакцию СПИЛЛИТЬ страничный кэш на диск (в rollback-журнале спилл требует
    EXCLUSIVE-лока). Читателю ставим короткий `busy_timeout`, чтобы негативная
    ветка падала быстро, а не через 5–10 с; на положительную ветку это не влияет
    (при WAL читателю ждать вообще нечего).
    """
    writer_engine, reader_engine = _engine(db_file, tuned), _engine(db_file, tuned)
    with writer_engine.connect() as setup:
        setup.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        setup.exec_driver_sql("INSERT INTO t (v) VALUES ('before')")
        setup.commit()

    writer = writer_engine.connect()
    try:
        if tiny_cache:
            writer.exec_driver_sql("PRAGMA cache_size=2")     # 2 страницы кэша
        payload = "x" * 2000
        for i in range(rows):
            writer.exec_driver_sql(f"INSERT INTO t (v) VALUES ('{payload}-{i}')")
        try:
            with reader_engine.connect() as reader:
                reader.exec_driver_sql("PRAGMA busy_timeout=50")
                # 🔴 БЕЗ фильтра по значению: с фильтром `WHERE v='before'`
                # утверждение «незакоммиченное не видно» доказывало само себя —
                # условие отсекало чужие строки независимо от изоляции.
                rows_seen = reader.execute(text("SELECT v FROM t")).scalars().all()
            assert rows_seen == ["before"]   # видно РОВНО закоммиченное
            return "ok"
        except OperationalError as e:
            assert "database is locked" in str(e)
            return "locked"
    finally:
        writer.rollback()
        writer.close()
        writer_engine.dispose()
        reader_engine.dispose()


def test_open_write_transaction_alone_does_not_block_readers_even_without_wal(tmp_path):
    """🔴 АНТИ-ВАКУУМ. Здесь раньше был тест «смысл фикса»: писатель держит
    открытую транзакцию, читатель читает — значит WAL работает. Это НЕПРАВДА:
    сценарий проходит и на rollback-журнале.

    Причина: незакоммиченный писатель держит RESERVED-лок, а не EXCLUSIVE, и
    читателей (SHARED) не трогает. Тест закреплён специально, чтобы ложное
    утверждение не вернулось в комментарии и в докстринги `database.py`."""
    assert _read_while_writer_holds_open_transaction(
        tmp_path / "bare.db", tuned=False, rows=1, tiny_cache=False) == "ok"
    assert _read_while_writer_holds_open_transaction(
        tmp_path / "wal.db", tuned=True, rows=1, tiny_cache=False) == "ok"


def test_wal_keeps_readers_alive_when_long_writer_spills_page_cache(tmp_path):
    """СМЫСЛ ФИКСА, который реально различает режимы: долгая write-транзакция
    спиллит страничный кэш на диск. На rollback-журнале спилл поднимает лок до
    EXCLUSIVE → читатель получает `database is locked`; в WAL страницы уходят в
    `-wal`, читатель продолжает читать снапшот.

    Это боевой сценарий деплоера: docker-сборка держит сессию минутами
    (`build_service`) и пишет достаточно, чтобы кэш вылился на диск, — а ЛК и
    фоновые циклы в это время читают. Замер: 20/20 прогонов `locked` без WAL
    (~440 мс до отказа) против 20/20 `ok` с WAL (~7 мс)."""
    without_wal = _read_while_writer_holds_open_transaction(
        tmp_path / "spill_bare.db", tuned=False, rows=40, tiny_cache=True)
    with_wal = _read_while_writer_holds_open_transaction(
        tmp_path / "spill_wal.db", tuned=True, rows=40, tiny_cache=True)
    assert without_wal == "locked", "негативная ветка не воспроизвелась — тест стал вакуумным"
    assert with_wal == "ok", "WAL не спас читателя при спилле кэша"


def _second_writer_result(db_file, tuned: bool):
    """Второй писатель при уже открытой write-транзакции первого."""
    e1, e2 = _engine(db_file, tuned), _engine(db_file, tuned)
    with e1.connect() as setup:
        setup.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        setup.commit()
    first = e1.connect()
    try:
        first.exec_driver_sql("INSERT INTO t (v) VALUES ('writer-1')")
        try:
            with e2.connect() as second:
                second.exec_driver_sql("PRAGMA busy_timeout=100")   # чтоб не ждать 10 с
                second.exec_driver_sql("INSERT INTO t (v) VALUES ('writer-2')")
                second.commit()
            return "ok"
        except OperationalError as e:
            assert "database is locked" in str(e)
            return "locked"
    finally:
        first.rollback()
        first.close()
        e1.dispose()
        e2.dispose()


def test_wal_does_not_give_a_second_concurrent_writer(tmp_path):
    """🔴 ГРАНИЦА ФИКСА (честность важнее красивого отчёта): WAL НЕ даёт двух
    писателей одновременно. Второй писатель получает `database is locked` и до
    фикса, и после — меняется только терпение (`busy_timeout` 5 000 → 10 000 мс).

    Замер на полных таймаутах: 5,53 с до фикса против 10,95 с после. В тесте
    второму писателю ставим 100 мс, иначе сьют ждал бы 16 с ради того же факта.
    Поэтому в докстрингах `database.py` нет обещания «параллельная публикация
    больше не получает отказ» — она его получает, просто позже."""
    assert _second_writer_result(tmp_path / "w_bare.db", tuned=False) == "locked"
    assert _second_writer_result(tmp_path / "w_wal.db", tuned=True) == "locked"


def test_fix_is_delivered_to_the_real_deployer_engine():
    """Доставка: слушатель `connect` навешен на боевой движок, и этот движок —
    ФАЙЛОВЫЙ (на in-memory WAL бессмысленен, фикс должен целиться в файл)."""
    assert event.contains(database.engine, "connect",
                          database._deployer_connect_listener)
    assert "deployer.db" in str(database.engine.url)


class _BoomCursor:
    """Курсор, падающий на ВТОРОМ `execute` (`busy_timeout`) — как реальный сбой
    после успешного `journal_mode`. Считает вызовы и запоминает закрытие."""

    def __init__(self):
        self.calls = 0
        self.closed = False

    def execute(self, _sql):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("disk I/O error")

    def close(self):
        self.closed = True


class _BoomConn:
    def __init__(self):
        self.cur = _BoomCursor()

    def cursor(self):
        return self.cur


def test_pragma_failure_does_not_break_startup_and_closes_cursor():
    """Best-effort + гигиена ресурса: сбой PRAGMA не роняет соединение (БД
    останется на прежнем журнале — деградация, а не отказ деплоера на старте),
    и курсор при этом ЗАКРЫВАЕТСЯ.

    До фикса `cursor.close()` стоял последней строкой `try`, поэтому исключение
    на втором PRAGMA утекало курсором (проверено: с прежней реализацией
    `closed` остаётся False)."""
    conn = _BoomConn()
    database.apply_sqlite_pragmas(conn)        # не бросает — этого и хотим
    assert conn.cur.calls == 2, "второй PRAGMA должен был быть вызван"
    assert conn.cur.closed, "курсор утёк: не закрыт при сбое второго PRAGMA"


def test_boom_cursor_would_leak_with_the_pre_fix_implementation():
    """Анти-вакуум для теста выше: воспроизводим ПРЕЖНЮЮ реализацию (close()
    последней строкой try) на том же фейковом курсоре и убеждаемся, что она
    действительно оставляла курсор незакрытым. Иначе проверка `closed` ничего
    не стоила бы."""
    conn = _BoomConn()
    with pytest.raises(RuntimeError):          # как было ДО фикса, без try/finally
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()
    assert not conn.cur.closed                 # ← утечка, которую закрыл фикс
