-- Push markers and evening greeting variant. Apply BEFORE the new image.
-- Nullable columns without defaults: no backfill or table rewrite; existing images ignore them.
-- Run db.apply without --commit first, then apply the reviewed hash during the release window.
-- A repeat apply is a no-op only if existing columns have the exact expected shape.
-- Keep routine_morning/routine_risk disabled for the initial two-week observation period.
-- Default rollback: restore the previous image and KEEP these additive columns and their data.
-- Dropping columns is optional later cleanup, not required for this release rollback.
BEGIN;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '60s';
-- Serialize the shape check and ADD against concurrent DDL. Timeout rolls back the entire change.
LOCK TABLE public.user_daily_stats IN ACCESS EXCLUSIVE MODE;
DO $push_columns$
DECLARE
    expected record;
BEGIN
    FOR expected IN
        SELECT * FROM (VALUES
            ('routine_notified_at', 'timestamp with time zone'::regtype),
            ('evening_push_variant', 'text'::regtype)
        ) AS fields(name, type_id)
    LOOP
        IF EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = 'public.user_daily_stats'::regclass
              AND attname = expected.name AND NOT attisdropped
              AND (atttypid <> expected.type_id OR atttypmod <> -1 OR attnotnull OR atthasdef
                   OR attidentity <> '' OR attgenerated <> '')
        ) THEN
            RAISE EXCEPTION 'Unexpected existing push column shape: %', expected.name;
        END IF;
    END LOOP;
END;
$push_columns$;
ALTER TABLE public.user_daily_stats
    ADD COLUMN IF NOT EXISTS routine_notified_at timestamp with time zone,
    ADD COLUMN IF NOT EXISTS evening_push_variant text;
COMMIT;
-- Validate against db/schema_contract.json after apply. No app_config/notification preference is changed.
