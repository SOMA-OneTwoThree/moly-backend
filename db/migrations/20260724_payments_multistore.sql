-- 결제 원장 다스토어·다통화 정합성 (SOMA-343)
-- App Store 단일·KRW 가정을 제거: 실제 store 기록, 통화·금액 무손실, 환불 상태 반영.
-- 전부 additive/loosening — 기존 행·기존 API와 호환(기존 정수 amount는 numeric으로 캐스팅).
BEGIN;

-- 금액: 정수(KRW 가정) → numeric. 소수점 가격(4.99)·외화를 반올림 없이 저장.
ALTER TABLE public.payments ALTER COLUMN amount TYPE numeric(14,4);

-- 통화: 미확인 통화를 KRW로 확정하지 않도록 NOT NULL·기본값 제거(미확인=NULL).
ALTER TABLE public.payments ALTER COLUMN currency DROP DEFAULT;
ALTER TABLE public.payments ALTER COLUMN currency DROP NOT NULL;

-- store: 기본값 의존 제거 — 기록 시 항상 실제 스토어를 명시한다(코드가 세팅).
ALTER TABLE public.payments ALTER COLUMN store DROP DEFAULT;

-- 스토어·통화별 매출 집계용 인덱스.
CREATE INDEX IF NOT EXISTS payments_store_idx ON public.payments (store);

COMMIT;
