-- 2026-08-05 마이그레이션 7개의 주석 경로 정리로 달라진 checksum을 원장과 맞춘다.
-- 각 파일의 적용 당시 버전과 현재 버전을 대조했으며 SQL 본문은 같고 첫 주석만 다르다.
-- 구 checksum이 정확히 일치할 때만 갱신하고, 이미 새 값이면 그대로 통과한다.
BEGIN;

DO $$
DECLARE
  v record;
  v_observed text;
BEGIN
  FOR v IN
    SELECT *
    FROM (VALUES
      ('20260805_ai_usage_ledger.sql',
       '86f8403b70f5a03866fb9c47192596524bcc86af7b1c6ab8c3397d46f7194829',
       '2c824b2644c9f951d083736708ca8dfc97123bbc9029e99dc58205eeab5a96b6'),
      ('20260805_mem0_v2_collection.sql',
       '9abdfd504b066e6f933ec9df812a0a67aca7d3c23c08e92c843773cfdf24640b',
       'ac03c35b7f998139e9ed27b5f5bf2de24ade58a4c8b8162ad38fb3fd5ca87400'),
      ('20260805_privacy_active_backfill.sql',
       '3d51da38f80afb2e9fe19cac37c377544fbc118bc8672fab045f901ca781cbeb',
       'f652a3c32075a47609e6a36b8cb9e12bdef475c89545d818e060e3685a659739'),
      ('20260805_privacy_epoch.sql',
       'e86bd01ff2152f69db3f17beba340a704eaa8ee7299b7401296b9fcaa5c7af73',
       'e96b0aabb1fc7911b2b8aaa193d5b6312c547babbbd255a3659f831c883d63ba'),
      ('20260805_relationship_render.sql',
       'f70a6546d59eb008a38fa5b42778501933e08edf46fec9f4f54967334d9923b7',
       '0f997598544f73a4084badf1d2977765aef4fe072baa711db338f9ccb7dca370'),
      ('20260805_shadow_prompt_traces.sql',
       '80982e7774d9498c3ad28c77c3e964950a108ef6cc7e54b6f5d00742b9585963',
       'e81c9a629bb7c835e993ea4685815aea6f83e596ba32bac427a48497d810a8db'),
      ('20260805_user_schedules.sql',
       '4b4641593c746afaa313e35f4b6b363b44deaf8bb60300b5a5ac757145a936ea',
       '2cb1e96dd3e5d54b14a97734c704e2ef63973d4465d4536cab59c3ae9e899d12')
    ) AS expected(migration_name, old_checksum, new_checksum)
  LOOP
    UPDATE public.schema_migrations
       SET checksum_sha256 = v.new_checksum
     WHERE migration_name = v.migration_name
       AND checksum_sha256 = v.old_checksum;

    SELECT checksum_sha256
      INTO v_observed
      FROM public.schema_migrations
     WHERE migration_name = v.migration_name;

    IF v_observed IS DISTINCT FROM v.new_checksum THEN
      RAISE EXCEPTION
        'migration ledger mismatch for %, expected %, observed %',
        v.migration_name, v.new_checksum, COALESCE(v_observed, '<missing>');
    END IF;
  END LOOP;
END $$;

COMMIT;
