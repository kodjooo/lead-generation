# Lead Generation Pipeline

Проект автоматизирует поиск компаний через Yandex Search API, обогащение контактами и персонализированную рассылку писем. Вся инфраструктура ориентирована на работу в Docker.

## Структура проекта

- `app/` — исходный код служб (`main`, `scheduler`, `worker`).
- `docs/` — требования, архитектура, план внедрения.
- `Dockerfile` — базовый образ Python 3.12.
- `docker-compose.yml` — оркестрация сервисов (`app`, `scheduler`, `worker`, `db`, `redis`).
- `.env`, `.env.example` — переменные окружения (секреты не коммитим, `.env` добавлен в `.gitignore`).

## Требования

- Docker 24.0+ и docker compose plugin
- Возможность открыть исходящие соединения на `smtp.gmail.com:587` (или другой SMTP)
- Доступ к Yandex Search API и Google Sheets

## Подготовка окружения

```bash
cp .env.example .env  # заполните значения согласно комментариям
docker compose pull   # заранее загрузить базовые образы
```

После заполнения `.env` примените миграции (см. раздел «Миграции БД») и запустите compose.

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

> Redis по умолчанию доступен только внутри сети docker compose. Это позволяет запускать стек на серверах, где уже установлен системный Redis (нет конфликта портов на 6379). Если нужен доступ с хоста, создайте `docker-compose.override.yml` и добавьте в нём `ports` для сервиса `redis`, например:

```yaml
services:
  redis:
    ports:
      - "6379:6379"
```

## Как работает пайплайн

1. **Подготовка данных.** Ниши, города и страны заносятся в таблицу Google Sheets (`NICHES_INPUT`). Сервис `SheetSyncService` (CLI `python -m app.tools.sync_sheet` или автосинхронизация) превращает каждую строку в набор поисковых запросов через `QueryGenerator`: выбирается регион (`lr`), рассчитывается ночное окно и время запуска, формируется `serp_queries` с метаданными и хэшами для идемпотентности. Итоги синхронизации фиксируются в листе и таблице `search_batch_logs`.
2. **Планирование и Yandex Search.** Контейнер `scheduler` берёт pending-запросы, проверяет ночное окно и квоты, затем через `YandexDeferredClient` создаёт deferred-операции (таблица `serp_operations`). Клиент автоматически обновляет IAM токен, следит за rate-limit и при необходимости откладывает выполнение. В течение ночи `scheduler` и `app` опрашивают операции (`get_operation`), пока не получат Base64-выдачу.
3. **Парсинг SERP.** Когда операция завершена, `SerpIngestService` декодирует XML, нормализует URL/домены, фильтрует запрещённые домены и записывает документы в `serp_results`. Для каждого домена создаётся или обновляется запись в `companies` (атрибуты и таймштампы обновляются, источник помечается как `yandex_serp`).
4. **Дедупликация компаний.** `DeduplicationService` перебирает `companies`, пересчитывает `dedupe_hash` (по имени и домену), помечает первичные записи и закрывает дубликаты. Дубликатам присваивается статус `duplicate` и `opt_out=True`, чтобы исключить их из дальнейшего пайплайна.
5. **Обогащение контактов.** Воркер `worker` и оркестратор выбирают компании без контактов. `ContactEnricher` строит список страниц (`/`, `/contact`, `/about` и др.), скачивает HTML, сохраняет текстовый фрагмент главной страницы в `companies.attributes.homepage_excerpt`, извлекает только первый найденный `mailto` email и записывает его в `contacts` как основной.
6. **Генерация писем.** Для каждого email без рассылки оркестратор собирает `CompanyBrief` и `OfferBrief`, затем `EmailGenerator` вызывает OpenAI Chat Completions (`gpt-4.1-mini`). При отсутствии ключа или ошибке возвращается предсказуемый fallback-шаблон. Ответ парсится по JSON-схеме и возвращает пару `subject`/`body`.
8. **Доставка.** Во время рабочего окна сервис выбирает `scheduled` записи с просроченным `scheduled_for`, проверяет opt-out и отправляет письмо через SMTP (`EmailSender.deliver`). Статус меняется на `sent`/`failed`/`skipped`, пишутся `sent_at`, `message_id`, `last_error`. Повторы исключены: отбор идёт с блокировкой строк (SKIP LOCKED).
9. **Статусы компаний.** После обхода контактов компания получает `contacts_ready`. Если email не найден, записывается `contacts_not_found`, и оркестратор её больше не обрабатывает.

## Локальный запуск

1. **Подготовьте окружение:**
   ```bash
   cp .env.example .env
   # Заполните .env значениями для Yandex, Google, SMTP, OPENAI
   docker compose pull
   ```
2. **Примените миграции:**
   ```bash
   docker compose up -d db
   docker compose exec db sh -c 'for f in /app/migrations/000*.sql; do psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$f"; done'
   ```
3. **Запустите сервисы:**
   ```bash
   docker compose up --build
   ```
