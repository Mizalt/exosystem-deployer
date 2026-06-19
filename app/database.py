# --- ИСПРАВЛЕННЫЙ ФАЙЛ: app/database.py ---

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

# УДАЛЯЕМ ГЛОБАЛЬНЫЙ ИМПОРТ, КОТОРЫЙ ВЫЗЫВАЕТ ЦИКЛ
# from . import models, schemas

# Всё изменяемое состояние деплоера держим в одном каталоге data/ — его удобно
# монтировать одним томом и сносить при деинсталляции (см. docker-compose.yml).
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SQLALCHEMY_DATABASE_URL = "sqlite:///./data/deployer.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
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