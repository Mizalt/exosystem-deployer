# --- ИСПРАВЛЕННЫЙ ФАЙЛ: app/database.py ---

import os
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

# УДАЛЯЕМ ГЛОБАЛЬНЫЙ ИМПОРТ, КОТОРЫЙ ВЫЗЫВАЕТ ЦИКЛ
# from . import models, schemas

# Всё изменяемое состояние деплоера держим в одном каталоге data/ — его удобно
# монтировать одним томом и сносить при деинсталляции (см. docker-compose.yml).
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SQLALCHEMY_DATABASE_URL = "sqlite:///./data/deployer.db"


def _env_pool_int(name: str, default: int, lo: int, hi: int) -> int:
    """Целое из env с санацией в [lo..hi]; пусто/мусор → дефолт (не роняем старт)."""
    raw = (os.environ.get(name) or "").strip()
    try:
        val = int(raw) if raw else default
    except ValueError:
        return default
    return max(lo, min(val, hi))


# Параметры пула (ADR-137). Дефолтный QueuePool (5+10, timeout 30) исчерпывался в
# боевом инциденте: сессия БД удерживается на ВСЁ время docker-сборки
# (build_service), параллельные публикации + фоновые циклы → «QueuePool limit of
# size 5 overflow 10 reached, timeout 30». Класс пула и check_same_thread=False
# НЕ меняем: файловый SQLite в SQLAlchemy 2.x использует QueuePool — параметры
# применяются. pool_pre_ping/pool_recycle — гигиена против протухших коннектов.
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_size=_env_pool_int("DEPLOYER_DB_POOL_SIZE", 10, 1, 64),
    max_overflow=_env_pool_int("DEPLOYER_DB_MAX_OVERFLOW", 20, 0, 128),
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
)


BUSY_TIMEOUT_MS = 10_000


def apply_sqlite_pragmas(dbapi_connection) -> None:
    """WAL + `busy_timeout` на КАЖДОЕ соединение деплоерской БД.

    ADR-137 после боевого инцидента поправил ПУЛ, но не ЖУРНАЛ. Ниже — что фикс
    журнала даёт и чего НЕ даёт; всё замерено, тесты в
    `tests/test_deployer_db_pragmas.py` воспроизводят обе половины.

    ЧТО WAL ДАЁТ:
      * читатель НЕ встаёт, когда долгая write-транзакция спиллит страничный кэш
        на диск. На rollback-журнале спилл обязан поднять лок до EXCLUSIVE
        (грязные страницы идут прямо в файл БД) — и читатель получает
        `database is locked`: замер 20/20 прогонов, отказ через ~440 мс. В WAL
        страницы уходят в `-wal`, читатель продолжает читать свой снапшот:
        20/20 успех за ~7 мс. Это и есть наша защита, потому что у деплоера самый
        долгий писатель на платформе — сессия держится всю docker-сборку
        (минуты, `build_service`), и именно она рано или поздно спиллит кэш;
      * узкое окно КОММИТА: читатель в цикле ловил 1–2 `database is locked` на
        каждый крупный коммит без WAL и ровно 0 с WAL (4 прогона). Эффект
        реальный, но событий единицы на тысячи чтений — в тест не вынесен,
        стабильно воспроизводится только спилл выше.

    ЧЕГО WAL НЕ ДАЁТ — второго одновременного ПИСАТЕЛЯ. Замер: пока писатель
    держит write-транзакцию, второй писатель получает `database is locked` и ДО
    фикса (5,53 с), и ПОСЛЕ (10,95 с). Изменилось только терпение: дефолт
    pysqlite 5 000 мс → наши 10 000 мс. Поэтому здесь НЕ написано «читатели
    больше не блокируются» и «WAL разводит писателя и читателей» — прежняя
    формулировка была преувеличением.

    Поправка к прежнему комментарию в этом же месте: НЕЗАКОММИЧЕННЫЙ писатель
    держит RESERVED, а не EXCLUSIVE, и сам по себе читателей НЕ блокирует — на
    `journal_mode=delete` читатель спокойно читает при открытой транзакции
    писателя (проверено, тест это фиксирует). EXCLUSIVE появляется только на
    коммите и при спилле кэша.

    `busy_timeout` поднимаем до 10 000 мс — вдвое больше дефолта pysqlite
    (замерено: драйвер и без нас ставит 5 000 мс, а не ноль).

    `journal_mode` — свойство ФАЙЛА, не соединения (переживает рестарт),
    повторная установка на каждом коннекте — безвредный no-op. `synchronous`
    сознательно НЕ трогаем: дефолт надёжнее при внезапном ребуте ноды.
    Best-effort: сбой PRAGMA не роняет старт — БД останется на прежнем журнале.
    Курсор закрываем через `closing`: раньше `cursor.close()` стоял последней
    строкой `try`, поэтому отказ на втором PRAGMA оставлял курсор незакрытым.

    🔗 Осознанный дубль `app/cloud/database.apply_sqlite_pragmas` (тот же приём,
    гейт T7 (общая нода)): движки живут в разных изданиях — open-core
    деплоер и cloud-контрол-плейн со своим entrypoint `app.cloud.app:cloud_app`
    (`Dockerfile.cloud`), — и общий импорт поднимал бы деплоерский движок в
    процессе контрол-плейна. Правишь здесь — правь и там; расхождение двух копий
    ловит `tests/test_cloud_db_pragmas.py::test_both_editions_apply_the_same_pragmas`.
    """
    try:
        with closing(dbapi_connection.cursor()) as cursor:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except Exception as e:  # noqa: BLE001 — гигиена соединения, не критичный путь
        print(f"ERROR: deployer-db PRAGMA (WAL/busy_timeout) не применились: {e}")


