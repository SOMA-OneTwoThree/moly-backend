

## 문서 목적
이 문서는 내가 너에게 주기 위한 프롬프팅을 알기 쉽게 적어준 것이고, 스텝별로 어떻게 할건지 여기 적어놓고 실행하면서 진행하면 됨. **단 애매한 것이 있다면 혼자 판단하지 말고 나에게 물어보며 진행해**.

## 현재 상황

팀 회의 결과 실제 사용자 데이터가 쌓이기전에, 데이터 스키마를 커머스에서 많이 쓰는 명칭으로 리팩토링 하기로 결정.
Order / Product / Payment /  Subscription / (Shipment) -> 이 도메인명은 관례이므로 추가 필요하면 추가 가능

## 고려해야할 점

- 현재 docs/의 ERD.md와 ARCHITECTURE.md로 Supabase에 query를 날려 테이블을 만들었고, API_SPEC을 이 backend 레포에 구현하여 프론트에 붙혔음.

- SQL.md보면 현재 테이블이 어떻게 구성되어있는지 확인 가능.

- 아마 ERD 명칭과 연관관계가 많이 바뀜에 따라 API_SPEC의 request, response명이 많이 달라질 것으로 예상. **대신 기존 서비스 흐름은 절대 바뀌어선 안됌.** , **리팩토링에 가장 힘을 쓰고 나서, 프론트가 API_SPEC.md을 보고 바로 붙힐 수 있도록 문서 작성에 힘 써야함** 

- 커머스 관례 테이블명에 따라 DDD 아키텍처를 지켜서 리팩토링 해야함. 서비스 로직을 그 전과 달라지지 않게 꼭 지키고, 그에 따른 test로직도 확실하게 재작성 바람.

- 리팩토링 하는 과정에서 그 전 코드들은 참고를 해서 문제없게끔 되었다면 과감하게 잔재를 지워주기 바람

- 자세한 내용들은 docs/ 아레에 있고, 특히 내 로컬에 backend-auth도 있으니 참고 바람. 혹시 그것도 수정할 것이 있다면 수정 바람. 

- 현재 DB가 쿼리문을 날려서 운영중인데 테이블 밀고 새로 생성할 예정임. 걱정하지 말고 ERD 재구성에 힘쓰면 됌.

- 아래 나온 결정사항 잘 참고해서 구성 바람


## 팀원과 회의 중 나

- hay_stack의 필요성? - 언제 받았는지, 어디서 받았는지
- hay_transaction의 ref_id의 문제 - 이름이 헷갈림. 참고용이면 상관없는데 내부 로직에서 사용하면 복잡해질 수 있음. 일일히 사람이 나중에 검수하기 힘듦.  -> cs에서 쉽게 확인하고 처리할 수 있게. id가 구매 쪽으로 매번 가는 거 아니면 검증을 자동화 할수 없다. 매번 해야한다.
- ref_id가 하나의 키를 담는게 아니라 여러개의 키를 담는게 문제. 사용자가 받는 건초더미는 저기다가 저장하는게 별로 필요 없어보인다. 차라리 order라는 이름을 더 많이 쓴다 커머스에서

- order, order_item 이름을 많이 씀, 나중에 묶음 아이템이 생긴다 했을 때 여러 아이템을 한 트랜잭션에 넣을 때 관리하기 편함

- 부분 환불 같은 로직 생길때도 유의미

- shop_item -> product라는 이름을 많이 씀

- item 붙은게 묶음, 안붙은게 개별인 것이 관례

- shop_item에서 어떤 slot인지 결정되는데, 굳이 user_item테이블과 user_equipment를 나눌 필요가 있나 -> - - user_equipment가 장착시기 트래킹을 위한 테이블로 생각됨

 - purchase 테이블에 얼마 결제했는지가 없음, 역추적하는 것보다 purchase에서 관리하길 바람

- 구매 이력과 재화 이력 분리 필요 -> 가격정책 변동시 문제됨 -> 매출 확인 문제 → 환불 정책 때 중요함

