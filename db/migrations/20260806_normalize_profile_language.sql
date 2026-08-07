-- 적용: python db/apply.py db/migrations/20260806_normalize_profile_language.sql --commit
-- profiles.language를 콘텐츠 언어 셋(ko·en·ja)으로 고정한다.
--
-- 왜: 이 제품이 유저에게 보여주는 글은 한국어·영어·일본어뿐이다. 페르소나도 말투 규칙도
-- 검수 프롬프트도 그 셋만 있다. 그런데 `profiles.language`에는 클라이언트가 보낸 값이 그대로
-- 들어와서(zh-Hant-TW·th·es-ES 등) 코드 곳곳이 그 값을 읽었다. LLM 지시에 그 값을 그대로
-- 넣던 곳들은 코드에서 이미 고쳤지만, 저장된 값이 계속 남아 있으면 앞으로 새로 생기는 코드가
-- 또 그 값을 읽는다. 저장되는 값 자체를 셋으로 좁혀서 미지원 언어라는 상태를 없앤다.
--
-- 규칙은 `app/services/i18n.py`의 `resolve()`와 같다.
--   지역 태그를 떼고(ko-KR -> ko) ko·en·ja면 그대로, 그 밖은 en, 비었으면 en.
--
-- 기본값도 한국어에서 영어로 바꾼다. 해외 출시 기준으로 '모르는 언어'는 영어가 맞다.
-- 한국 유저는 온보딩에서 ko가 실제로 들어오므로 영향이 없다.
--
-- ⚠️ 이 파일은 [[migration-before-merge]]대로 코드 머지·배포보다 **먼저** 적용한다.
--    코드는 이미 `resolve()`를 거치므로 값이 좁혀져도 동작이 같다(적용 전후 모두 안전).
--
-- ⚠️ `profiles`는 moly-auth(계정 API)도 쓴다. 그쪽 코드를 고치지 않아도 되도록 트리거로
--    막는다. 값을 거부하지 않고 조용히 바꾸므로 온보딩이 실패하지 않는다.
BEGIN;

-- BCP 47 언어 태그 -> 콘텐츠 언어 버킷. i18n.resolve()와 같은 규칙이다.
CREATE OR REPLACE FUNCTION public.normalize_content_language(tag text)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = public
AS $$
  SELECT CASE
    WHEN tag IS NULL OR btrim(tag) = '' THEN 'en'
    WHEN lower(split_part(btrim(tag), '-', 1)) IN ('ko', 'en', 'ja')
      THEN lower(split_part(btrim(tag), '-', 1))
    ELSE 'en'
  END
$$;

-- 기존 행 정리. ko-KR -> ko, en-US -> en, zh-Hant-TW -> en 같은 식이다.
UPDATE public.profiles
   SET language = public.normalize_content_language(language)
 WHERE language IS DISTINCT FROM public.normalize_content_language(language);

-- 앞으로 들어오는 값도 같은 규칙으로 좁힌다. 거부가 아니라 변환이다 — 거부하면 moly-auth의
-- 온보딩이 실패한다.
CREATE OR REPLACE FUNCTION public.normalize_profile_language()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  NEW.language := public.normalize_content_language(NEW.language);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_normalize_profile_language ON public.profiles;
CREATE TRIGGER trg_normalize_profile_language
  BEFORE INSERT OR UPDATE OF language ON public.profiles
  FOR EACH ROW
  EXECUTE FUNCTION public.normalize_profile_language();

-- 컬럼 기본값도 영어로. 온보딩이 언어를 안 보내면 여기가 쓰인다.
ALTER TABLE public.profiles ALTER COLUMN language SET DEFAULT 'en';

COMMIT;

-- 적용 뒤 확인:
--   SELECT language, count(*) FROM public.profiles GROUP BY language ORDER BY 2 DESC;
--     -> ko·en·ja 세 값만 나와야 한다.
--   INSERT/UPDATE로 'zh-Hant-TW'를 넣어 보고 'en'으로 저장되는지 본다.
