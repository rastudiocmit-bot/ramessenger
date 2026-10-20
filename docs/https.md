# HTTPS для первого пилота

Целевой домен — `messenger.rastudio.org`, сервер — `83.222.25.109`.
API слушает `127.0.0.1:8100`, PostgreSQL не публикует порт на хост.
Команды ниже выполняет администратор сервера после проверки локального readiness.
Конфигурация Nginx и сертификат сайта `rastudio.org` обслуживаются отдельно.

## Новая установка

Эти шаги описывают первоначальное включение домена. Установленный пилот повторно
инициализировать не требуется. Примеры включаются в новые runtime-пакеты; старый
пакет из запуска CI `39050368387` их не содержит. Не менять проверенные файлы
и контрольные суммы уже установленного старого пакета.

1. Создать DNS A-запись `messenger` на IP сервера и проверить разрешение имени:

   ```bash
   getent ahostsv4 messenger.rastudio.org
   ```

2. Подготовить каталог проверки:

   ```bash
   install -d -m 755 /var/www/center-messenger-acme/.well-known/acme-challenge
   ```

3. Для нового домена установить `infra/nginx/messenger.bootstrap.conf.example`
   отдельным файлом `/etc/nginx/sites-available/center-messenger` и создать ссылку
   с тем же именем в `sites-enabled`. Сначала проверить, что оба имени свободны;
   существующую конфигурацию не перезаписывать. Затем выполнить:

   ```bash
   nginx -t 2>&1 && systemctl reload nginx
   ```

4. Поместить файл `center-messenger-check` с содержимым `center-messenger-acme`
   и правами 0644 в каталог проверки. Проверить выдачу через HTTP:

   ```bash
   curl --fail --silent --show-error --max-time 15 http://messenger.rastudio.org/.well-known/acme-challenge/center-messenger-check
   ```

   Ожидается `center-messenger-acme`. В начальной конфигурации остальные пути
   возвращают 503: сертификат ещё не подключён.

5. Выпустить отдельный сертификат через уже работающий Nginx:

   ```bash
   certbot certonly --webroot --webroot-path /var/www/center-messenger-acme --cert-name messenger.rastudio.org -d messenger.rastudio.org --non-interactive --deploy-hook 'nginx -t && systemctl reload nginx' 2>&1
   ```

   Используется существующий ACME-аккаунт. Если его нет, Certbot объяснит,
   каких параметров регистрации недостаёт; регистрация является отдельным шагом.
   Ожидаются `Successfully received certificate` и файлы `fullchain.pem`,
   `privkey.pem` в `/etc/letsencrypt/live/messenger.rastudio.org/`.

6. Сохранить резервную копию начального файла вне `sites-enabled`, затем заменить
   только `/etc/nginx/sites-available/center-messenger` содержимым
   `infra/nginx/messenger.conf.example`. Сертификат должен уже существовать.
   Выполнить `nginx -t 2>&1 && systemctl reload nginx`.

7. Проверять HTTPS отдельной командой после применения конфигурации:

   ```bash
   curl --fail --silent --show-error --max-time 15 https://messenger.rastudio.org/health/ready
   ```

   Ожидается `{"status":"ready"}`. Reload посылает сигнал master-процессу, и смена
   workers занимает время. Если запрос, отправленный сразу после reload, ещё видит
   прежний сертификат, повторить запрос отдельно. Если несовпадение сохраняется,
   проверить подключённые server blocks, DNS и фактически выдаваемый сертификат.
   Проверку TLS оставлять включённой.

8. Проверить сайт студии:

   ```bash
   curl --silent --show-error --location --output /dev/null --write-out 'HTTP %{http_code}\n' --max-time 15 https://rastudio.org
   ```

   Ожидается `HTTP 200`; отдельно убедиться, что Nginx и PM2 активны.

## Продление и WinSCP

HTTP-маршрут `/.well-known/acme-challenge/` остаётся в итоговом файле, остальные
HTTP-запросы перенаправляются на HTTPS. Успешное продление вызывает сохранённый
deploy-hook, который проверяет конфигурацию и выполняет reload.

Проверить расписание автоматического продления:

```bash
systemctl list-timers --all --no-pager 'certbot*'
```

В обычном SSH-терминале тест выполняется командой:

```bash
certbot renew --cert-name messenger.rastudio.org --dry-run --non-interactive --run-deploy-hooks 2>&1
```

В WinSCP длительная команда может потерять канал вывода и завершиться с
`BrokenPipeError`. Запускать тест один раз в фоне, сохранив весь вывод в файл.
Внешние скобки обязательны: WinSCP дописывает к команде свой разделитель,
который несовместим с завершающим `&` без группировки.

```bash
(nohup sh -c 'certbot renew --cert-name messenger.rastudio.org --dry-run --non-interactive --run-deploy-hooks; renew_exit=$?; printf "\nCERTBOT_EXIT_CODE=%s\n" "$renew_exit"; exit "$renew_exit"' > /opt/center-messenger/certbot-renew-test.log 2>&1 < /dev/null &)
```

Новый запуск перезаписывает этот файл и может получить `Another instance of
Certbot is already running`, пока первый продолжает работу. При таком сообщении
проверять процессы и основной журнал; блокировки не удалять и процессы не убивать:

```bash
ps -eo pid,etime,args | grep '[c]ertbot' || true
tail -n 30 /var/log/letsencrypt/letsencrypt.log 2>&1
```

Certbot 2.9.0 может назначать случайную паузу перед неинтерактивным продлением.
В первом успешном тесте она составила 436 секунд; точное ожидание записано
в основной журнал. После окончания прочитать файл результата:

```bash
tail -n 40 /opt/center-messenger/certbot-renew-test.log
```

Критерий успеха — `all simulated renewals succeeded`, отсутствие ошибок deploy-hook
и итоговый код процесса 0. Один код 0 без проверки результата hook недостаточен.
`nginx -t` пишет обычные диагностические сообщения в stderr; поэтому Certbot может
пометить `syntax is ok` / `test is successful` как `WARNING: ... error output`.
Эти строки сами по себе не означают провал теста. При настоящей ошибке hook нужно
разобрать его код завершения и текст. Тест `--dry-run` не заменяет рабочий сертификат
тестовым; `--run-deploy-hooks` проверяет hook с действующим сертификатом.

Примеры передают заголовки прокси, но текущий Uvicorn запущен с
`--no-proxy-headers`. Перед реализацией входа нужно отдельно настроить доверие
к прокси и публичный HTTPS URL для cookies и перенаправлений.

## Источники

- [Certbot: webroot, renew и deploy hooks](https://eff-certbot.readthedocs.io/en/stable/using.html).
- [Nginx: HTTPS](https://nginx.org/en/docs/http/configuring_https_servers.html).
- [Nginx: применение конфигурации](https://nginx.org/en/docs/control.html).
- [WinSCP: длительные команды](https://winscp.net/eng/docs/scriptcommand_call).
- [WinSCP: завершающий амперсанд и запуск из дочерней оболочки](https://winscp.net/forum/viewtopic.php?t=6298).
