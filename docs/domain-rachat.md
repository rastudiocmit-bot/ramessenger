# Переезд на rachat.ru

Мессенджер переезжает с `messenger.rastudio.org` на `https://rachat.ru`
(`www.rachat.ru` перенаправляется на `rachat.ru`). Сервер прежний, `83.222.25.109`.
Сайт `rastudio.org` не затрагивается.

Код готов заранее и ничего не меняет до шага 4: адрес мессенджера задаётся
переменной `CM_PUBLIC_URL` в `/opt/center-messenger-github/.env`, а проверки
автодеплоя берут адрес оттуда же.

## 1. DNS

A-записи `rachat.ru` и `www.rachat.ru` → `83.222.25.109`. Проверка:

```bash
getent ahostsv4 rachat.ru; getent ahostsv4 www.rachat.ru
```

## 2. Сертификат

```bash
cd /tmp && gh api -H "Accept: application/vnd.github.raw" repos/rastudiocmit-bot/ramessenger/contents/infra/nginx/rachat.bootstrap.conf.example > rachat.bootstrap.conf
test ! -e /etc/nginx/sites-available/center-messenger-rachat && install -m 644 rachat.bootstrap.conf /etc/nginx/sites-available/center-messenger-rachat
ln -s /etc/nginx/sites-available/center-messenger-rachat /etc/nginx/sites-enabled/center-messenger-rachat
nginx -t 2>&1 && systemctl reload nginx
certbot certonly --webroot --webroot-path /var/www/center-messenger-acme --cert-name rachat.ru -d rachat.ru -d www.rachat.ru --non-interactive --deploy-hook 'nginx -t && systemctl reload nginx' 2>&1
```

## 3. Nginx для rachat.ru

```bash
cd /tmp && gh api -H "Accept: application/vnd.github.raw" repos/rastudiocmit-bot/ramessenger/contents/infra/nginx/rachat.conf.example > rachat.conf
install -m 644 rachat.conf /etc/nginx/sites-available/center-messenger-rachat
nginx -t 2>&1 && systemctl reload nginx
curl --fail --silent --show-error --max-time 15 https://rachat.ru/health/ready
```

Ожидается `{"status":"ready"}`. Мессенджер уже открывается на обоих адресах, но
работает (отправка, сохранение) пока только на старом.

## 4. Переключение адреса

```bash
cd /opt/center-messenger-github
grep -q '^CM_PUBLIC_URL=' .env 2>/dev/null && sed -i 's|^CM_PUBLIC_URL=.*|CM_PUBLIC_URL=https://rachat.ru|' .env || echo 'CM_PUBLIC_URL=https://rachat.ru' >> .env
CM_IMAGE=$(docker inspect -f '{{.Config.Image}}' center-messenger-api-1) docker compose -p center-messenger -f compose.yaml up -d --no-deps --no-build --force-recreate api
sleep 20; curl --fail --silent --show-error --max-time 15 https://rachat.ru/api/system/info
```

С этого момента приглашения, cookie входа и адрес MAX-бота — на `rachat.ru`.
Сотрудникам нужно войти заново на новом адресе (вход привязан к домену).

## 5. Старый адрес → новый

```bash
cp /etc/nginx/sites-available/center-messenger /root/center-messenger.nginx.before-rachat
cd /tmp && gh api -H "Accept: application/vnd.github.raw" repos/rastudiocmit-bot/ramessenger/contents/infra/nginx/messenger.moved.conf.example > moved.conf
install -m 644 moved.conf /etc/nginx/sites-available/center-messenger
nginx -t 2>&1 && systemctl reload nginx
curl -sI https://messenger.rastudio.org/ | head -3
```

Ожидается `301` на `https://rachat.ru/`. Вебхуки MAX-бота на старом адресе
продолжают работать, пока не выполнен шаг 6.

## 6. MAX-бот

Владелец: «Подключения» → MAX-бот → «Включить приём». Мессенджер подписывает
бота на `https://rachat.ru/hooks/max/…` и удаляет подписку этого же подключения
на старом адресе (`CM_LEGACY_PUBLIC_URLS`, по умолчанию
`https://messenger.rastudio.org`). Чужие подписки бота не трогаются.

## Откат

Шаги 4–5 обратимы: вернуть `CM_PUBLIC_URL=https://messenger.rastudio.org` в `.env`,
пересоздать `api` той же командой, восстановить
`/root/center-messenger.nginx.before-rachat`, `nginx -t && systemctl reload nginx`.
