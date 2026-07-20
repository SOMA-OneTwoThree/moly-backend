-- trigger 설치가 커밋된 뒤 실행하는 기존 대화 watermark seed.
-- seed 도중 새 응답은 trigger가 pending 행을 만들며 DO NOTHING이 그 상태를 보존한다.
-- 적용: python db/apply.py db/migrations/20260720_memory_ingestion_states_seed.sql --commit

BEGIN;

INSERT INTO public.memory_ingestion_states (
  user_id, activity_date, through_message_id, completed_at
)
SELECT
  m.user_id,
  m.activity_date,
  CASE
    WHEN m.activity_date < ((now() AT TIME ZONE p.timezone) - INTERVAL '4 hours')::date
      AND EXISTS (
        SELECT 1
        FROM public.diaries d
        WHERE d.user_id = m.user_id
          AND d.diary_date = m.activity_date
          AND d.source IN ('llm', 'preset')
      )
      THEN max(m.id)
    ELSE 0
  END,
  CASE
    WHEN m.activity_date < ((now() AT TIME ZONE p.timezone) - INTERVAL '4 hours')::date
      AND EXISTS (
        SELECT 1
        FROM public.diaries d
        WHERE d.user_id = m.user_id
          AND d.diary_date = m.activity_date
          AND d.source IN ('llm', 'preset')
      )
      THEN now()
    ELSE NULL
  END
FROM public.messages m
JOIN public.profiles p ON p.id = m.user_id
WHERE m.kind = 'normal'
GROUP BY m.user_id, m.activity_date, p.timezone
ON CONFLICT (user_id, activity_date) DO NOTHING;

COMMIT;
