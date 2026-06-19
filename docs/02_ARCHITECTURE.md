# 02 — Архитектура

## Стек

- **Backend:** Python 3.12, FastAPI, Uvicorn (образ деплоера — `Dockerfile`).
- **БД:** SQLite (`data/deployer.db`) + самописные миграции (`app/database.py`).
  Всё изменяемое состояние деплоера — в каталоге `data/` (один том).
- **Оркестрация:** `docker-py` (`docker.from_env()`) через смонтированный
  `/var/run/docker.sock`.
- **Reverse-proxy / SSL:** Nginx + Certbot (Let's Encrypt) в отдельных контейнерах.
- **Запуск:** `docker compose up -d --build` (deployer + nginx + certbot).
- **Frontend:** статический `index.html` + `static/js/app.js` + `static/css/style.css`.

## Доменная модель (3 уровня)

Это ядро дизайна. Разделение «код / намерение / факт / публикация» — сильная
сторона проекта и прямой аналог Kubernetes.

```
AppBlueprint (приложение, «репозиторий»)
   └── Artifact (версия = загруженный ZIP, immutable)
          └── Deployment (ЖЕЛАЕМОЕ состояние: artifact + target_replicas + group)
                 ├── Instance (ФАКТ: живой контейнер на порту)  ← создаёт Оркестратор
                 └── Application (ПУБЛИКАЦИЯ: домен + SSL → Nginx)
```

| Сущность | Аналог в K8s | Смысл |
|----------|--------------|-------|
| `AppBlueprint` | образ/репозиторий | именованное приложение, группирует версии |
| `Artifact` | image tag | конкретная неизменяемая версия (ZIP + hash) |
| `Deployment` | Deployment/ReplicaSet | желаемое: какая версия и сколько реплик |
| `Instance` | Pod | конкретный запущенный контейнер (в сети `deployer-net`) |
| `Application` | Ingress + Service | публичный домен + SSL, балансировка на Deployment |
| `AppGroup` | пул адресов | логический диапазон портов для уникальности имён инстансов |
| `AppUser` | — | пользователи защищённого приложения (protected mode) |
| `User` | — | администратор панели |

Файлы: `app/models.py`, `app/crud.py`, `app/schemas.py`.

## Поток деплоя

1. **Upload:** админ заливает ZIP → `Artifact` (файл в `uploads/<sha256>.zip`,
   путь хранится в posix-форме).
2. **Deploy:** создаётся `Deployment` (artifact + target_replicas + group).
3. **Reconcile (оркестратор, цикл 5 c):** видит `actual < target` → SCALE UP:
   - назначает логический порт из группы (`get_available_port`) — для уникального
     имени инстанса (host-порт НЕ публикуется);
   - `docker_manager.deploy_service`: распаковывает ZIP, генерирует `Dockerfile`
     приложения (python:3.9-slim, ставит uvicorn+fastapi, CMD `uvicorn main:app
     --port 80`), билдит образ, запускает контейнер **в сети `deployer-net`** с
     `restart_policy=unless-stopped`, меткой `manager=cloud-deployer`;
   - сохраняет `Instance` с **реальным** именем контейнера (`deployer-<name>`).
   - Падающий контейнер не плодит реплики: backoff + CrashLoopBackOff.
4. **Publish:** админ создаёт `Application` (домен + SSL) → `nginx_manager` генерит
   конфиг Nginx; Nginx проксирует на деплоер `/api/proxy/<app>/`, а деплоер
   (`proxy.py`) — на нужный `Instance` по имени контейнера на порт 80.
5. **SSL:** выпуск Let's Encrypt через контейнер Certbot (`ssl_service`).

## Топология (контейнеризовано — ADR-002, ADR-005)

```
[ Любой сервер с Docker ]  —  docker compose up -d --build
  └── сеть deployer-net:
        ├── deployer        (контейнер; /var/run/docker.sock; порт 7999, не публикуется)
        ├── deployer-nginx-proxy       (80/443 наружу; -> http://deployer:7999)
        ├── deployer-certbot-companion (спит; выпуск сертификатов по требованию)
        └── deployer-dep_<app>_<ver>_<port>  ← контейнеры приложений (порт 80 внутри)
```

- Деплоер управляет Docker через **смонтированный сокет**.
- Всё общение — по **именам контейнеров** в `deployer-net` (host-порты приложений
  не публикуются; кроссплатформенно). nginx → деплоер через `resolver`+переменную
  (`proxy_pass http://$deployer_upstream:7999`) — переживает смену IP при рестарте.
- nginx/certbot создаёт **compose** (их bind-mount пути резолвятся на хосте);
  app-контейнеры (без bind-mount) создаёт деплоер.
- Legacy host-process режим (деплоер процессом на хосте) ещё возможен через
  `DEPLOYER_PROXY_HOST=host.docker.internal`, но не основной.

## Известные архитектурные слабые места (тех-долг)

- **SQLite + многопоточность:** оркестратор (`asyncio.to_thread`) и web-воркеры
  делят одну SQLite-базу; `check_same_thread=False` снимает защиту, но не решает
  гонки. Для одного сервера терпимо; при росте — PostgreSQL.
- **Самописные миграции** (ADD COLUMN по диффу метаданных) — хрупко при изменении
  типов/удалении колонок. Кандидат на Alembic.
- **Жёстко зашитый Dockerfile приложений** (только python:3.9 + uvicorn `main:app`) —
  не универсально. Нужен выбор рантайма / пользовательский Dockerfile.
- **Нет аутентификации внутренней связи** деплоер↔контейнеры; полагаемся на сеть.
- ~~docker-cli в образе ради `docker exec`~~ — **устранено** (ADR-007): управление
  Docker идёт через docker-py (`docker_manager.exec_*`), образ — чистый slim.

Установка и эксплуатация — в `07_DEPLOY.md`.
