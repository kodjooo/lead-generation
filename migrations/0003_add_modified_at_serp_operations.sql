ALTER TABLE serp_operations
    ADD COLUMN IF NOT EXISTS modified_at TIMESTAMPTZ;

UPDATE serp_operations
SET modified_at = COALESCE(modified_at, completed_at, requested_at, NOW())
WHERE modified_at IS NULL;

ALTER TABLE serp_operations
    ALTER COLUMN modified_at SET DEFAULT NOW();

ALTER TABLE serp_operations
    ALTER COLUMN modified_at SET NOT NULL;
