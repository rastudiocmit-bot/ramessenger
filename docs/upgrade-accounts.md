# Обновление установленной основы до кабинета

Процедура относится к установленному пакету `39050368387` в
`/opt/center-messenger` и новому проверенному пакету этапа `accounts`.
Применение — вручную по шагам после успешного CI. Файлы сайта, Nginx и PM2
обновление не меняет. Примерные `RUN_ID` и пути ниже заменить конкретным успешным
запуском; не запускать буквально с незаменёнными обозначениями.

## 1. Получить отдельный пакет

Создать новый каталог 0700, скачать artifact `center-messenger-linux-amd64` через
уже авторизованный `gh`, проверить внешнюю сумму и распаковать **в новый каталог**.
Ничего не распаковывать поверх `/opt/center-messenger`.

```bash
mkdir -m 700 /opt/center-messenger-download-RUN_ID
gh run download RUN_ID --repo capitalbtl-lab/center-messenger --name center-messenger-linux-amd64 --dir /opt/center-messenger-download-RUN_ID
cd /opt/center-messenger-download-RUN_ID
sha256sum -c center-messenger-linux-amd64.tgz.sha256
mkdir -m 700 /opt/center-messenger-accounts-RUN_ID
tar -xzf center-messenger-linux-amd64.tgz -C /opt/center-messenger-accounts-RUN_ID --strip-components=1
cd /opt/center-messenger-accounts-RUN_ID
python3 scripts/load_image.py
```

Загрузчик нового пакета совместим с Docker 29/containerd. Старый загрузчик из
исходного каталога повторно запускать не требуется. Эта команда только проверяет
и импортирует образ, не запускает сервисы.

Проверить новый `release.json`: stage `accounts`, правильный source commit,
платформа `linux/amd64`. Digest PostgreSQL должен совпасть с установленным
`/opt/center-messenger/release.json`. В этой операции не обновлять сам PostgreSQL.
Проверить, что оба Compose имеют `name: center-messenger`: используется прежняя
база `center-messenger_database`, а не новая пустая база с другим именем.

## 2. Резервная копия перед миграцией

Команды выполнять с действующим проектом, пока старая версия ещё работает.
Каталог с копией должен быть 0700; файлы — 0600. Пример для конкретного пути:

```bash
mkdir -m 700 /opt/center-messenger-backup-RUN_ID
sh -c 'umask 077; docker compose -f /opt/center-messenger/compose.yaml --project-directory /opt/center-messenger exec -T db pg_dump -U cm_owner -d center_messenger -Fc > /opt/center-messenger-backup-RUN_ID/database.dump'
sh -c 'umask 077; docker compose -f /opt/center-messenger/compose.yaml --project-directory /opt/center-messenger exec -T db pg_restore --list < /opt/center-messenger-backup-RUN_ID/database.dump > /opt/center-messenger-backup-RUN_ID/database.list'
cp -a /opt/center-messenger/.secrets /opt/center-messenger-backup-RUN_ID/
cp -a /opt/center-messenger/.env /opt/center-messenger/release.json /opt/center-messenger/compose.yaml /opt/center-messenger-backup-RUN_ID/
```

Проверить коды завершения dump и `pg_restore --list`, размер копии и наличие
таблиц в списке. Проверка списка не заменяет полное восстановление в отдельный
кластер. До загрузки клиентских данных нужны внешний backup и фактический
restore-тест; этот шаг не подтверждает RPO/PITR или восстановление после утраты VPS.

## 3. Сохранить действующие пароли

Из нового каталога, в котором ещё нет `.secrets`:

```bash
cp -a /opt/center-messenger/.secrets .secrets
python3 scripts/init_secrets.py
docker compose config --quiet
```

Два действующих пароля не меняются; создаётся только новый одноразовый код
владельца. Не копировать `.env` старого пакета поверх нового: она выберет старый
образ. Для пилота URL по умолчанию — `https://messenger.rastudio.org`.
Для другого домена нужно явно задать `CM_PUBLIC_URL` до запуска.

## 4. Миграция и замена API

Из нового каталога, после проверки резервной копии и доступности текущей БД:

```bash
docker compose run --rm --no-deps migrate
docker compose up -d --no-deps --wait api
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8100/health/ready
curl --fail --silent --show-error --max-time 15 https://messenger.rastudio.org/api/system/info
curl --silent --show-error --location --output /dev/null --write-out 'HTTP %{http_code}\n' --max-time 15 https://rastudio.org
```

`--no-deps` сохраняет существующий контейнер БД. Миграция `0002_accounts`
добавляет таблицы учётных записей и политики; контакты/дети/диалоги не удаляются.
Переход с данными foundation проверяется интеграционным тестом. Пока миграция
завершена, а старый API ещё не заменён, его readiness временно будет 503 из-за
проверки версии схемы. В этот момент продолжить запланированную замену API.

Ожидается ready, версия `0.2.0`, stage `accounts`, сайт студии HTTP 200.
Затем открыть `https://messenger.rastudio.org/`, создать владельца и проверить
выход/повторный вход. Код брать из **нового** каталога `.secrets`; не присылать его
в переписку. Старый каталог и backup сохранить. Дальнейшие Compose-команды
выполнять из нового каталога, который теперь является активным.

## Если шаг завершился ошибкой

При ошибке проверки пакета или dump остановиться до миграции. При ошибке миграции
сохранить её вывод и проверить текущую ревизию; не пересоздавать базу и не менять
пароли. Миграции выполняются транзакционно.

Старый API ожидает `0001_foundation`, поэтому переключение одного только образа
после миграции не является исправным откатом. `alembic downgrade` удаляет новые
аккаунты, приглашения и сессии. До первого входа такой возврат можно планировать
отдельно, после появления данных нужен согласованный restore-план и проверенная
копия. Автоматического разрушительного отката нет. Никогда не использовать
`down --volumes`, глобальный Docker prune или удаление тома рабочей базы.
