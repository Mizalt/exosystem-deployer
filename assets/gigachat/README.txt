# GigaChat CA bundle (ADR-112)

Сюда положить **публичный** корневой сертификат НУЦ Минцифры (Russian Trusted Root CA),
которым подписан GigaChat (`gigachat.devices.sberbank.ru` / `ngw.devices.sberbank.ru`).
Это НЕ секрет — публичный CA, в `.gitignore` НЕ добавлять.

## Зачем

GigaChat подписан РОССИЙСКИМ CA, которого нет в системном trust-store slim-образа →
обычная `verify` в httpx падает. Бандл сертификата в образ + `verify=<путь>` устраняет
причину БЕЗ отключения проверки (MITM-защита сохранена). Альтернатива — `verify=False`
(`ai.vision.insecure_ssl=true`) — только временный отладочный fallback, НЕ для прода.

## Что положить

Файл: `russian_trusted_root_ca.pem` (PEM, корневой сертификат НУЦ Минцифры).
Источник — официальный портал Госуслуг/Минцифры (`gu-st.ru` → «Корневой сертификат
(PEM)») или бандл, поставляемый Сбером в документации GigaChat. Проверь отпечаток
перед добавлением.

## Как подхватывается

`Dockerfile.cloud` копирует эту папку в `/etc/ssl/gigachat/`. Путь к бандлу —
env `CLOUD_GIGACHAT_CA_BUNDLE` (дефолт `/etc/ssl/gigachat/russian_trusted_root_ca.pem`).
`app/cloud/services/ai_infra.py::_vision_ssl_verify`: если файл существует → `verify=<путь>`;
если НЕТ (файл не положили / dev-Windows) → `verify=True` (системный trust). То есть
отсутствие файла НЕ ломает сборку и НЕ отключает проверку — просто нет прод-бандла.

Обновление сертификата — осознанной пересборкой образа (supply-chain, как пин версии
claude-code).
