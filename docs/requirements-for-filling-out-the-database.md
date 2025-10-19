АЛГОРИТМ СКРИПТА: от минимального ввода (ниша/город) → пачка главных страниц компаний

Версия: 1.0  |  Дата: 2025‑10‑18

Цель
──────────────────────────────────────────────────────────────────────────────
1) Пользователь вносит минимум: ниша (обязательно), город (опц.), страна (опц.), batch_tag (опц.).
2) Скрипт генерирует умные поисковые запросы (без файловых mime:*), пишет их в очередь БД (ваш формат).
3) Ночным deferred‑воркером получает SERP → нормализует URL до главной страницы → дедуплицирует → сохраняет компании.
4) На выходе — «пачка главных страниц» (уникальные домены), готовых к обогащению и персонализации письма.

Google‑таблица (минимальный ввод)
──────────────────────────────────────────────────────────────────────────────
Лист: NICHES_INPUT
• niche        — ОБЯЗАТЕЛЬНО (пример: “стоматология”, “грузоперевозки”).
• city        — опционально (пример: “Москва”). Если пусто — используем страну.
• country     — опционально (пример: “Россия”). Нужна, если city пусто.
• batch_tag   — опционально (произвольная метка партии).

Служебные поля, которые скрипт сам заполнит в этом же листе:
• status, generated_count, db_inserted_count, db_duplicate_count,
  db_first_scheduled_for, db_last_scheduled_for, last_error.

Очередь запросов (ваша текущая БД)
──────────────────────────────────────────────────────────────────────────────
Таблица: search_queue (как у вас сейчас)
id | query_text | query_hash | region_code | is_night_window | status | scheduled_for | created_at | updated_at | last_error | metadata

Заполнение полей:
• query_text      — «lang:ru» + niche + (опц. place) + “триггер” + минус‑домены.
• query_hash      — SHA1(query_text + '|' + region_code).
• region_code     — код lr (город → страна → 225 по умолчанию).
• is_night_window — TRUE.
• status          — 'pending' (для воркера отложенных операций).
• scheduled_for   — равномерно распределить внутри ближайшего ночного окна (UTC).
• created_at/updated_at — NOW(UTC).
• last_error      — NULL.
• metadata        — JSON: {niche, city, country, trigger, batch_tag, language, selection: "strict|balanced"}.

Параметры генерации запросов (минимум шума, без файлов)
──────────────────────────────────────────────────────────────────────────────
• language = "ru" → префикс в query_text: "lang:ru".
• Триггеры по умолчанию (без mime:*):
  - intent_core:  "оставить заявку", "онлайн запись"
  - intent_biz:   "рассчитать стоимость", "коммерческое предложение", "бриф" (для агентств)
• Минус‑домены (шум/агрегаторы): avito.ru, market.yandex.ru, 2gis.ru, hh.ru, flamp.ru,
  otzovik.com, irecommend.ru, youtube.com, vk.com, reddit.com, pikabu.ru, (и др. по желанию).
• Лимит на нишу: 5–6 запросов (ядро + 2–3 триггера).

Построение query_text
──────────────────────────────────────────────────────────────────────────────
Формула:  query_text = "lang:ru " + niche + (" " + place? "") + (" " + trigger? "") + " " + negatives
• place = city, если указали; иначе country; иначе пусто (регион задаём кодом lr).
• trigger — берём по одному из списка (каждый — отдельный запрос).
• negatives — конкатенация "-site:<домен>" для списка шумных доменов.
• Не используем OR в одном запросе; каждый вариант — отдельная строка в очереди.
• Следим за длиной ≤ 400 символов (если больше — обрезать часть минус‑доменов).

Ночное планирование
──────────────────────────────────────────────────────────────────────────────
• NIGHT_WINDOW (UTC): 20:00–05:59 (настраивается).
• spaced scheduling: шаг 45 сек между запросами.
• Если текущая ночь ещё впереди — раскладываем на сегодня; иначе — на следующую ночь.
• Если запросов больше, чем слотов — перенос остатка на следующие ночи.

Воркфлоу по шагам
──────────────────────────────────────────────────────────────────────────────
Шаг A. Загрузка ввода из Google Sheets
  1) Читать NICHES_INPUT со статусом пусто/NEW.
  2) Нормализовать niche/city/country; определить lr:
     city→lr  |  иначе country→lr  |  иначе 225.
  3) Для каждой строки построить 4–6 запросов (см. триггеры) и записать в search_queue:
     UPSERT по query_hash (ON CONFLICT DO NOTHING).
  4) Обновить в листе counters: generated_count/inserted/duplicates, status=EXPORTED, либо FAILED и last_error.

Шаг B. Создание отложенных операций в Яндекс (ночью)
  1) Выбрать из search_queue: status='pending' AND scheduled_for<=NOW() AND is_night_window=TRUE.
  2) Для каждого записи создать deferred‑операцию (Yandex Search API v2 /web/searchAsync), сохранив operation_id
     (рекомендуется отдельная таблица search_ops: id, query_hash, operation_id, submitted_at, status).
  3) Перевести статус очереди в 'submitted' (или оставлять 'pending' — на ваш выбор, важно не запускать повторно).

