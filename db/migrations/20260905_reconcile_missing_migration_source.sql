-- 운영 원장에는 기록됐지만 저장소 원본이 유실된 2026-08-28 FK 핫픽스의 checksum을
-- 복원한 재현 파일과 맞춘다. 구 checksum이 정확히 일치하거나 이미 새 값인 경우만 통과한다.
BEGIN;

UPDATE public.schema_migrations
   SET checksum_sha256 = '009c0dbf51142159dbd6e0dd1fa4053714e3094fdb73cf53b237650579e1b1bc'
 WHERE migration_name = '20260828_fix_contract_items_fk_setnull.sql'
   AND checksum_sha256 = 'fcbdf3ceb747281cdb4a7a6a025fdd3f88b7c1cc4477fed1a3130b79486a96aa';

DO $$
DECLARE
  v_observed text;
BEGIN
  SELECT checksum_sha256
    INTO v_observed
    FROM public.schema_migrations
   WHERE migration_name = '20260828_fix_contract_items_fk_setnull.sql';

  IF v_observed IS DISTINCT FROM
     '009c0dbf51142159dbd6e0dd1fa4053714e3094fdb73cf53b237650579e1b1bc' THEN
    RAISE EXCEPTION
      'migration ledger mismatch for 20260828_fix_contract_items_fk_setnull.sql, observed %',
      COALESCE(v_observed, '<missing>');
  END IF;
END $$;

COMMIT;
