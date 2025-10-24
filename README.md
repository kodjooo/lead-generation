# Lead Generation Pipeline

Проект автоматизирует поиск компаний через Yandex Search API, обогащение контактами и персонализированную рассылку писем. Вся инфраструктура ориентирована на работу в Docker.

## Структура проекта

- `app/` — исходный код служб (`main`, `scheduler`, `worker`).
- `docs/` — требования, архитектура, план внедрения.
- `Dockerfile` — базовый образ Python 3.12.
- `docker-compose.yml` — оркестрация сервисов (`app`, `scheduler`, `worker`, `db`, `redis`).
- `.env`, `.env.example` — переменные окружения (секреты не коммитим, `.env` добавлен в `.gitignore`).

## Подготовка окружения

```bash
cp .env.example .env  # заполните значения согласно комментариям
docker compose pull   # заранее загрузить базовые образы
```

## Быстрый старт в Docker Compose

```bash
docker compose up --build
```

Сервисы:
- `app` — оркестратор полного цикла (deferred → дедуп → enrichment → рассылка).
- `scheduler` — постановка deferred-запросов и polling операций.
- `worker` — enrichment контактов и отправка писем.
- `db` — PostgreSQL 16 (storage для пайплайна).
- `redis` — брокер задач/кэш.

## Как работает пайплайн

1. **Подготовка данных.** Ниши, города и страны заносятся в таблицу Google Sheets (`NICHES_INPUT`). Сервис `SheetSyncService` (CLI `python -m app.tools.sync_sheet` или автосинхронизация) превращает каждую строку в набор поисковых запросов через `QueryGenerator`: выбирается регион (`lr`), рассчитывается ночное окно и время запуска, формируется `serp_queries` с метаданными и хэшами для идемпотентности. Итоги синхронизации фиксируются в листе и таблице `search_batch_logs`.
2. **Планирование и Yandex Search.** Контейнер `scheduler` берёт pending-запросы, проверяет ночное окно и квоты, затем через `YandexDeferredClient` создаёт deferred-операции (таблица `serp_operations`). Клиент автоматически обновляет IAM токен, следит за rate-limit и при необходимости откладывает выполнение. В течение ночи `scheduler` и `app` опрашивают операции (`get_operation`), пока не получат Base64-выдачу.
3. **Парсинг SERP.** Когда операция завершена, `SerpIngestService` декодирует XML, нормализует URL/домены, фильтрует запрещённые домены и записывает документы в `serp_results`. Для каждого домена создаётся или обновляется запись в `companies` (атрибуты и таймштампы обновляются, источник помечается как `yandex_serp`).
4. **Дедупликация компаний.** `DeduplicationService` перебирает `companies`, пересчитывает `dedupe_hash` (по имени и домену), помечает первичные записи и закрывает дубликаты. Дубликатам присваивается статус `duplicate` и `opt_out=True`, чтобы исключить их из дальнейшего пайплайна.
5. **Обогащение контактов.** Воркер `worker` и оркестратор выбирают компании без контактов. `ContactEnricher` строит список страниц (`/`, `/contact`, `/about` и др.), скачивает HTML, сохраняет текстовый фрагмент главной страницы в `companies.attributes.homepage_excerpt`, извлекает только первый найденный `mailto` email и записывает его в `contacts` как основной.
6. **Генерация писем.** Для каждого email без рассылки оркестратор собирает `CompanyBrief` и `OfferBrief`, затем `EmailGenerator` вызывает OpenAI Chat Completions (`gpt-4.1-mini`). При отсутствии ключа или ошибке возвращается предсказуемый fallback-шаблон. Ответ парсится по JSON-схеме и возвращает пару `subject`/`body`.
7. **Очередь рассылки.** `EmailSender.queue` сохраняет результат генерации в `outreach_messages` со статусом `scheduled`, добавляя случайную задержку 4–8 минут и гарантируя, что `scheduled_for` попадает в окно 09:10–20:45 по МСК. В `metadata` кладётся email получателя и исходный JSON-запрос к LLM, что позволяет аудитировать письма до фактической отправки и повторно поднимать зависшие сообщения.
8. **Доставка и повторные запуски.** В рабочем окне сервис выбирает `scheduled` записи с `scheduled_for <= NOW()`, снова проверяет opt-out и только потом отправляет письмо через SMTP (`EmailSender.deliver`). Статус обновляется на `sent`/`failed`/`skipped`, добавляются `sent_at`, `message_id`, `last_error`. `app/main.py` и `worker` крутят полный цикл (`scheduled/processed/enriched/queued/sent`), и благодаря идемпотентным upsert перезапуски безопасны.

