# --- Dockerfile деплоера (ядра инфраструктуры) ---
# Сам деплоер тоже работает в контейнере (ADR-002). Управляет Docker через
# смонтированный /var/run/docker.sock.

FROM python:3.12-slim

# docker-cli в образе НЕ нужен: всё управление Docker (создание контейнеров,
# nginx reload/test, генерация self-signed SSL в certbot-контейнере) идёт через
# docker-py по смонтированному сокету — см. app/services/docker_manager.py
# (exec_in_container / exec_stream_in_container). Образ остаётся чистым slim.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Сначала зависимости — лучше кешируется при изменениях кода.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем исходники (через .dockerignore исключены боевые данные и тома).
COPY . .

EXPOSE 7999

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7999"]
