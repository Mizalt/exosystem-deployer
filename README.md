# exosystem-deployer

**Self-hosted PaaS — «Kubernetes для малого бизнеса».** Ставится на сервер одной
командой, разворачивает приложения через веб-панель, держит сервисы запущенными по
принципу желаемого состояния. Без DevOps-инженера и дорогих managed-платформ.

> ⚠️ Проект в активной разработке (MVP). Установка одной командой проверена на
> чистом Debian 11. См. документацию в [`docs/`](docs/README.md).

## Возможности (текущие)

- **Декларативная оркестрация:** задаёшь «нужно N реплик версии X» — ядро
  поддерживает это состояние, чинит упавшее (CrashLoopBackOff, как в k8s).
- **Всё в контейнерах:** деплоер, reverse-proxy и certbot поднимаются одним
  `docker compose`. Деплоер управляет Docker через смонтированный сокет.
- **Reverse-proxy + авто-SSL:** Nginx + Let's Encrypt, домены и сертификаты из UI.
- **Библиотека версий:** загруженные артефакты (ZIP) → версии → деплои → реплики.
- **Аутентификация панели** (JWT), catchall-403 для неизвестных доменов.

## Установка одной командой

**Linux:**
```sh
curl -fsSL https://raw.githubusercontent.com/Mizalt/exosystem-deployer/main/install.sh | sh
```

**Windows (Docker Desktop, PowerShell):**
```powershell
irm https://raw.githubusercontent.com/Mizalt/exosystem-deployer/main/install.ps1 | iex
```

Скрипт проверит Docker, склонирует код в `/opt/exosystem-deployer` (или
`%USERPROFILE%\exosystem-deployer`), сгенерирует секрет, поднимет стек и напечатает
**одноразовый пароль администратора**. Дальше — задай домен панели и открой
`https://<домен>`.

### Требования
- Docker Engine + плагин `docker compose`
- `git`, открытые порты `80`/`443`
- Целевая ОС: Linux (Debian 11+ рекомендуется); поддержка Windows — Docker Desktop

## Запуск из исходников (разработка)

```sh
git clone https://github.com/Mizalt/exosystem-deployer.git
cd exosystem-deployer
cp .env.example .env   # задайте DEPLOYER_SECRET_KEY
docker compose up -d --build
```

Тесты:
```sh
pip install -r requirements-dev.txt
pytest
```

## Архитектура (кратко)

```
AppBlueprint (приложение) → Artifact (версия) → Deployment (желаемое состояние)
                                                   ├── Instance (контейнер-реплика)
                                                   └── Application (домен + SSL)
```

Подробно — [`docs/02_ARCHITECTURE.md`](docs/02_ARCHITECTURE.md). Установка и первый
деплой — [`docs/07_DEPLOY.md`](docs/07_DEPLOY.md).

## Безопасность

Репозиторий публичный — **секреты в него не коммитятся** (см. `.gitignore`:
`.env`, `secret.key`, `data/`, `ssl_certs/`, `uploads/` и т.д. исключены). Деплоер
монтирует `docker.sock` (root-эквивалент на хосте) — ставьте только на доверенные
серверы.

## Деинсталляция

```sh
docker compose down
rm -rf data uploads nginx_configs ssl_certs acme_challenge
```

## Лицензия

**Business Source License 1.1 (BSL 1.1)** — см. [`LICENSE`](LICENSE). Это
**source-available**-лицензия (не OSI-«open source», но исходники открыты):

- ✅ **Можно свободно:** читать, изучать, изменять код; **ставить в production на
  свой сервер** и пользоваться для своих приложений; настраивать продукт клиентам
  на **их собственных серверах** (единичные, single-tenant установки — например,
  фрилансер/агентство под конкретного клиента).
- ⛔ **Нельзя без отдельной коммерческой лицензии:** предлагать продукт третьим
  лицам как **мульти-тенант хостинг/PaaS** (перепродажа «деплой-как-сервис», где
  ценность — функциональность самого продукта). Это «Competing Service».
- ⏳ **Со временем открывается полностью:** каждая версия через **4 года** после
  публикации автоматически переходит под **Apache License 2.0**.

По коммерческим лицензиям и исключениям — `licensing@exosystem.tech`.
