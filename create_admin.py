# --- ИЗМЕНЕННЫЙ ФАЙЛ: create_admin.py (Резервный вариант) ---

import getpass
from sqlalchemy.orm import Session
from app.database import SessionLocal, engine
from app.models import User, Base
from app.security import get_password_hash
import sys  # Добавляем импорт sys

# Проверяем, можно ли использовать getpass.
# Если нет (например, из-за IDE), используем input с предупреждением.
# Часто sys.stdin.isatty() возвращает False в некоторых IDE, что ломает getpass
USE_GETPASS = True
try:
    if not sys.stdin.isatty():
        print(
            "ВНИМАНИЕ: Обнаружена среда, которая может некорректно обрабатывать getpass. Используется стандартный input (пароль будет виден).")
        USE_GETPASS = False
except Exception:
    pass


def secure_input(prompt):
    if USE_GETPASS:
        return getpass.getpass(prompt)
    else:
        return input(prompt + " (ОТОБРАЖАЕТСЯ): ")


def create_admin_user():
    """Создает администратора через командную строку."""
    db: Session = SessionLocal()

    print("--- Создание пользователя-администратора ---")

    # Проверяем, существуют ли уже пользователи
    if db.query(User).first():
        print("В базе данных уже есть пользователи. Скрипт завершает работу.")
        overwrite = input("Хотите создать еще одного? (y/n): ").lower()
        if overwrite != 'y':
            db.close()
            return

    while True:
        username = input("Введите имя пользователя: ").strip()
        if not username:
            print("Имя пользователя не может быть пустым.")
            continue
        if db.query(User).filter(User.username == username).first():
            print(f"Пользователь с именем '{username}' уже существует.")
            continue
        break

    while True:
        password = secure_input("Введите пароль: ")

        # Убраны отладочные print'ы

        if not password:
            print("Пароль не может быть пустым.")
            continue

        password_confirm = secure_input("Подтвердите пароль: ")

        if password != password_confirm:
            print("Пароли не совпадают. Попробуйте снова.")
            continue
        break

    hashed_password = get_password_hash(password)
    admin = User(username=username, hashed_password=hashed_password)

    db.add(admin)
    db.commit()

    print(f"\nПользователь '{username}' успешно создан!")
    db.close()


if __name__ == "__main__":
    # Убедимся, что таблица существует
    Base.metadata.create_all(bind=engine)
    create_admin_user()