- 외래키 -> modern 외래키 인덱싱 걸면 대부분 성능문제 해결

- 테이블을 문제 없는 선에서 깔끔하게 줄일 필요가 있어보임

---
---

# 실행 계획 (2026-07-12 작성 — Claude)

## A. 확정된 설계 결정 (Q&A 완료)

| # | 질문 | 결정 |
|---|---|---|
| 1 | hay_packs + shop_items 통합? | **단일 `products` 테이블로 통합** — `product_type`(`hay_pack` / `cosmetic`) 구분, 타입별 컬럼은 nullable + CHECK 강제 |
| 2 | orders/order_items 범위? | **모든 구매를 orders로** — IAP 건초(KRW 결제)와 상점 꾸미기(HAY 결제) 모두. `orders.currency`로 구분. `iap_purchases` 테이블 폐기 |
| 3 | payments 범위? | **IAP + 구독 결제 모두 기록** — 스토어 transaction_id, 결제금액, 상태. 구독은 RevenueCat 이벤트(구매/갱신)마다 payment 행 생성 → 매출을 payments 한 테이블에서 집계 |
| 4 | user_items / user_equipment? | **user_items로 통합** — `equipped_slot`/`equipped_at` 컬럼 추가, `user_equipment` 폐기. 구독 전용 장착은 `source='subscription'` 행으로 흡수 |

파생 결정 (위 결정에서 자연히 따라오는 것 — 이견 있으면 말해줘):

- **`hay_transactions`(원장)는 이름 유지.** 재화 원장은 커머스 주문 도메인이 아니라 지갑(wallet) 도메인이고, 현재 구조(원장=진실, balance=캐시)가 이미 관례적. 대신:
  - **`ref_id`(다형 text) 폐기** → `order_id uuid FK NULL` 로 교체. 구매 관련 원장 행은 주문으로 자동 추적 가능(CS 자동화 — 회의 요구사항).
  - 환불 clawback 멱등 판정(현재 유일한 ref_id 읽기 로직)은 원장 스캔 대신 **`subscription_hay_grants.revoked_at` + `clawback_hay_transaction_id`** 로 이동.
- 원장 `type` enum은 유지하되 `iap_purchase` → **`order_payment`는 하지 않음** — 기존 값 유지(`attendance` `ad_reward` `routine_reward` `iap_purchase` `subscription_grant` `shop_purchase` `refund_revoke` `admin_adjustment`). 흐름 의미가 그대로라 이름도 그대로 두는 게 프론트 혼란이 적음.
- **서비스 흐름 불변** — 엔드포인트 경로·에러 코드·게이팅·멱등 규칙은 전부 그대로. 바뀌는 건 내부 테이블/모델명과 일부 응답 필드명뿐.

## B. 목표 스키마 (경제 도메인만 — 나머지 테이블은 불변)

### B.1 `products` ← shop_items + hay_packs 통합

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | uuid PK | |
| `product_type` | enum `product_type` (`hay_pack` `cosmetic`) | |
| `name` / `description` | text | |
| `slot` | enum `equipment_slot` NULL | cosmetic 전용 (`background` `head` `neck` `body`) |
| `price_hay` | int NULL | cosmetic 전용. NULL = 비매품(구독 전용) |
| `is_subscriber_only` | bool default false | cosmetic 전용 |
| `hay_amount` | int NULL | hay_pack 전용 — 지급 건초량 |
| `price_krw` | int NULL | hay_pack 전용 — 표시 참고용(결제가는 StoreKit) |
| `app_store_product_id` | text UNIQUE NULL | hay_pack 전용 |
| `assets` | jsonb NULL | cosmetic 전용 |
| `is_active` / `sort_order` | | |

- CHECK (타입별 상호 강제):
  - `hay_pack`: `hay_amount IS NOT NULL AND app_store_product_id IS NOT NULL AND slot IS NULL AND price_hay IS NULL AND is_subscriber_only = false`
  - `cosmetic`: `slot IS NOT NULL AND hay_amount IS NULL AND app_store_product_id IS NULL AND is_subscriber_only = (price_hay IS NULL)` (기존 CHECK 승계)
