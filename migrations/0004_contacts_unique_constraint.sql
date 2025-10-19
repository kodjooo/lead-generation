DROP INDEX IF EXISTS uidx_contacts_value_type;

ALTER TABLE contacts
    ADD CONSTRAINT uidx_contacts_value_type UNIQUE (contact_type, value);
