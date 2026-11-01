# Согласованный выпуск сопровождения семьи

Код подготовлен; публикация и успешная установка — разные состояния.
Фактический VPS в этой среде недоступен. Полный SHA кандидата и SHA-256 новой
policy должны быть указаны в сообщении выпуска, до запуска команды.

## Предметный объём допуска

Существующий `telephony-worker` получает локальный разбор уже загруженных
звонков, закрытый пользовательский API и очередь сопровождения. Новых
платных вызовов этот разбор не делает. MAX worker и его pin сохраняются.

Изменения защищённых существующих файлов:

| Файл | Причина |
| --- | --- |
| `telephony_worker.py` | Регистрация локального care-step, private `/care`, проверка readiness трёх собственных шагов. |
| `telephony_import.py` | При изменившемся факте звонка ставится собственное событие care в той же транзакции. |
| `telephony_archive_import.py` | Аналогичное событие архива с низким приоритетом; ночное окно и интервал ИИ не меняются. |
| `telephony_ai_store.py` | Только после успешного сохранения готовой расшифровки ставится care-событие. |
| `tests/test_telephony_archive_import.py` | Fixture возвращает реальные по форме строки UPSERT; проверяется отдельная низкоприоритетная очередь. Старые проверки сохранены. |
| `tests/run_max_compatibility.py` | Прежние gates сохранены, добавлены family-care, Tasks и PostgreSQL/RLS/атомарность. |

Новые защищённые модули: `phone_care.py`, `phone_care_model.py`,
`phone_care_protocol.py`, `phone_care_worker.py`, `phone_care_bridge.py`,
`phone_care_api.py`; новые обязательные `test_phone_care.py`,
`test_phone_care_database.py`. В policy добавлены их точные SHA-256 и шаблон
`phone_care*.py`, чтобы последующие правки этого контура требовали проверки.

Общий `tasks.py` получает `create_in` для единой транзакции создания задачи
и подтверждения телефонного источника. Обычный `create` использует тот же код;
публичный API не принимает произвольный телефонный источник или ID задачи.
Этот файл и `test_tasks.py` теперь также закреплены.

Новая policy изменяет **только перечисленные хэши и добавления**, список tests
и `telephony_runtime_contract` на `novofon-family-care-v1`. Остальные защищённые
хэши, схема, MAX-контракт и старый application baseline сохраняются.
Это отдельный явно устанавливаемый проверенный комплект, не автоматическое
обновление policy кандидатом. Прежняя policy:
`77d760ec8c4dceb102aced3eceff249f63dabeb1ab289b0a98dbd655b0887ca3`.

## Установка

Используется существующий `scripts/prepare_telephony_update.py`, затем только
`ramessenger-local-build.service`. Не создавать новый runner, не править live
checkout, не удалять lock/approval и не запускать обходной Compose.

1. В отдельный каталог клонировать опубликованную ветку и проверить её полный
   SHA из сообщения выпуска. При несовпадении остановиться.
2. Получить фактические image tags API/MAX/телефонии через `docker inspect`,
   а `CM_BUILD_SHA` API — отдельным чтением одной переменной, без вывода env/секретов.
3. Передать их как `--expected-api`, `--expected-max`, `--expected-telephony`,
   `--expected-live`, а проверенные hashes — как `--expected-policy` и
   `--previous-policy`. Prepare перепроверяет clean checkout, текущий main,
   live SHA/image, закреплённые процессы, Compose, schema и policy под штатным lock.
4. После `TELEPHONY UPDATE ADMITTED` запустить существующую службу. Обязательные
   offline и database suites выполняются перед promotion. Readiness обоих новых
   процессов и неизменность MAX проверяются штатным путём; ошибка откатывается
   через существующий механизм. Не считать queued/start/build успехом выпуска.

## Проверка результата

```bash
systemctl show ramessenger-local-build.service -p ActiveState -p SubState -p Result
cm_care_run="$(systemctl show ramessenger-local-build.service -p InvocationID --value)"
test -n "$cm_care_run" && journalctl "_SYSTEMD_INVOCATION_ID=$cm_care_run" -n 45 --no-pager -o cat
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Health.Status}}' center-messenger-api-1 center-messenger-max-worker-1 center-messenger-telephony-worker-1
curl -fsS http://127.0.0.1:8100/health/ready
```

Нужны `LOCAL DEPLOYED <точный SHA>`, новый API и telephony images healthy,
прежний MAX image, readiness ready, оба suite без skips. После этого открыть
**Телефония → Обращения**. Первичная обработка идёт в фоне; видны её состояние
и время прохода. Создать одну задачу после проверки цитаты и убедиться, что
она одна и открывает исходное обращение. Это проверка пользовательского
действия, не автоматическая отправка клиенту.

Медиашлюз, реальный SIP-разговор, подсказки в прямом эфире и голосовой
ответчик этим выпуском не включаются. Детальный статус всех десяти пунктов:
[сопровождение семьи](../telephony/family-care.md).