7. **Очередь рассылки.** `EmailSender.queue` сохраняет результат генерации в `outreach_messages` со статусом `scheduled`, добавляя случайную задержку 4–8 минут и гарантируя, что `scheduled_for` попадает в окно 09:10–19:45 по МСК. Email и JSON-запрос LLM кладутся в `metadata`, чтобы можно было восстановить, что именно отправляется.

## Полезные команды

- Запуск синхронизации Google Sheets вручную:
  ```bash
  docker compose run --rm app python -m app.tools.sync_sheet --batch-tag <tag>
  ```
- Просмотр очереди писем:
  ```bash
  docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "SELECT id, scheduled_for, status FROM outreach_messages ORDER BY scheduled_for LIMIT 20;"
  ```
- Перепланировать очередь с задержкой (пример интерактивного скрипта):
  ```bash
  docker compose run --rm app python - <<'PY'
  import random
  from datetime import datetime, timedelta, timezone
  from zoneinfo import ZoneInfo
  from sqlalchemy import text
  from app.modules.send_email import EmailSender
  from app.modules.utils.db import get_session_factory

  sender = EmailSender()
  tz = ZoneInfo(sender.timezone_name)
  current = datetime.now(tz)
  session = get_session_factory()()
  rows = session.execute(text("SELECT id FROM outreach_messages WHERE status='scheduled' ORDER BY created_at"))
  for row in rows.mappings():
      current += timedelta(seconds=random.randint(240, 480))
      session.execute(text("UPDATE outreach_messages SET scheduled_for = :ts WHERE id = :id"),
                      {"ts": current.astimezone(timezone.utc), "id": row["id"]})
  session.commit()
  PY
  ```
- Переотправка письма вручную:
  ```bash
  docker compose run --rm app python - <<'PY'
  from app.modules.send_email import EmailSender
  sender = EmailSender()
  sender.deliver(
      outreach_id="<uuid>",
      company_id="<company uuid>",
      contact_id="<contact uuid>",
      to_email="test@example.com",
      subject="Тест",
      body="Тестовое письмо"
  )
  PY
  ```

## Деплой на удалённом сервере через Git

1. **Подготовка сервера:** установите Docker и docker compose plugin, создайте отдельного пользователя без root.
2. **Клонируйте репозиторий:**
   ```bash
   git clone https://github.com/kodjooo/lead-generation.git
   cd lead-generation
   cp .env.example .env
   ```
3. **Заполните `.env`:** пропишите ключи Yandex и Google, параметры SMTP (пароль приложения берите в кавычках), установите `EMAIL_SENDING_ENABLED=true`.
4. **Разместите ключи сервисных аккаунтов:** скопируйте файлы JSON в каталог `secure/` на сервере. Если файла нет (`secure/authorized_key.json`), Docker создаст директорию с таким именем, и сервисы завершатся ошибкой `IsADirectoryError`.
5. **Примените миграции:**
   ```bash
   docker compose up -d db
   docker compose exec db sh -c 'for f in /app/migrations/000*.sql; do psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$f"; done'
   ```

   Если вызываете команду прямо в терминале сервера и переменные окружения недоступны, подставьте значения явно (по умолчанию `leadgen`):

   ```bash
   for f in migrations/000*.sql; do
   docker compose exec -T db psql -U leadgen -d leadgen < "$f"
   done
   ```
   
6. **Запустите сервисы:**
   ```bash
   docker compose up -d --build
   ```
   Хостовой Redis останавливать не нужно: контейнерный Redis работает только внутри сети compose и не занимает порт `6379` на сервере.
7. **Обновление:**
   ```bash
   git pull
   docker compose up -d --build
   ```
   Если есть новые миграции — повторите шаг 4.
8. **Мониторинг:**
   ```bash
   docker compose logs -f app
   docker compose logs -f worker
   ```

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
docker compose exec db sh -c 'for f in /app/migrations/000*.sql; do psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$f"; done'
```

Если вызываете команду прямо в терминале сервера и переменные окружения недоступны, подставьте значения явно (по умолчанию `leadgen`):

```bash
for f in migrations/000*.sql; do
  docker compose exec -T db psql -U leadgen -d leadgen < "$f"
done
```

## Развёртывание на удалённом сервере

См. раздел «Деплой на удалённом сервере через Git» выше — там перечислены все шаги (клонирование репозитория, заполнение `.env`, миграции и запуск). Дополнительно рекомендуется настроить:

- автоматический старт с помощью systemd unit, если сервер перезагружается;
- регулярные бэкапы каталога `pg_data` и файла `.env`;
- централизованный сбор логов (`docker compose logs`, Loki, ELK и т.д.).

## Тестирование

```bash
docker compose run --rm app python -m pytest
```
4. **Проверка рассылки:** убедитесь, что `EMAIL_SENDING_ENABLED=true`, а текущее время попадает в окно 09:10–19:45 (МСК). Для ручного теста можно изменить `scheduled_for` конкретной записи.
