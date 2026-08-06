-- 일기 호환 트리거 — 구 코드가 도는 동안 `diaries`의 새 컬럼을 대신 채운다.
--
-- ⚠️ **이 파일을 따로 적용하면 안 된다.**
--    `20260804_zzz_conversational_recall.sql` 사본 안에 끼워 넣어야 한다.
--
--    넣는 위치: `ALTER TABLE public.diaries ADD COLUMN ...` 블록(89~102행)이 끝난 **뒤**,
--              `ALTER TABLE public.diaries ALTER COLUMN display_date SET NOT NULL`(117행)이
--              오기 **전**.
--
--    왜 그 위치인가
--      - 더 앞(별도 파일로 미리 적용): 트리거 함수가 읽는 `kind`·`display_date`가 아직 없다.
--        PostgreSQL은 트리거를 만들 때 컬럼 존재를 검사하지 않으므로 생성은 성공하고,
--        그 순간부터 **모든 일기 삽입이 실패한다**(개발 DB에서 재현 확인).
--      - 더 뒤(11번 파일을 다 적용한 뒤): `display_date NOT NULL`과 CHECK 3종이 트리거 없이
--        존재하는 구간이 생겨 구 코드의 일기 생성이 실패한다.
--      `db/apply.py`가 파일 하나를 트랜잭션 하나로 실행하므로, 파일 안 저 위치에서만
--      위험 구간이 0이 된다.
--
-- 무엇을 채우나
--    구 코드의 일기 모델에는 `kind`·`record_status`·`display_date`·`activity_date`가 아예 없다.
--    트리거가 `source` 값을 보고 대신 채운다. `author`는 컬럼 기본값 'capi'라 손대지 않는다.
--
--    source    → kind         activity_date   record_status
--    welcome   → welcome      비움            published(기본값)
--    llm       → shared_day   diary_date      published(기본값)
--    preset    → capi_day     diary_date      published(기본값)
--    none      → 비움         비움            processed
--
--    이 조합이 `diaries_kind_ck`·`diaries_kind_activity_ck`를 전부 통과한다.
--
-- 안전 장치
--    1. `RETURN NEW`로 끝낸다. 행을 버리면(`RETURN NULL`) `RETURNING id`를 쓰는 저장이 깨진다.
--    2. **비어 있을 때만 채운다.** 배포 뒤 3단계까지는 새 코드가 넣는 값에도 이 트리거가 돈다.
--       지금은 새 코드의 매핑과 같아서 덮어써도 결과가 같지만, 조건을 걸어 두면 나중에
--       매핑이 달라져도 안전하다.
--    3. **3단계에서 반드시 지운다.** 아래 "제거" 문장을 쓴다.

CREATE OR REPLACE FUNCTION public.diaries_legacy_compat()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.display_date IS NULL THEN
    NEW.display_date := NEW.diary_date;
  END IF;

  IF NEW.kind IS NULL THEN
    NEW.kind := CASE NEW.source
                  WHEN 'welcome' THEN 'welcome'
                  WHEN 'llm'     THEN 'shared_day'
                  WHEN 'preset'  THEN 'capi_day'
                  ELSE NULL
                END;
  END IF;

  IF NEW.activity_date IS NULL AND NEW.kind IN ('shared_day', 'capi_day') THEN
    NEW.activity_date := NEW.diary_date;
  END IF;

  -- source='none'은 사용자에게 안 보이는 처리 표시다. record_status 기본값이 'published'라
  -- 그대로 두면 kind가 비어 있어 `diaries_kind_ck`에 걸린다.
  IF NEW.source = 'none' AND NEW.record_status = 'published' THEN
    NEW.record_status := 'processed';
  END IF;

  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS diaries_legacy_compat_tg ON public.diaries;
CREATE TRIGGER diaries_legacy_compat_tg
  BEFORE INSERT ON public.diaries
  FOR EACH ROW
  EXECUTE FUNCTION public.diaries_legacy_compat();


-- ── 3단계에서 지울 때 쓰는 문장 (여기서는 실행하지 않는다) ─────────────────────
-- DROP TRIGGER IF EXISTS diaries_legacy_compat_tg ON public.diaries;
-- DROP FUNCTION IF EXISTS public.diaries_legacy_compat();