# Слушатель ИМЕНОВАННЫЙ (не lambda) — чтобы тест мог проверить именно ДОСТАВКУ
# фикса до боевого движка через `event.contains`. Сами значения PRAGMA тест
# сверяет на своём ФАЙЛОВОМ движке: на `:memory:` WAL не включается, поэтому
# проверка на подменённом в тестах движке была бы «зелёной, но не доехавшей».
def _deployer_connect_listener(dbapi_connection, _record) -> None:
    apply_sqlite_pragmas(dbapi_connection)


event.listen(engine, "connect", _deployer_connect_listener)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def init_db_with_migrations():
    """
    Инициализирует базу данных.
    1. Создает все таблицы, определенные в models.py, если их не существует.
    2. Проверяет существующие таблицы и добавляет недостающие колонки.
    3. Создает группы по умолчанию, если их нет.
    """
    print("INFO: Initializing and migrating database...")

    # Шаг 1: Создаем все таблицы, которые могут отсутствовать
    # ВАЖНО: Мы импортировали models, поэтому Base.metadata знает о всех наших таблицах

    # ПЕРЕМЕЩАЕМ ИМПОРТЫ СЮДА, ЧТОБЫ РАЗОРВАТЬ ЦИКЛ ПРИ ЗАПУСКЕ
    from . import models, schemas

    Base.metadata.create_all(bind=engine)

    # Шаг 2: Проверяем и добавляем недостающие колонки (миграция)
    inspector = inspect(engine)
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            table_name = table.name
            if not inspector.has_table(table_name):
                print(f"INFO: Table '{table_name}' was just created. Skipping column check.")
                continue

            existing_columns = {col['name'] for col in inspector.get_columns(table_name)}
            for column in table.columns:
                column_name = column.name
                if column_name not in existing_columns:
                    try:
                        # --- ИСПРАВЛЕННАЯ ЛОГИКА ---
                        # Мы больше не используем column.compile(), который генерировал неверный SQL.
                        # Вместо этого мы строим запрос вручную, что более надежно для SQLite.
                        column_type = column.type.compile(dialect=engine.dialect)
                        add_column_sql = f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}'

                        print(f"INFO: Missing column '{column_name}' in table '{table_name}'. Applying migration...")
                        print(f"  > EXEC: {add_column_sql}")
                        connection.execute(text(add_column_sql))
                        connection.commit()  # Используем commit после каждой транзакции
                        print(f"SUCCESS: Column '{column_name}' added to table '{table_name}'.")
                    except Exception as e:
                        print(f"ERROR: Failed to add column '{column_name}' to table '{table_name}'. Error: {e}")
                        connection.rollback()

    # Шаг 3: Инициализация данных по умолчанию
    db = SessionLocal()
    try:
        # Теперь models доступен благодаря импорту выше
        if not db.query(models.AppGroup).first():
            print("INFO: AppGroup table is empty. Creating default groups.")
            default_groups = [
                schemas.AppGroupCreate(name="frontend-apps", start_port=8001, end_port=8010),
                schemas.AppGroupCreate(name="backend-services", start_port=9001, end_port=9010)
            ]
            for group_data in default_groups:
                db_group = models.AppGroup(**group_data.model_dump())
                db.add(db_group)
            db.commit()
            print("INFO: Default groups created.")
    finally:
        db.close()

    print("INFO: Database initialization and migration complete.")


# Функция для получения сессии БД в эндпоинтах
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()