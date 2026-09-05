-- 2026-08-28 운영 핫픽스의 재현 가능한 원본.
-- 복합 FK 삭제 시 NOT NULL user_id까지 NULL로 만들지 않고 source_message_id만 NULL 처리한다.
BEGIN;

DO $$
DECLARE
  v_definition text;
  v_expected constant text :=
    'FOREIGN KEY (user_id, source_message_id) REFERENCES messages(user_id, id) ON DELETE SET NULL (source_message_id)';
BEGIN
  SELECT pg_get_constraintdef(oid)
    INTO v_definition
    FROM pg_constraint
   WHERE conrelid = 'public.user_interaction_contract_items'::regclass
     AND conname = 'user_interaction_contract_items_user_id_source_message_id_fkey';

  IF v_definition IS DISTINCT FROM v_expected THEN
    ALTER TABLE public.user_interaction_contract_items
      DROP CONSTRAINT IF EXISTS user_interaction_contract_items_user_id_source_message_id_fkey;
    ALTER TABLE public.user_interaction_contract_items
      ADD CONSTRAINT user_interaction_contract_items_user_id_source_message_id_fkey
      FOREIGN KEY (user_id, source_message_id)
      REFERENCES public.messages(user_id, id)
      ON DELETE SET NULL (source_message_id);
  END IF;

  SELECT pg_get_constraintdef(oid)
    INTO v_definition
    FROM pg_constraint
   WHERE conrelid = 'public.user_interaction_contract_items'::regclass
     AND conname = 'user_interaction_contract_items_user_id_source_message_id_fkey';

  IF v_definition IS DISTINCT FROM v_expected THEN
    RAISE EXCEPTION 'unexpected contract item source FK: %', COALESCE(v_definition, '<missing>');
  END IF;
END $$;

COMMIT;