Шаг C. Опрос и загрузка SERP → нормализация до главной
  1) Периодически опрашивать операции; когда done=true — забирать rawData (XML/HTML).
  2) Извлечь из результатов URL и host, игнорировать агрегаторы/мусор на своей стороне.
  3) Нормализовать URL до главной страницы домена:
     - выбрать схему https;
     - убрать путь/параметры/якорь;
     - удалить www.;
     - punycode/IDN → единый формат;
     - выполнить HEAD с follow‑redirect (по желанию) и зафиксировать канонический хост.
  4) Дедуплицировать по домену (host_norm).

Шаг D. Сохранение компаний (итог — пачка главных страниц)
  • Рекомендую минимальную таблицу companies:
    companies(id, canonical_domain UNIQUE, website_url, first_seen_at, last_seen_at, source)
  • При первом появлении домена — INSERT; при повторном — UPDATE last_seen_at.
  • Экспорт «пачки главных страниц» → CSV/таблица для последующих шагов (обогащение, письмо).

Идемпотентность и статусы
──────────────────────────────────────────────────────────────────────────────
• Очередь: UNIQUE(query_hash); при повторном запуске дублей не будет.
• Операции: хранить operation_id по query_hash (чтобы не создавать повторно).
• Результаты: дедуп по canonical_domain (UNIQUE).
• Рекомендуемые статусы search_queue: pending → submitted → ready | failed.

Псевдокод (упрощённый)
──────────────────────────────────────────────────────────────────────────────
for row in sheets.NICHES_INPUT where status in ('', 'NEW'):
    lr = resolve_lr(row.city, row.country, fallback=225)
    expansions = ["", '"оставить заявку"', '"онлайн запись"', '"рассчитать стоимость"', '"коммерческое предложение"', '"бриф"'][:6]
    for exp in expansions:
        qtext = build_query("lang:ru", row.niche, row.city or row.country, exp, NEG_SITES)
        h = sha1(qtext + '|' + str(lr))
        sf = next_night_slot()
        upsert(search_queue, {query_text:qtext, query_hash:h, region_code:lr, is_night_window:true, status:'pending', scheduled_for:sf, metadata:{...}})
update_sheet_counters(...)

# Ночью: создать операции, потом забрать результаты, нормализовать, дедуп, сохранить companies.

Минимальные DDL (рекомендуемые дополнения)
──────────────────────────────────────────────────────────────────────────────
-- Таблица для отложенных операций (опционально, но удобно)
CREATE TABLE IF NOT EXISTS search_ops(
  id BIGSERIAL PRIMARY KEY,
  query_hash CHAR(40) NOT NULL,
  operation_id TEXT NOT NULL,
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL DEFAULT 'submitted'
);

-- Итоговые компании (главные страницы)
CREATE TABLE IF NOT EXISTS companies(
  id BIGSERIAL PRIMARY KEY,
  canonical_domain TEXT NOT NULL UNIQUE,
  website_url TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'yandex_search_api',
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

Примечания
──────────────────────────────────────────────────────────────────────────────
• В этой версии по умолчанию отключены файловые операторы mime:*. Цель — собрать доменные сайты.
• Город в тексте запроса опционален; регион передаём кодом lr на уровне API/БД.
• Минус‑домены в текст запроса не добавляем — они фильтруются при сохранении результатов (см. `excluded_domains`).
• Контакты собираются только с `https://<canonical_domain>`: ищем `mailto:`/`tel:` и текстовые совпадения, но телефоны сохраняем, лишь если приводятся к `+7XXXXXXXXXX` или `8XXXXXXXXXX`; первичными считаются только контакты из ссылок.
• Главная страница сохраняется без тегов (до 40 000 символов) в `homepage_excerpt`, чтобы использовать контент при генерации писем.
• Если нужно — легко включить «нишевые» расширения (например, “замер/расчёт” для окон/ремонта) через небольшой словарь правил.

DEFAULTS_min_no_files

{
  "language": "ru",
  "night_window": {
    "start_utc": "20:00",
    "end_utc": "05:59"
  },
  "spacing_seconds": 45,
  "region_fallback_lr": 225,
  "max_queries_per_niche": 1,
  "triggers": [],
  "excluded_domains": [
    "avito.ru",
    "market.yandex.ru",
    "2gis.ru",
    "hh.ru",
    "flamp.ru",
    "otzovik.com",
    "irecommend.ru",
    "youtube.com",
    "vk.com",
    "reddit.com",
    "pikabu.ru"
  ],
  "regions_lr": {
    "Россия": 225,
    "Москва": 213,
    "Санкт‑Петербург": 2,
    "Новосибирск": 65
  }
}