### Управление оркестратором

Запустить оркестратор однократно:

```bash
docker compose run --rm app --mode once
```

Фоновый режим по умолчанию (`loop`) запускается в контейнерах `app`, `scheduler`, `worker` при `docker compose up`.

## Переменные окружения

### Yandex Cloud

- `YANDEX_CLOUD_FOLDER_ID` — ID каталога (консоль YC → «Обзор»).
- `YANDEX_CLOUD_IAM_TOKEN` — можно оставить пустым; при наличии ключа сервисного аккаунта пайплайн возьмёт токен автоматически.
- `YANDEX_CLOUD_SA_KEY_FILE` / `YANDEX_CLOUD_SA_KEY_JSON` — путь или содержимое ключа сервисного аккаунта. Получить ключ:

  ```bash
  yc iam key create --service-account-name <sa_name> --output key.json
  ```

  Ключ храните в Secret Manager или CI и не коммитьте в репозиторий.
- `YANDEX_ENFORCE_NIGHT_WINDOW` — если `true`, отправка запросов выполняется только в ночное окно; установите `false` для дневных тестов.

### Google Sheets

- `GOOGLE_SHEET_ID` — идентификатор таблицы с листом `NICHES_INPUT` (из URL вида `https://docs.google.com/spreadsheets/d/<ID>/...`).
- `GOOGLE_SHEET_TAB` — имя вкладки (по умолчанию `NICHES_INPUT`).
- `GOOGLE_SA_KEY_FILE` / `GOOGLE_SA_KEY_JSON` — ключ сервисного аккаунта Google с доступом на чтение/редактирование таблицы.
- `SHEET_SYNC_ENABLED` — включает автоматическую синхронизацию (true/false).
- `SHEET_SYNC_INTERVAL_MINUTES` — период автосинхронизации (мин., по умолчанию 60).
- `SHEET_SYNC_BATCH_TAG` — опциональный фильтр по партии.

### Email и OpenAI

- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` — параметры SMTP-провайдера.
- `EMAIL_SENDING_ENABLED` — если `false`, письма только сохраняются в `outreach_messages` со статусом `scheduled`, реальная отправка отключена.
- `OPENAI_API_KEY` — ключ OpenAI для генерации персонализированных писем.

## Синхронизация запросов из Google Sheets

1. Заполните таблицу на листе `NICHES_INPUT` (столбцы `niche`, `city`, `country`, `batch_tag`).
2. Выполните синхронизацию:

   ```bash
   docker compose run --rm app python -m app.tools.sync_sheet
   # или выбрать конкретную партию
   docker compose run --rm app python -m app.tools.sync_sheet --batch-tag batch-2025-10
   ```

   Скрипт создаст записи в `serp_queries` и обновит служебные колонки листа (`status`, `generated_count` и т.д.).

3. При установке `SHEET_SYNC_ENABLED=true` оркестратор автоматически вызывает синхронизацию каждые `SHEET_SYNC_INTERVAL_MINUTES` минут, используя тот же CLI-процесс под капотом.

## Миграции БД

После обновления проекта выполните SQL-миграции:

```bash
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < migrations/0001_init.sql
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < migrations/0002_reporting.sql
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < migrations/0003_add_modified_at_serp_operations.sql
```

## Развёртывание на удалённом сервере

1. Установите Docker и плагин docker compose (например, `apt install docker.io docker-compose-plugin`).
2. Создайте отдельного пользователя без пароля root-доступа, добавьте его в группу `docker`.
3. Выполните `git pull https://github.com/kodjooo/lead-generation.git` в целевой директории и заполните `.env` секретами (используйте `scp`/`sftp` или менеджер секретов).
4. Запустите `docker compose up -d --build` и убедитесь, что все сервисы перешли в состояние `healthy`.
5. Настройте автоматический старт (systemd unit, cron `@reboot` или Docker restart policies уже включены).
6. Организуйте бэкапы каталога `pg_data` (PostgreSQL) и аудит логов (`docker logs` или Loki/ELK).

## Тестирование

```bash
docker compose run --rm app python -m pytest
```