- UNIQUE `(id, slot)` — user_items의 장착 슬롯 일치 복합 FK 대상 (기존 shop_items 방식 승계)

### B.2 `orders` / `order_items` — 모든 구매의 단일 진입점

**`orders`**
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid FK→profiles | |
| `currency` | enum `order_currency` (`KRW` `HAY`) | KRW = IAP 건초구매 / HAY = 상점 꾸미기 구매 |
| `status` | enum `order_status` (`pending` `paid` `failed` `refunded`) | HAY 주문은 트랜잭션 안에서 즉시 `paid` |
| `total_amount` | int | KRW 결제금액 또는 HAY 차감량 (양수) |
| `created_at` / `updated_at` | timestamptz | |

**`order_items`**
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | uuid PK | |
| `order_id` | uuid FK→orders (CASCADE) | |
| `product_id` | uuid FK→products | |
| `quantity` | int default 1 | MVP는 항상 1 — 묶음 대비 |
| `unit_price` | int | 구매 시점 가격 스냅샷 (가격정책 변동 대비 — 회의 요구사항) |

- 인덱스: `orders(user_id, created_at DESC)`, `order_items(order_id)`, `order_items(product_id)`

### B.3 `payments` — 실결제(현금) 기록

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid FK→profiles | |
| `order_id` | uuid FK→orders NULL | IAP 건초 주문과 1:1. 구독 결제는 NULL |
| `subscription_id` | uuid FK→subscriptions NULL | 구독 결제(구매·갱신) 시 연결 |
| `store` | text default `'app_store'` | |
| `store_transaction_id` | text UNIQUE | 멱등 키 (기존 iap_purchases.transaction_id 승계) |
| `amount` | int NULL | 결제금액(KRW). RC 이벤트에 가격 있으면 기록, 없으면 NULL |
| `currency` | text default `'KRW'` | |
| `status` | enum `payment_status` (`paid` `refunded`) | |
| `paid_at` / `created_at` | timestamptz | |

- CHECK: `order_id IS NOT NULL OR subscription_id IS NOT NULL`
- 매출 집계 = `SELECT sum(amount) FROM payments WHERE status='paid'` 한 방.

### B.4 `user_items` — 보유 + 장착 통합 (user_equipment 폐기)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid FK→profiles | |
| `product_id` | uuid FK→products | cosmetic만 |
| `source` | enum `user_item_source` (`purchase` `subscription` `admin_grant`) | `purchase`=주문 구매 / `subscription`=구독 전용 장착 시 자동 생성(소유 아님, 인벤토리 API 미노출) / `admin_grant`=운영 무상 지급 |
| `order_id` | uuid FK→orders NULL | `purchase`면 필수, 그 외 NULL |
| `equipped_slot` | enum `equipment_slot` NULL | NULL = 미장착. 값 있으면 장착 중 |
| `equipped_at` | timestamptz NULL | 장착 시기 트래킹 (기존 user_equipment 역할 승계) |
| `acquired_at` | timestamptz | |

- UNIQUE `(user_id, product_id)` — 중복 구매 방지 (승계)
- **부분 UNIQUE `(user_id, equipped_slot) WHERE equipped_slot IS NOT NULL`** — 슬롯당 1개 장착 (기존 user_equipment PK 승계)
- **복합 FK `(product_id, equipped_slot) → products(id, slot)`** — 슬롯 일치 DB 강제 (승계. equipped_slot NULL이면 FK 미평가 = 미장착 행 자유)
- CHECK: `(source = 'purchase') = (order_id IS NOT NULL)`
- 구독 만료/환불 시: 기존 "user_equipment 행 삭제" → **`source='subscription'` 행 삭제**로 동일 효과(기본 복귀).

### B.5 변경되는 기존 테이블

