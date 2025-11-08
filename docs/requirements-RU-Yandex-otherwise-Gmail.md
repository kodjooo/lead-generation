ТЗ: Маршрутизация отправки по MX-записям (RU → Яндекс, иначе → Gmail)
Версия: 1.0

Назначение
Добавить перед отправкой письма проверку MX домена адресата и выбрать SMTP-канал:
— если MX относится к российским почтовым провайдерам (Яндекс, Mail.ru, Rambler и т. п.) → отправлять через личный аккаунт Яндекса;
— иначе → отправлять как сейчас, через Gmail.
Остальные части сервиса (поиск, дедупликация, генерация, планирование) — без изменений.

1) Область работ
— Модуль MX-классификации домена получателя + кэш результата.
— Выбор канала перед фактической доставкой письма: yandex | gmail.
— Запись результата MX-проверки и выбранного канала в metadata записи письма (без изменения схемы БД).
— Фолбэк-логика на случай DNS/SMTP ошибок.

2) Точки интеграции
— app/modules/send_email.py (EmailSender.deliver): вызывать MX-классификацию сразу перед установлением SMTP-соединения.
— app/config.py: добавить ENV-параметры для MX-роутинга и Яндекс SMTP.
— (новый) app/modules/mx_router.py: DNS-запрос, кэш, классификация.
— tests/: unit-тесты для mx_router и выбора канала.
Примечание: MX проверяем при доставке (deliver-time), НЕ при постановке в очередь.

3) Критерии готовности (DoD)
— В outreach_messages.metadata сохраняются:
  metadata.mx.class: "RU" | "OTHER" | "UNKNOWN"
  metadata.mx.records: ["mx.yandex.net", ...]  (доменные имена MX)
  metadata.route.provider: "yandex" | "gmail"
— RU → provider=yandex; иначе → provider=gmail.
— DNS timeout/NXDOMAIN → 1 ретрай; если снова ошибка → class=UNKNOWN, provider=gmail.
— Нет/ошибка авторизации Яндекс SMTP → фолбэк на gmail с metadata.route.fallback=true и warning в лог.

4) Конфигурация (.env / config.py)
ROUTING_ENABLED=true
ROUTING_MX_CACHE_TTL_HOURS=168
ROUTING_DNS_TIMEOUT_MS=1500
ROUTING_DNS_RESOLVERS="1.1.1.1,8.8.8.8"

# Подстроки для распознавания российских MX
ROUTING_RU_MX_PATTERNS="mx.yandex.net,mxs.mail.ru,mx1.mail.ru,mxs-cloud.mail.ru,mx.rambler.ru,mxs.rambler.ru"

# Домены-получатели, которые считаем RU без DNS
ROUTING_FORCE_RU_DOMAINS="yandex.ru,yandex.com,mail.ru,bk.ru,inbox.ru,list.ru,rambler.ru"

# Gmail (как сейчас)
GMAIL_SMTP_HOST=smtp.gmail.com
GMAIL_SMTP_PORT=587
GMAIL_SMTP_TLS=true
GMAIL_USER="mark.aborchi@gmail.com"
GMAIL_PASS="***"
GMAIL_FROM="Марк Аборчи <mark.aborchi@gmail.com>"

# Яндекс (личный аккаунт)
YANDEX_SMTP_HOST=smtp.yandex.ru
YANDEX_SMTP_PORT=465
YANDEX_SMTP_SSL=true
YANDEX_USER="mark***@yandex.ru"
YANDEX_PASS="***"          # app-password
YANDEX_FROM="Марк Аборчи <mark***@yandex.ru>"

5) Зависимости
— requirements.txt: dnspython>=2.6
— Кэш: если есть Redis, использовать его; иначе — in-memory LRU+TTL на процесс.

6) Алгоритм
1. Домен = часть после '@' в to_email, в нижнем регистре.
2. Если ROUTING_ENABLED=false → provider=gmail (стандартный путь).
3. Кэш (ключ mx:<domain>): если есть актуальное значение → использовать.
4. Иначе DNS MX lookup (dnspython) с таймаутом и кастомными резолверами.
   4.1 При ошибке — 1 повтор с альтернативным резолвером.
   4.2 При повторной ошибке → class=UNKNOWN, records=[].
5. Классификация:
   — если домен получателя входит в ROUTING_FORCE_RU_DOMAINS → class=RU;
   — иначе если любая MX содержит подстроку из ROUTING_RU_MX_PATTERNS → class=RU;
   — иначе → class=OTHER.
