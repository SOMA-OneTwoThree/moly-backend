# DB 리셋 런북 — 커머스 스키마 전환 (2026-07-13)

> `db/schema.sql`은 **생성만** 담은 깨끗한 파일이다(기존 테이블 정리 없음). 리셋은 이 문서의 절차로 수행한다.
> 대상: prod Supabase (project ref `qkgjlgzsharnilxnkytd`) · 팀 결정: 유저 데이터 보존 없이 재생성.

## 순서 요약

`0. 백업(app_config) → 1. public 테이블 DROP → 1.5 계정(auth.users) 전체 삭제 → 1.6 mem0(vecs 스키마) 삭제 → 2. 신 스키마 적용 → 3. 시드 → 4. app_config 복원 → 5. 서버 동시 배포 → 6. 스모크`

DB를 미는 순간부터 배포돼 있는 구 코드는 깨진다 — **1~5를 한 호흡에** 진행할 것.

**절대 하지 말 것**: `auth`·`storage` 스키마의 테이블 DROP(Supabase 관리 영역 — Auth/Storage 서비스가 깨짐). 계정은 행 삭제(`DELETE`)로만 민다. Storage의 `shop-assets` 버킷은 **보존**(products 시드가 그 public URL을 참조).

---

## 0. 사전 백업 — app_config (필수)

운영 설정값(`free_launch_until` 등)은 시드에 없다. SQL Editor에서 실행해 결과를 복사해 둔다:

```sql
-- 현재 값을 INSERT문으로 내보내기 (결과 컬럼을 그대로 4단계에서 실행)
select format(
  'INSERT INTO public.app_config (key, value, description) VALUES (%L, %L::jsonb, %L);',
  key, value::text, description
) as restore_sql
from public.app_config
order by key;
```

`moly_life_ments`에 운영에서 직접 등록한 멘트가 있다면 같은 방식으로 내보낸다(시드 파일 `db/seed_moly_life_ments.sql`만 쓰고 있었다면 생략).

## 1. 기존 테이블 삭제 (SQL Editor)

```sql
-- ⚠️ mem0 테이블(memories 등)과 auth.*, storage.*는 목록에 없음 — 건드리지 않는다.
BEGIN;
DROP TABLE IF EXISTS public.idempotency_keys           CASCADE;
DROP TABLE IF EXISTS public.chat_contexts              CASCADE;
DROP TABLE IF EXISTS public.reward_ad_sessions         CASCADE;
DROP TABLE IF EXISTS public.user_devices               CASCADE;
DROP TABLE IF EXISTS public.user_notification_settings CASCADE;
DROP TABLE IF EXISTS public.routine_completions        CASCADE;
DROP TABLE IF EXISTS public.routines                   CASCADE;
DROP TABLE IF EXISTS public.diaries                    CASCADE;
DROP TABLE IF EXISTS public.user_equipment             CASCADE;
DROP TABLE IF EXISTS public.user_items                 CASCADE;
DROP TABLE IF EXISTS public.payments                   CASCADE;
DROP TABLE IF EXISTS public.iap_purchases              CASCADE;
DROP TABLE IF EXISTS public.subscription_hay_grants    CASCADE;
DROP TABLE IF EXISTS public.subscriptions              CASCADE;
DROP TABLE IF EXISTS public.user_daily_stats           CASCADE;
DROP TABLE IF EXISTS public.hay_transactions           CASCADE;
DROP TABLE IF EXISTS public.order_items                CASCADE;
DROP TABLE IF EXISTS public.orders                     CASCADE;
DROP TABLE IF EXISTS public.greetings                  CASCADE;
DROP TABLE IF EXISTS public.messages                   CASCADE;
DROP TABLE IF EXISTS public.app_config                 CASCADE;
DROP TABLE IF EXISTS public.moly_life_ments            CASCADE;
DROP TABLE IF EXISTS public.shop_items                 CASCADE;
DROP TABLE IF EXISTS public.hay_packs                  CASCADE;
DROP TABLE IF EXISTS public.products                   CASCADE;
DROP TABLE IF EXISTS public.conversations              CASCADE;
DROP TABLE IF EXISTS public.profiles                   CASCADE;
COMMIT;
```

## 1.5 계정 전체 삭제 (auth.users — 행 삭제만)

```sql
-- auth 스키마 테이블은 DROP 금지. 행 삭제로 전 계정 제거 —
-- identities/sessions/refresh_tokens는 auth 내부 FK CASCADE로 함께 정리된다.
DELETE FROM auth.users;
```

- 1단계에서 public 테이블을 이미 DROP했으므로 public 쪽 CASCADE 걱정 없음.
- 계정을 남기고 싶다면 이 단계를 건너뛰면 된다 — 기존 계정은 다음 로그인 때 moly-auth self-heal(`ensureProfile`)이 profiles를 재생성한다.

