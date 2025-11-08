-- Очистка рассылки от невалидных адресов (телефоны, пустые строки, mailto без домена)
UPDATE outreach_messages
SET status = 'skipped',
    last_error = 'invalid_email',
    metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('reason', 'invalid_email')
WHERE status IN ('scheduled', 'failed')
  AND (
        metadata ->> 'to_email' IS NULL
        OR metadata ->> 'to_email' !~ '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'
        OR metadata ->> 'to_email' ~ '^[+]?\\d+$'
      );

-- Удаляем «email»-контакты, где в значение попал телефон или пустая строка
DELETE FROM contacts
WHERE contact_type = 'email'
  AND (
        value IS NULL
        OR value NOT LIKE '%@%'
      );
