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

### Google Sheets

- `GOOGLE_SHEET_ID` — идентификатор таблицы с листом `NICHES_INPUT` (из URL вида `https://docs.google.com/spreadsheets/d/<ID>/...`).
- `GOOGLE_SHEET_TAB` — имя вкладки (по умолчанию `NICHES_INPUT`).
- `GOOGLE_SA_KEY_FILE` / `GOOGLE_SA_KEY_JSON` — ключ сервисного аккаунта Google с доступом на чтение/редактирование таблицы.
- `SHEET_SYNC_ENABLED` — включает автоматическую синхронизацию (true/false).
- `SHEET_SYNC_INTERVAL_MINUTES` — период автосинхронизации (мин., по умолчанию 60).
- `SHEET_SYNC_BATCH_TAG` — опциональный фильтр по партии.

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
