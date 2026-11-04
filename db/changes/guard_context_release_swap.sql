-- 최초 운영 전환 6/6: 네 검증 완료 뒤 messages 이름만 짧은 TX에서 교체한다.
-- db.apply --statement-timeout-ms 750 사용. 바깥 DO 전에 제한을 설정한다.
-- 원본/새 CHECK가 없거나 검증되지 않았으면 조용히 건너뛰지 않고 중단한다.
LOCK TABLE public.messages IN ACCESS EXCLUSIVE MODE NOWAIT;
DO $guardrails_swap$
DECLARE
    old_constraint record;
    new_constraint record;
    existing_kinds text[];
BEGIN
    SELECT c.contype,c.convalidated,pg_get_constraintdef(c.oid) AS definition
      INTO old_constraint FROM pg_constraint c
      WHERE c.conrelid='public.messages'::regclass AND c.conname='messages_kind_check';
    IF NOT FOUND OR old_constraint.contype <> 'c' OR NOT old_constraint.convalidated THEN
        RAISE EXCEPTION 'Original messages CHECK missing or invalid; inspect catalog';
    END IF;
    SELECT c.contype,c.convalidated,pg_get_constraintdef(c.oid) AS definition
      INTO new_constraint FROM pg_constraint c
      WHERE c.conrelid='public.messages'::regclass AND c.conname='messages_kind_guardrails_check';
    IF NOT FOUND OR new_constraint.contype <> 'c' OR NOT new_constraint.convalidated THEN
        RAISE EXCEPTION 'Validated guardrails CHECK missing; inspect catalog before swap';
    END IF;
    SELECT array_agg(DISTINCT parts[1] ORDER BY parts[1]) INTO existing_kinds
      FROM regexp_matches(old_constraint.definition, '''([^'']+)''', 'g') AS parts;
    IF existing_kinds IS DISTINCT FROM ARRAY[
        'fortune_context_root','fortune_derived','greeting','normal','topic_opening']::text[]
       AND existing_kinds IS DISTINCT FROM ARRAY[
        'fortune_context_root','fortune_derived','greeting','normal','safety','safety_check','topic_opening']::text[] THEN
        RAISE EXCEPTION 'Unexpected original message kinds; preserve existing CHECK';
    END IF;
    SELECT array_agg(DISTINCT parts[1] ORDER BY parts[1]) INTO existing_kinds
      FROM regexp_matches(new_constraint.definition, '''([^'']+)''', 'g') AS parts;
    IF existing_kinds IS DISTINCT FROM ARRAY[
        'fortune_context_root','fortune_derived','greeting','normal','safety','safety_check','topic_opening']::text[] THEN
        RAISE EXCEPTION 'Unexpected guardrails message kinds; do not swap';
    END IF;
END
$guardrails_swap$;

-- REVIEWED_SWAP_BODY
ALTER TABLE public.messages DROP CONSTRAINT messages_kind_check;
ALTER TABLE public.messages
  RENAME CONSTRAINT messages_kind_guardrails_check TO messages_kind_check;