- **`hay_transactions`**: `ref_id` 제거, `order_id uuid FK NULL` 추가. 인덱스 `(user_id, created_at DESC)` 유지 + `(order_id)` 추가.
- **`subscription_hay_grants`**: `revoked_at timestamptz NULL`, `clawback_hay_transaction_id bigint FK NULL` 추가 (환불 회수 멱등 — ref_id 스캔 대체).
- **`subscriptions`**: 변경 없음 (이미 커머스 관례명).
- **폐기**: `hay_packs`, `shop_items`, `iap_purchases`, `user_equipment` (4개 감소, 3개 신설 — 순 -1이지만 결제금액·주문 추적 확보)
- **불변**: profiles, user_daily_stats, reward_ad_sessions, messages, greetings, diaries, routines, routine_completions, user_notification_settings, user_devices, app_config, moly_life_ments, chat_contexts, idempotency_keys

### B.6 원장 `order_id` 연결 규칙 (type별)

| type | order_id | 비고 |
|---|---|---|
| `iap_purchase` (+) | ✅ KRW 주문 | payments↔orders↔원장 전부 연결 |
| `shop_purchase` (−) | ✅ HAY 주문 | |
| `attendance` `ad_reward` `routine_reward` | NULL | 광고 멱등은 기존대로 reward_ad_sessions |
| `subscription_grant` (+) | NULL | grants.hay_transaction_id 역방향 유지 |
| `refund_revoke` (−) | NULL | grants.clawback_hay_transaction_id로 역추적 |

## C. 단계별 실행 계획

> 각 단계 완료 시 이 문서의 체크박스를 갱신하고 커밋. DB는 밀고 재생성 예정이므로 마이그레이션 스크립트 불필요 — schema.sql 전면 재작성.

- [x] **STEP 1 — DDL 재작성** (2026-07-12 완료): `db/schema.sql` 전면 재작성 — products/orders/order_items/payments/user_items(통합) 신설, 구테이블 4종 DROP 목록 포함, FK 인덱스 전부 명시. `db/seed_and_triggers.sql` → products 통합 시드. 로컬 Postgres 17에서 스키마+시드 적용 및 제약 스모크 테스트(슬롯 불일치 FK·슬롯당 1장착 부분 UNIQUE·타입별 CHECK·payments CHECK·탈퇴 CASCADE) 전부 통과.
  - 참고: enum은 기존 컨벤션(text + CHECK) 유지 — 모델이 String 매핑(asyncpg 마찰 회피).
  - 참고: user_items의 `(source='purchase') = (order_id IS NOT NULL)` CHECK는 **뺐음** — 탈퇴 CASCADE 중 order 삭제가 SET NULL을 먼저 발동해 제약 위반이 나는 순서 문제. 서비스 레이어에서 보장.
- [x] **STEP 2 — 모델** (2026-07-12 완료): `product.py`/`order.py`(Order·OrderItem)/`payment.py`/`user_item.py`(통합) 신설, `hay_transaction.py`(ref_id→order_id), `subscription_hay_grant.py`(+revoked_at·clawback_hay_transaction_id). 구모델 4개 삭제. **모델↔DB 컬럼 1:1 대조 스크립트 전 테이블 일치 확인.**
  - 학습: SQLAlchemy `default=uuid.uuid4`는 flush 시점 적용 → flush 전 id를 참조하는 Order·Subscription은 생성자에서 `id=uuid.uuid4()` 명시.
- [x] **STEP 3 — 서비스** (2026-07-12 완료):
  - `order.py` 신설(create_paid_order — 주문+항목, 가격 스냅샷), `payment.py`(← iap.py git mv): 멱등 검증 → 주문·결제·원장
  - `shop.py`: 카탈로그는 shop.get_products에 유지(별도 catalog.py 안 만듦 — 모듈 수 절제), 구매/인벤토리/장착 → user_items 기반 재작성
  - `subscription.py`: RC 활성 이벤트마다 payments 기록, clawback 멱등 = grants.revoked_at, unequip = user_items 기반
  - `economy.py` products(hay_pack) 조회, `hay_ledger.py` order_id, `ads.py` ref_id 제거
  - ⚠️ 의도된 edge 개선 2건: (a) 증정 이력 없는 환불은 회수하지 않음(구코드는 받은 적 없는 건초도 회수), (b) 잔액 0 회수는 원장 기록 없이 revoked_at만(구코드는 amount=0 삽입 → DB CHECK 위반 버그)
  - ⚠️ 리뷰어(architect)가 실DB 재현으로 잡은 블로커 수정: 같은 슬롯 장착 교체가 부분 UNIQUE의 statement 단위 평가와 충돌 → put_equipment를 검증→해제 전부→flush→장착 2-pass로 재구성
