# Голосовой ввод и AI-агенты (миграция `0013_ai_voice`)

## Что появилось

- **Микрофон в чате.** На компьютере — кнопка слева от «Отправить». На телефоне,
  пока поле пустое, круглая кнопка отправки становится микрофоном. Запись до
  30 секунд → Yandex SpeechKit → AI-агент → текст попадает в поле ввода, клиенту
  ничего не уходит, пока сотрудник сам не нажмёт «Отправить».
- **Подключения → Настройки сервисов → Голос и AI.** Ключ сервисного аккаунта
  Yandex Cloud и ID каталога (ключ хранится зашифрованным в `ConnectorVault`,
  в ответах API не возвращается), месячный лимит расходов, «Проверить связь».
- **AI-агенты.** Название, задача (пока «Голосовой ввод»), модель
  (YandexGPT Pro 5.1 / Alice AI LLM / YandexGPT 5 Pro), промпт, температура,
  проверка на фразе, история версий. Если модель не ответила или ответ
  непригоден, в поле вставляется распознанный текст без обработки.
- Учёт: таблица `ai_usage` хранит только длительность, токены, стоимость и
  результат — без аудио и без текстов. Аудио на сервере не сохраняется.

Права: настройки и агенты — владелец и администратор; микрофон — все, кто
может писать в чатах (наблюдатель — нет). 60 распознаваний в час на сотрудника.

## Схема

Миграция аддитивная: `ai_settings`, `ai_agents`, `ai_agent_versions`,
`ai_usage`, все с `FORCE ROW LEVEL SECURITY` и политикой `tenant_isolation`.
Ревизия поднята в `database.py`, `release.json`, `scripts/local_build_vps.py`
и `scripts/github_promote_vps.py`.

Автовыкладка с этой версии сама применяет миграции (резервная копия →
`alembic upgrade head` → новый API, при сбое — `downgrade` и прежний API).
Скрипт автовыкладки живёт на сервере, поэтому новую версию скрипта один раз
ставят вручную, а он сразу выкладывает `main`:

```sh
gh api repos/rastudiocmit-bot/ramessenger/contents/scripts/local_build_vps.py \
  -H 'Accept: application/vnd.github.raw' > /tmp/local_build_vps.py \
 && install -m 700 /tmp/local_build_vps.py /opt/center-messenger-github/local_build_vps.py \
 && systemctl start ramessenger-local-build.timer \
 && systemctl start ramessenger-local-build.service; \
 journalctl -u ramessenger-local-build.service -n 30 --no-pager
```

Откат: миграция только добавляет таблицы, поэтому старый образ можно
вернуть после `alembic downgrade 0012_member_sections`
(`docker compose ... run --rm migrate alembic downgrade 0012_member_sections`),
затем `CM_IMAGE="$OLD" ... up -d --no-deps --force-recreate api`.

## Настройка Yandex Cloud

1. В консоли Yandex Cloud: каталог → сервисный аккаунт с ролями
   `ai.speechkit-stt.user` и `ai.languageModels.user`.
2. У аккаунта создать **API-ключ** (область — SpeechKit и Foundation Models).
3. Скопировать ID каталога (`b1g…`).
4. В мессенджере: Подключения → Настройки сервисов → Голос и AI →
   вставить ключ и ID каталога → «Сохранить» → «Проверить связь».
5. Создать агента (промпт по умолчанию уже подставлен), отметить
   «Голосовой ввод в чатах» и сохранить.

Ориентир по цене: распознавание 0,16 ₽ за каждые 15 секунд, нормализация
YandexGPT Pro 5.1 — около 0,3 ₽ за сообщение.
