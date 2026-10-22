# Center Messenger

Кабинет мессенджера для клиентов студии: единое окно с переписками,
командой, настройками подключений CRM и ботами MAX.

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
python3 -m compileall -q app/backend/src app/backend/migrations scripts
docker run --rm --entrypoint alembic <image> -c /app/alembic.ini history
```

## Деплой

Публикация выполняется из GitVerse по push в `main`, файл
`.gitverse/workflows/ci.yml`:

1. раннер `self-hosted` на VPS клонирует точный коммит по SSH;
2. проверяется синтаксис Python и собирается образ
   `center-messenger:gitverse-<sha12>` (linux/amd64);
3. в контейнере проверяются приложение и граф миграций;
4. `scripts/build_release.py` собирает бандл в каталог релиза раннера;
5. `sudo /usr/local/sbin/center-messenger-gitverse-deploy <sha>` передаёт бандл
   проверенному контроллеру, который делает резервную копию, применяет миграции и
   переключает активную версию.

Запросы на слияние проверяются отдельным заданием: оно собирает образ
`center-messenger:pr-<sha12>`, проверяет приложение и граф миграций и удаляет
образ за собой, ничего не публикуя. Публикация идёт только по push в `main`.

Сборка на сервере сайта не является штатным способом обновления. Откат
руководствуется совместимостью схемы: `docker compose down --volumes` в
production запрещён.

## Обслуживание сервера

На VPS ежедневно в 04:17 запускается `/usr/local/sbin/center-messenger-prune-images`
из `scripts/prune_images.sh`: он удаляет устаревшие образы `center-messenger:gitverse-*`,
оставляя активный и предыдущий, снимает образы проверок запросов на слияние и
ограничивает кэш сборщика. Если в этот момент идёт сборка, шаг пропускается.
Контейнеры, том базы, `.secrets` и каталоги релизов контроллера скрипт не трогает.

## Документация

- [Размещение](docs/deployment.md)
- [Контроллер обновлений](docs/autodeploy.md)
- [HTTPS](docs/https.md)
- [Аккаунты](docs/accounts.md)
- [Подключение AlfaCRM](docs/alfa-connection.md)
- [Справочник CRM](docs/crm-directory.md)
- [Бот MAX](docs/max-bot.md)
