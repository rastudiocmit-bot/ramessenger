# Подключение GitHub к безопасному автодеплою

**Репозиторий:** `rastudiocmit-bot/ramessenger`  
**Ветка:** `main`  
**Сервер:** `83.222.25.109`  
**Домен:** `messenger.rastudio.org`

## Автовыкладка из `main`

Выкладка выполняется на самом сервере: таймер
`deploy/systemd/ramessenger-local-build.timer` каждые 5 минут запускает уже
используемую ручную сборку `ramessenger-local-build.service`
(`/opt/center-messenger-github/local_build_vps.py`). Входящие порты, SSH-ключи
в GitHub и runner'ы не нужны: сервер сам читает `main`.

Каждый запуск:

1. Узнаёт SHA последнего коммита `main`. Если этот коммит уже установлен —
   выходит, ничего не меняя.
2. Клонирует ровно этот коммит, проверяет синтаксис Python, тест отката,
   неизменность `compose.yaml` и ревизию миграций `0013_ai_voice`.
3. Собирает образ `center-messenger:local-<sha>`, проверяет здоровье БД и API.
4. Делает и проверяет резервную копию PostgreSQL в
   `/opt/center-messenger-github/rollback/`.
5. Пересоздаёт **только** контейнер API, проверяет локальный и публичный
   `/health/ready`, при ошибке возвращает прежний образ.

Коммит, установка которого не удалась, повторно не ставится
(`last_failed_sha`); следующий коммит в `main` будет пробоваться заново.
Изменения схемы БД или `compose.yaml` автоматически **не** выкладываются —
сборка останавливается и ждёт ручной проверки.

**Важно:** после включения каждый push в `main` попадает на рабочий сайт
в течение ~5–10 минут (плюс время сборки). Изменения, которые не готовы
к выкладке, держите в отдельных ветках.

### Включение (один раз, на сервере под root)

```sh
# 1. Проверить, что ручная сборка разрешена и работает
cat /opt/center-messenger-github/local-build-state.json      # должно быть "enabled": true
systemctl cat ramessenger-local-build.service
python3 /opt/center-messenger-github/local_build_vps.py --preview

# 2. Установить и запустить таймер
cat > /etc/systemd/system/ramessenger-local-build.timer <<'EOF'
[Unit]
Description=Deploy new commits from GitHub main to Center Messenger automatically

[Timer]
OnBootSec=3min
OnUnitInactiveSec=5min
RandomizedDelaySec=30
Persistent=false
Unit=ramessenger-local-build.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now ramessenger-local-build.timer
systemctl list-timers ramessenger-local-build.timer --no-pager
```

Не включайте одновременно `ramessenger-github-deploy.timer` или старый
`center-messenger-deploy.timer`: сборка откажется работать при их активности.

### Наблюдение и отключение

```sh
journalctl -u ramessenger-local-build.service -n 50 --no-pager   # LOCAL DEPLOYED / UNCHANGED / BLOCKED
cat /opt/center-messenger-github/local-build-state.json            # last_deployed_sha, last_failed_sha
systemctl disable --now ramessenger-local-build.timer              # выключить автовыкладку
```

Ручная выкладка по-прежнему: `systemctl start ramessenger-local-build.service`.

## Предыстория

Репозиторий выбран и GitHub Actions проверяет код в `.github/workflows/verify.yml`.
Старый контроллер настроен на другой источник
(`устаревший репозиторий (отключён)`, ветка `production`); его нельзя
переиспользовать простой заменой текста. Файл `.gitverse/workflows/ci.yml`
предназначен для GitVerse, а не GitHub. Никаких новых входящих портов,
GitHub self-hosted runner с полным Docker-доступом и SSH-ключей на сайте
автоматически не создаётся.

## Условия безопасного включения

1. Подтвердить актуальное состояние сервера, список запущенных сервисов, Nginx,
   свободные ресурсы и каталог активного мессенджера.
2. Остановить **только** старый таймер автодеплоя после проверки отсутствия
   выполняющейся установки. Сохранить его конфигурацию и журнал.
3. Подготовить отдельный контроллер/учётные данные GitHub с минимальными
   правами чтения **только** `rastudiocmit-bot/ramessenger`. Проверить SHA
   выбранного коммита и артефакты CI. Отключить выбор старых сборок.
4. Выделить отдельный защищённый выпускной ref и предусмотреть ручное подтверждение
   первого развёртывания. Не считать любой push в `main` разрешением на продакшен.
5. Сделать внешнюю зашифрованную копию PostgreSQL и секретов; проверить восстановление.
6. Проверить совместимость миграций и запуск нового образа отдельно; менять
   только API проекта, сохранять том БД и прежний образ для отката.
7. Проверить `127.0.0.1:8100/health/ready`, публичный HTTPS, сайт `rastudio.org`
   и журналы; только после успешной первой публикации включать таймер.

Существующие Nginx, PM2, порт 3000, сайт студии и другие контейнеры
не должны изменяться. **Не использовать `docker compose down --volumes`,
`docker system prune -a` и удаление рабочей базы.**

## Диагностика текущего контроллера (команды только для чтения)

```bash
systemctl status center-messenger-deploy.timer --no-pager
systemctl status center-messenger-deploy.service --no-pager
systemctl list-timers --all --no-pager | grep -i messenger || true
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
ss -lntp | grep -E ':(80|443|8100|3000)\\b' || true
curl --fail --silent --show-error http://127.0.0.1:8100/health/ready
```

Сами команды выше не устанавливают обновлений и не перезапускают службы.

## Дистанционная диагностика VPS из GitHub

В репозитории доступен ручной workflow `.github/workflows/vps-audit.yml`.
Он выполняет **только чтение**, используя `scripts/vps-audit.sh`.
Для него в GitHub Environment `production` потребуются:

- Environment secret `DEPLOY_SSH_KEY`: отдельный приватный SSH-ключ
  пользователя диагностики; никогда не добавлять в исходный код.
- Environment secret `DEPLOY_KNOWN_HOSTS`: заранее проверенная
  `known_hosts` запись именно для `83.222.25.109`. Fingerprint сверить
  по независимому доверенному каналу, а не принимать через `ssh-keyscan`
  без проверки.
- Environment variable `DEPLOY_USER`: существующий пользователь SSH
  с минимальными правами на чтение состояния служб.

Настроить одобрение запуска через GitHub Environment `production`,
предпочтительно с обязательным reviewer. Далее запустить
**Actions → VPS read-only audit → Run workflow**.
Это **не** включает деплой. После успешного аудита отдельно проверить
реальный контроллер на сервере и провести тест восстановления до миграции.
Не выдавать диагностическому ключу root или passwordless sudo;
если Docker не доступен для чтения, проверка Docker вернёт диагностическое
сообщение, но доступ повышать автоматически нельзя.