## 1.6 mem0 장기기억 삭제 (vecs 스키마)

```sql
-- mem0(vecs 라이브러리)가 만든 스키마 통째로 제거. 컬렉션·인덱스 포함.
DROP SCHEMA IF EXISTS vecs CASCADE;
-- vector 확장은 삭제하지 않는다(mem0 재생성 시 필요) — DROP EXTENSION 금지.
```

- 서버가 다음 기억 저장 시 vecs 스키마·컬렉션(`memories`)을 자동 재생성한다(최초 생성과 동일 경로).
- (선택) 서버 인프라에 mem0 history SQLite 파일이 있으면 함께 삭제 — 없어도 무해(변경 이력 캐시일 뿐).
- 혹시 예전 mem0 셋업이 public에 남긴 잔재(`public.memories`/`public.mem0` 테이블, `public.match_vectors` 함수)가 보이면 그것도 DROP — 현재 코드가 참조하지 않는다.

## 2. 신 스키마 적용

로컬에서 (`.env`에 `SUPABASE_DB_CONNECTION_STRING` 필요):

```bash
python db/apply.py db/schema.sql            # dry-run(자동 ROLLBACK) — 성공 확인
python db/apply.py db/schema.sql --commit   # 실제 반영
```

또는 SQL Editor에 `db/schema.sql` 전체를 붙여넣어 실행해도 된다(파일이 자체 트랜잭션 포함).

## 3. 시드

```bash
python db/apply.py db/seed_and_triggers.sql --commit    # 가입 트리거 + products(건초 3종·꾸미기 6종)
python db/apply.py db/seed_moly_life_ments.sql --commit # 몰리의 삶 멘트 풀
```

## 4. app_config 복원

**2026-07-13 백업본(prod 실측 — 리셋 후 이대로 실행):**

```sql
-- ⚠️ free_launch_token_limit는 prod=50000, 코드 기본값=30000 — 복원 안 하면 한도가 30000으로 떨어진다.
INSERT INTO public.app_config (key, value, description) VALUES
  ('free_launch_until', '"2026-09-01T04:00:00+09:00"'::jsonb,
   '런칭 무료 종료(활동일 8/31, 04:00 경계). 이 시각 이전 전원 무료. backend·moly-auth 공유'),
  ('free_launch_token_limit', '50000'::jsonb,
   '런칭 무료 기간 일 토큰 한도. backend·moly-auth 공유')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, description = EXCLUDED.description;
```

이후 리셋 때는 0단계에서 내보낸 INSERT문을 실행. 그 외 키는 아래 템플릿 참고 — **모든 키는 서버 코드 기본값이 있어서 미삽입 시에도 동작한다**(`app/config.py`). DB 값은 재배포 없이 운영 조정이 필요할 때만 넣으면 된다.


(moly-auth도 이 중 `daily_token_limit`·`diary_llm_min_tokens`·`free_launch_until`·`free_launch_token_limit`를 읽는다 — 같은 테이블이라 별도 작업 없음.)

## 5. 서버 배포 (동시)

- **moly-backend**: `refactor/schema-refactor` 브랜치 머지 → 배포.
- **moly-auth**: `lib/account/service.ts`의 user_items 장착 조회 변경분 배포.
- RevenueCat 웹훅 URL·Authorization, AdMob SSV 설정은 변경 없음.

## 6. 스모크 체크리스트

| 확인 | 방법 | 기대 |
|---|---|---|
| 신규 가입 | 새 계정 로그인 | profiles 자동 생성(트리거), `trial_ends_at` = 가입+48h, **기본 지급 3종 + 기본 루틴 2개** |
| 부팅 집계 | `GET /me` | 200, `equipment` 4슬롯 전부 null (지급은 되지만 장착은 안 됨) |
| 카탈로그 | `GET /shop/products` | 배경 2 + 아이템 4 (시드) — 집·운동·선글라스는 `owned:true` |
| 인벤토리 | `GET /inventory` | 기본 지급 3종 id |
| 루틴 | `GET /routines` | "이불 정리하기"·"물 마시기" 2건 (주 7회, 리마인더 off) |
| 충전소 | `GET /charging-station` | `hay_products` 3종 + `balance:0` |
| 출석 | `POST /charging-station/attendance` | `{granted:10, balance_after:10}` / 재호출 409 |
| 상점 구매 | `POST /shop/purchases` (귤) | 402(잔액 부족) — 정상 게이팅 / 선글라스는 409 ALREADY_OWNED |
| 구독 상태 | `GET /subscription` | `status:"none"` (+ 런칭 기간이면 `in_trial:true`) |
| RC 웹훅 | RC 대시보드 테스트 이벤트 발송 | 200 `{"status":"ok"}` + subscriptions/payments 행 생성 |