6. Выбор канала: RU → yandex; OTHER|UNKNOWN → gmail.
   Если выбран yandex, но нет валидной авторизации → фолбэк на gmail (metadata.route.fallback=true).
7. Заголовки:
   — yandex: From=YANDEX_FROM; (опционально) Reply-To=GMAIL_FROM;
   — gmail:  From=GMAIL_FROM.
8. Отправка существующим SMTP-кодом (задержки/лимиты — без изменений).
9. В metadata сохранить:
   metadata.mx = {"class": "...", "records": [...], "checked_at": "...iso..."}
   metadata.route = {"provider": "...", "fallback": true|false}
   (Если есть поле channel — синхронизировать с provider.)
10. Логи: domain, mx_class, provider, dns_latency_ms, smtp_provider, error. Email маскировать.

7) Псевдокод
# app/modules/mx_router.py
def classify_domain(domain: str) -> tuple[str, list[str]]:
    if domain in FORCE_RU_DOMAINS: return "RU", []
    cached = cache_get(f"mx:{domain}")
    if cached: return cached["class"], cached["records"]
    try:
        records = dns_mx_lookup(domain, timeout=DNS_TIMEOUT, resolvers=RESOLVERS)
    except DnsError:
        try: records = dns_mx_lookup(domain, timeout=DNS_TIMEOUT, resolvers=ALT_RESOLVERS)
        except DnsError:
            cache_set(f"mx:{domain}", {"class":"UNKNOWN","records":[]}, ttl=CACHE_TTL)
            return "UNKNOWN", []
    hosts = [strip_dot(r.exchange).lower() for r in records]
    mx_class = "RU" if any(sub in h for h in hosts for sub in RU_MX_PATTERNS) else "OTHER"
    cache_set(f"mx:{domain}", {"class": mx_class, "records": hosts}, ttl=CACHE_TTL)
    return mx_class, hosts

# app/modules/send_email.py (перед SMTP connect)
mx_class, mx_records = mx_router.classify_domain(recipient_domain)
provider = "yandex" if mx_class == "RU" else "gmail"
fallback = False
try:
    if provider == "yandex":
        smtp = SmtpClient(YANDEX_SMTP_HOST, YANDEX_SMTP_PORT, ssl=YANDEX_SMTP_SSL, auth=YANDEX_AUTH)
        msg.From = YANDEX_FROM; msg.ReplyTo = GMAIL_FROM
    else:
        smtp = SmtpClient(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, tls=GMAIL_SMTP_TLS, auth=GMAIL_AUTH)
        msg.From = GMAIL_FROM; msg.ReplyTo = None
    smtp.send(msg, to=to_email)
except SmtpAuthError:
    if provider == "yandex":
        # фолбэк на Gmail
        smtp = SmtpClient(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, tls=GMAIL_SMTP_TLS, auth=GMAIL_AUTH)
        msg.From = GMAIL_FROM; msg.ReplyTo = None
        smtp.send(msg, to=to_email)
        fallback = True
    else:
        raise
finally:
    message.metadata["mx"] = {"class": mx_class, "records": mx_records, "checked_at": now_iso()}
    message.metadata["route"] = {"provider": "yandex" if (provider=="yandex" and not fallback) else "gmail",
                                 "fallback": fallback}
    save(message)

8) Тесты (минимум)
— MX: ["mx.yandex.net"] → RU → yandex.
— MX: ["aspmx.l.google.com"] → OTHER → gmail.
— force-domain: user@mail.ru → RU без DNS.
— timeout → retry → timeout → UNKNOWN → gmail.
— SmtpAuthError при yandex → фолбэк на gmail, metadata.route.fallback=true.
— Проверка, что metadata.mx / metadata.route заполняются.

9) Безопасность и логи
— Не логировать секреты. Email маскировать (ma***@domain.ru). TLS/SSL обязателен.
— Логировать только домен получателя, mx_class, provider, время DNS, код SMTP/ошибку.

10) Изменения в репозитории
— requirements.txt: dnspython>=2.6
— app/config.py: чтение ENV из раздела 4
— app/modules/mx_router.py: новый модуль
— app/modules/send_email.py: выбор канала + запись metadata
— tests/test_mx_router.py, tests/test_send_email_routing.py: новые тесты

11) Приёмка
— Ручной прогон 5–10 писем:
   * @yandex.ru / @mail.ru → отправка через Яндекс (From=YANDEX_FROM)
   * @gmail.com / домены с Google MX → через Gmail (From=GMAIL_FROM)
   * адрес с искусственной DNS-ошибкой → class=UNKNOWN, provider=gmail
— В БД у писем заполнены metadata.mx и metadata.route; статусы «sent».