# Center Messenger

Кабинет мессенджера для клиентов студии: единое окно с переписками,
командой, настройками подключений CRM и ботами MAX.

Инструкция разработчику: [общий загрузчик и подключение новых каналов](app/backend/src/center_messenger/loader/README.md).
В ней описаны адаптеры телефонии, вложений и мессенджеров, очереди, сохранение
курсоров, пример без сети и проверки совместимости.

Последний этап: [точечная загрузка клиентов и диагностика «Сводки»](docs/releases/2026-10-27-customer-lookup-summary.md).
Дополнение: [чтение карточки без пересчёта переписки](docs/releases/2026-10-27-family-read.md).

## Стек

- **API** — FastAPI (Python 3.12), точка входа `center_messenger.main:app`
- **БД** — PostgreSQL 16, миграции Alembic (`app/backend/migrations`)
- **Публикация** — Nginx как reverse proxy перед API (`infra/nginx`)
- **Запуск** — Docker Compose (`compose.yaml`), образ собирается из `Dockerfile`

## Структура

| Путь | Назначение |
| --- | --- |
| `app/backend/src` | код приложения |
| `app/backend/migrations` | миграции Alembic |
| `infra/nginx` | примеры конфигурации виртуального хоста |
| `infra/postgres` | init-скрипт роли приложения в PostgreSQL |
| `scripts` | сборка релизного бандла, загрузка образа, preflight, инициализация секретов |
| `.gitverse/workflows` | CI/CD GitVerse |
| `docs` | эксплуатационная документация |

## Сборка и запуск

```bash
docker build --platform linux/amd64 -t center-messenger:local .
docker compose config --quiet
docker compose up -d --wait db
docker compose run --rm migrate
docker compose up -d --wait api
curl --fail --silent --show-error http://127.0.0.1:8100/health/ready
```

Ожидается `{"status":"ready"}`. Процесс без применённых миграций или с ролью
владельца БД возвращает 503.

## Проверки

```bash
python3 -m pip install -r requirements-test.txt
python3 -m compileall -q app/backend/src app/backend/migrations scripts
PYTHONPATH=app/backend/src python3 -m unittest discover -s tests -p 'test_*.py' -v
npm ci --ignore-scripts
npm test
docker run --rm --entrypoint alembic <image> -c /app/alembic.ini history
```

Для проверок импорта/чтения сообщений задайте `CM_TEST_DATABASE_URL` с адресом
отдельной тестовой PostgreSQL. Набор создаёт случайную схему и применяет в ней
все миграции. Без этого адреса интеграционные проверки явно пропускаются.
Для тестов интерфейса требуется Node.js 24; Node не требуется рабочему серверу.

Проверка настоящей прокрутки в Chromium на вымышленных данных:

```sh
npx playwright install --with-deps chromium
npm run test:scroll
npm run test:progress
```

Проверяются 201 сообщение, нативное колесо мыши, сохранение позиции и отсутствие
пропусков при старых/новых обновлениях. Для уже установленного Chromium можно
задать `CM_TEST_CHROMIUM_EXECUTABLE`; дополнительные аргументы передаются JSON-массивом
в `CM_TEST_CHROMIUM_ARGS`. Доступ к рабочим сообщениям и отправка клиентам не нужны.
Проверка `test:progress` открывает весь кабинет с вымышленным API: ошибка 503,
зависший запрос с отменой по таймауту, восстановление статуса и прекращение
опроса при выходе из подключений. Проверка блокировок в `test_history_snapshot.py`
требует обычной PostgreSQL; для PGlite она явно пропускается.

## Репозиторий и развёртывание

**Актуальный репозиторий:** `rastudiocmit-bot/ramessenger`, ветка `main`.
**Целевой VPS:** `83.222.25.109`, приложение на `messenger.rastudio.org`.

GitHub Actions (`.github/workflows/verify.yml`) проверяет синтаксис Python,
Docker Compose, сборку образа и граф миграций при push/PR.
**Автоматическая публикация на VPS не включена.** Включать её только после
аудита установленного на сервере контроллера, миграции доверия с прежнего
`устаревший репозиторий (отключён)` и проверки резервного копирования.
Прежний GitVerse workflow остаётся историческим и не управляет GitHub Actions.

Не изменять работающие Nginx/PM2 и сайт `rastudio.org`; не выполнять
`docker compose down --volumes` на сервере.

## Документация

- [Этап 4: ошибки и продолжение загрузки карточек](docs/releases/2026-10-27-card-refresh-resume.md)
- [Размещение](docs/deployment.md)
- [Контроллер обновлений](docs/autodeploy.md)
- [HTTPS](docs/https.md)
- [Аккаунты](docs/accounts.md)
- [Подключение AlfaCRM](docs/alfa-connection.md)
- [Справочник CRM](docs/crm-directory.md)
- [Бот MAX](docs/max-bot.md)
