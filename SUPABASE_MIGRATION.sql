-- Run once in Supabase SQL Editor for existing deployments.

ALTER TABLE teacher
ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;

UPDATE teacher
SET created_at = NOW()
WHERE created_at IS NULL;

ALTER TABLE teacher
ALTER COLUMN created_at SET DEFAULT NOW();

ALTER TABLE teacher
ALTER COLUMN created_at SET NOT NULL;

-- Existing projects may already have these columns; IF NOT EXISTS keeps this safe.
ALTER TABLE teacher
ADD COLUMN IF NOT EXISTS class_name VARCHAR(30);

ALTER TABLE teacher
ADD COLUMN IF NOT EXISTS section VARCHAR(10);

CREATE INDEX IF NOT EXISTS ix_teacher_class_name ON teacher(class_name);
CREATE INDEX IF NOT EXISTS ix_teacher_section ON teacher(section);
