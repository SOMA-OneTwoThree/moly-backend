-- 적용: PYTHONPATH=. uv run python db/apply.py db/migrations/20260807_schema_migrations_bootstrap.sql --env prod --allow-prod --commit
--
-- 변경 기록표(`schema_migrations`)를 만든다. **운영 전환에서 가장 먼저 적용한다.**
--
-- 왜 따로 필요한가
--   `db/apply.py`는 파일을 실행한 뒤 매번 `public.schema_migrations`를 조회해 기록을 남긴다
--   (apply.py 32~45행). 그런데 그 표를 만드는 문장은 `20260804_zzz_conversational_recall.sql`
--   안에 있고, 그 파일은 적용 목록에서 11번째다. 표가 없으면 **앞의 10개가 전부 실패**하고
--   미리보기(dry-run)조차 같은 경로를 지나 실패한다. 그래서 표를 먼저 만드는 파일이 필요하다.
--
--   이 파일 자체는 apply.py로 적용해도 된다. apply.py는 SQL을 먼저 실행하고 그 뒤에 표를
--   조회하므로, 이 파일이 표를 만든 다음 조회가 이루어진다.
--
-- 컬럼 구성은 dev의 `public.schema_migrations`와 같게 맞췄다(2026-08-07 확인).
--
-- 아래 INSERT는 `20260729`까지 이미 적용된 18개 파일을 기록해 둔다. 안 해두면 다음 사람이
-- "무엇이 적용됐는지" 조사를 처음부터 다시 한다. checksum은 2026-08-07 기준 파일 내용의
-- SHA-256이다. `applied_at`은 실제 적용 시각을 모르므로 이 파일을 돌린 시각이 들어간다.
--
-- 참고: dev의 기록표에는 이 18개가 없다(dev는 20260804부터만 기록). 운영이 더 많은 이력을
-- 갖게 되지만 문제는 없다 — 기록이 있으면 같은 파일을 다시 적용할 때 내용이 바뀌었는지
-- 확인해 주므로 오히려 안전하다.

BEGIN;

CREATE TABLE IF NOT EXISTS public.schema_migrations (
  migration_name  text        NOT NULL PRIMARY KEY,
  checksum_sha256 text        NOT NULL,
  applied_at      timestamptz NOT NULL DEFAULT now(),
  applied_by      text        NOT NULL DEFAULT CURRENT_USER
);

INSERT INTO public.schema_migrations(migration_name, checksum_sha256) VALUES
  ('20260713_appearance_v2_cutover.sql',      '46213f556ce762579db73e754c0bc1557fdf917c9fe0d0e45a86e8d01a1e9201'),
  ('20260713_appearance_v2_expand.sql',       '474c0d623988e61acf78f3800700c60f60b4726c188ce121387193046b4f7f96'),
  ('20260715_diary_welcome_source.sql',       'ca354ce730a73427898c15f453e1565cf10ea7ea59c7458275326b648944721b'),
  ('20260716_products_price_hay_positive.sql','222089465b2f943eef97b5bb6aba40cf6ab37674928a13df55adda06dc11c0b1'),
  ('20260716_routine_weekday_only.sql',       '25d229866e837c9d0e7fa72fd802db97637dc6831a1042576672dc43b1152303'),
  ('20260717_capi_dated_diary.sql',           '07831194a801e95909c9a87f0080a44f1106dc250de7dac14e0b024a5337da6a'),
  ('20260719_hat_glasses_rightside.sql',      '87f72ec0780593ed4fbb3a54613668b7568fe94cb933a2063571c63c4f850b24'),
  ('20260720_feedback.sql',                   '62875d44812a05622197ab5a5f360ffa20dfdf3e00be7949e67797494c70a6cc'),
  ('20260720_products_v2_only.sql',           '76e72f5029a88c6334fbc06fcdfc3104b0421709efa90c51780e403a4f0c1c9c'),
  ('20260724_notify_idempotency.sql',         '8306be2178314dfdfcf945de1dabd2658b72ea9529ce57674ff439ceb90d5ae1'),
  ('20260724_payments_multistore.sql',        '2c64aa1698506dff18461697c633ebaf073b8be44d8e42908aa9b90cb07e5750'),
  ('20260724_products_play_store_id.sql',     'ea4034ed95c23ce530c1ac0d2d535f112e196b011255ac5774875c481748077e'),
  ('20260724_user_devices_android.sql',       '4f619eb24e9d3d257d4c101923b1119a083919caf1504946fee29545d987477f'),
  ('20260727_catalog_i18n.sql',               'f23515c6202c9193207c7159707f827c2ea9e64639516924bd0cce61d5c49dbd'),
  ('20260727_diary_gen_claims.sql',           '738f9b6ed5aec7e881b2099eb20aef993700d6331febe369f939a5ee06779eb3'),
  ('20260728_rc_webhook_consistency.sql',     '09c89e62c22f43e791670cd0d6f30e3b39a8b7e79db25f6986b9c2c9c37c1ad6'),
  ('20260729_diary_source_none.sql',          'cfe756cb995bf1283f2e5d00ccc0fbff904ebc518d989b2cd1b1e47f5ffe7a80'),
  ('20260729_retire_workout_theme.sql',       '966e9f3780aebae0bd6f47b2fc1c5c9d8d66735796da0d92ee60697c55c897a6')
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