- [x] **STEP 4 — API/스키마 레이어** (2026-07-12 완료): 경로 전부 불변. `/charging-station` `hay_packs`→`hay_products`, `POST /shop/purchases` 응답에 `order_id` 추가. 에러코드 불변. ※ `POST /wallet/purchases`는 RevenueCat 전환으로 이 백엔드에 미존재(IAP는 RC 웹훅 경로) — STEP 6에서 API_SPEC 정정 필요.
- [x] **STEP 5 — 테스트** (2026-07-12 완료): 기존 시나리오 전부 이식 + 신규(주문 스냅샷, payments 기록, clawback 멱등 3종, 장착 통합 6종, IAP grant_pack 4종 — tests/test_payment.py 신설). **pytest 135 passed**(test_ads 1건은 리팩토링 전에도 실패하는 기존 env 문제 — DATABASE_URL 미설정 시 실 엔진 생성). **실DB e2e 34/34 통과**(같은 슬롯 스왑 회귀 포함).
- [x] **STEP 6 — 문서** (2026-07-13 완료):
  - `API_SPEC.md`: 상단 changelog(프론트 변경점 5개) + `hay_products` 키 + 구매 응답 `order_id` + **RC 전환 현행화**(verify/restore·/wallet/purchases·/webhooks/appstore 제거 명시, RC SDK 흐름 기술) + **광고 SSV 자동지급 현행화**(/ads/reward 클레임 제거, /reward-ad-sessions 계약 명시) + 부록 A/B 정리. ※ 리팩토링 검증 중 스펙-구현 괴리(RC·SSV)를 발견해 함께 정정함.
  - `ERD.md`: §2 다이어그램·§4 경제 도메인 전면 개정(products/orders/order_items/payments/user_items 통합), §8 RLS 목록, §9 매핑 갱신.
  - `SQL.md`: 신 스키마 적용 DB의 information_schema에서 자동 생성(22테이블).
  - `ARCHITECTURE.md` 등 기타 문서: 구 테이블명 참조 없음 확인(grep).
- [x] **STEP 7 — moly-auth 수정** (2026-07-13 완료): `backend/lib/account/service.ts` `loadEquipment` → `user_items`(equipped_slot NOT NULL) 조회로 변경. `npx tsc --noEmit` 통과. (subscriptions/profiles/user_daily_stats는 불변이라 영향 없음)
- [ ] **STEP 8 — DB 재생성 + 배포**: 모든 코드 준비 완료 — 절차는 **`docs/DB_RESET_RUNBOOK.md`** (app_config 백업/복원 SQL, SQL Editor DROP 블록, 적용·시드·배포·스모크 순서). `db/schema.sql`은 생성만 담은 클린 파일(2026-07-13 결정 — 기존 테이블 정리는 런북에서 수동).

## D. 리스크 노트

- ref_id 읽기 로직은 clawback 1곳뿐(subscription.py:96-105) — grants.revoked_at 교체 시 기존 테스트 `test_rc_cancellation_refund_clawback` 시나리오로 회귀 검증.
- user_items 통합의 유일한 의미 변화: 구독전용 장착 시 행이 생김(source='subscription'). **인벤토리 API는 source='purchase'만 노출**해 기존 응답 불변 유지.
- moly-auth의 `GET /me` equipment 블록 — STEP 7 전까지 신 DB에서 깨짐. moly-backend와 moly-auth를 같은 타이밍에 배포 필요.