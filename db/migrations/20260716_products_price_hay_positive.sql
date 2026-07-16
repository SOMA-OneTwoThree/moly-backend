-- 0원 상품 금지 — 0원 구매는 hay_ledger가 amount=0 원장 행을 만들어
-- hay_transactions.amount <> 0 CHECK와 충돌(generic 500)하므로 카탈로그 입구에서 차단.
-- NULL(비매품)은 CHECK를 통과한다. 운영 카탈로그에 0원 상품 없음 확인(2026-07-16) — 즉시 적용 가능.

BEGIN;

ALTER TABLE public.products
  ADD CONSTRAINT products_price_hay_positive_ck CHECK (price_hay >= 1);

COMMIT;
