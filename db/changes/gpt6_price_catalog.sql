-- Append-only: 기존 단가·과거 원장·사용자 quota는 변경하지 않는다.
-- 배포 전에 적용. 최초 적용 시각부터 효력 발생, 재실행은 같은 버전을 유지한다.
INSERT INTO public.ai_price_catalog
    (catalog_version, provider, model, input_micro_usd, cached_input_micro_usd,
     cache_write_micro_usd, output_micro_usd, source_note, effective_from)
VALUES
    (20260923, 'openai', 'gpt-5.6-luna', 200000, 20000, 250000, 1200000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-5.6-luna', now()),
    (20260923, 'openai', 'gpt-5.6-terra', 2000000, 200000, 2500000, 12000000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-5.6-terra', now()),
    (20260923, 'openai', 'gpt-6-luna', 100000, 10000, 125000, 500000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-6-luna', now()),
    (20260923, 'openai', 'gpt-6-sol', 2000000, 200000, 2500000, 10000000,
     'OpenAI Standard <=272K, verified 2026-09-23; https://developers.openai.com/api/docs/models/gpt-6-sol', now())
ON CONFLICT (catalog_version, provider, model) DO NOTHING;

-- 충돌한 버전이 다른 단가라면 조용히 성공시키지 않는다.
DO $$
BEGIN
    IF (SELECT count(*) FROM public.ai_price_catalog p
        JOIN (VALUES
            ('gpt-5.6-luna', 200000, 20000, 250000, 1200000),
            ('gpt-5.6-terra', 2000000, 200000, 2500000, 12000000),
            ('gpt-6-luna', 100000, 10000, 125000, 500000),
            ('gpt-6-sol', 2000000, 200000, 2500000, 10000000)
        ) AS expected(model, i, r, w, o) ON p.model = expected.model
        WHERE p.catalog_version = 20260923 AND p.provider = 'openai'
          AND (p.input_micro_usd, p.cached_input_micro_usd,
               p.cache_write_micro_usd, p.output_micro_usd) =
              (expected.i, expected.r, expected.w, expected.o)) <> 4 THEN
        RAISE EXCEPTION 'GPT-6 catalog version conflicts with expected Standard rates';
    END IF;
END $$;
