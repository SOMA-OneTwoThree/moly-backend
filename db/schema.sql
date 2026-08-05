-- moly-backend 스키마 (Supabase / PostgreSQL)
-- 단일 소스 = app/models/*.py (코드가 실제로 읽고 쓰는 형태). 설계 근거 = docs/ERD.md.
-- enum은 모델이 String 매핑(asyncpg 바인딩 마찰 회피) → DB도 text + CHECK 제약으로 검증.
-- 클라 직접 쓰기 없음(전부 서버 API 경유) → RLS deny-default(심층 방어). 서버는 owner 롤이라 RLS 우회.
-- 실행 전제: 빈 public 스키마(생성만 있음 — 기존 테이블 정리는 docs/DB_RESET_RUNBOOK.md).
-- 실행: psql "<conn>" -f db/schema.sql  (또는 python db/apply.py)

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- updated_at 자동 갱신 트리거 함수(기존 존재 시 재사용). 없으면 생성.
CREATE OR REPLACE FUNCTION public.set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ─────────────────────────────────────────────────────────────
-- 1. 계정·프로필
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.profiles (
  id                 uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  nickname           text        CHECK (char_length(nickname) <= 10),
  language           text        NOT NULL DEFAULT 'ko',
  timezone           text        NOT NULL DEFAULT 'Asia/Seoul',
  -- 음수 허용 = 부채(환불 회수 시 증정 소비분을 음수로 내려 이후 획득이 자연 상계 → 완전 회수, SOMA-372).
  -- 소비 경로의 <0 게이트는 코드가 유지(hay_ledger.apply allow_negative=False 기본).
  hay_balance        integer     NOT NULL DEFAULT 0,
  trial_ends_at      timestamptz,
  review_prompted_at timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER profiles_set_updated_at BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ─────────────────────────────────────────────────────────────
-- 2. 서버 원본 카탈로그(FK 없음) — products / moly_life_ments / app_config
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.products (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_type         text    NOT NULL CHECK (product_type IN ('hay_pack','cosmetic')),
  name                 text    NOT NULL,           -- 원문(ko). 다국어는 name_i18n
  description          text,
  name_i18n            jsonb CONSTRAINT products_name_i18n_obj_ck CHECK (name_i18n IS NULL OR jsonb_typeof(name_i18n) = 'object'),  -- 언어별 이름({ko,en,ja}), object만(SOMA-346)
  -- cosmetic 전용
  public_id            text    UNIQUE,           -- API 노출용 안정 문자열 ID
  slot                 text    CHECK (slot IN ('theme','hat','glasses','neck','body')),
  -- NULL = 구매 불가(기본 지급 등). 0원은 구매 시 원장 CHECK(amount<>0)와 충돌하므로 금지.
  price_hay            integer CONSTRAINT products_price_hay_positive_ck CHECK (price_hay >= 1),
  is_subscriber_only   boolean NOT NULL DEFAULT false,
  asset_version        integer,
  assets               jsonb,
  -- hay_pack 전용
  hay_amount           integer,
  price_krw            integer,                 -- 표시 참고용(결제가는 StoreKit)
  app_store_product_id text    UNIQUE,
  play_store_product_id text   UNIQUE,          -- Google Play 상품ID(Play Console 확정 후 주입, NULL 허용)
  is_active            boolean NOT NULL DEFAULT true,
  -- 신버전(rightside 자세) 계약에만 노출 — 레거시 카탈로그/인벤토리에서 제외.
  is_v2_only           boolean NOT NULL DEFAULT false,
  sort_order           integer NOT NULL DEFAULT 0,
  -- 타입별 컬럼 상호 강제
  CONSTRAINT products_hay_pack_ck CHECK (
    product_type <> 'hay_pack' OR (
      hay_amount IS NOT NULL AND app_store_product_id IS NOT NULL
      AND public_id IS NULL AND slot IS NULL AND price_hay IS NULL
      AND asset_version IS NULL AND assets IS NULL
      AND is_subscriber_only = false
    )
  ),
  CONSTRAINT products_cosmetic_ck CHECK (
    product_type <> 'cosmetic' OR (
      public_id IS NOT NULL AND slot IS NOT NULL
      AND hay_amount IS NULL AND app_store_product_id IS NULL AND price_krw IS NULL
      AND play_store_product_id IS NULL
      -- 구독 전용 꾸미기는 폐지 — 카탈로그에 노출되면서 구매만 막히는 상품을 만들 수 없다.
      AND is_subscriber_only = false
      -- 최종 에셋이 없는 상품은 inactive로만 준비할 수 있다.
      AND (
        is_active = false
        OR (asset_version IS NOT NULL AND asset_version >= 1 AND assets IS NOT NULL)
      )
    )
  ),
  -- user_items 복합 FK (product_id, equipped_slot) → (id, slot) 대상
  CONSTRAINT products_id_slot_uq UNIQUE (id, slot)
);

CREATE TABLE public.moly_life_ments (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  content    text    NOT NULL,
  weather    text    NOT NULL CHECK (weather IN ('sunny','cloudy','rainy','windy')),
  is_active  boolean NOT NULL DEFAULT true,
  -- 날짜 지정본(직접 작성) = 그날 우선 선택. NULL = 랜덤 폴백 풀.
  diary_date date,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- 한 날짜당 지정본 1건만(부분 유니크). NULL 풀 행은 제약 밖.
CREATE UNIQUE INDEX moly_life_ments_diary_date_uq
  ON public.moly_life_ments (diary_date) WHERE diary_date IS NOT NULL;

CREATE TABLE public.app_config (
  key         text PRIMARY KEY,
  value       jsonb NOT NULL,
  description text,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────
-- 3. 대화(messages → greetings 순서) · 주문 · 원장 · 일일 통계
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.messages (
  id            bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  user_id       uuid   NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  sender        text   NOT NULL CHECK (sender IN ('user','moly')),
  kind          text   NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal','greeting')),
  content       text   NOT NULL,
  input_tokens  integer,
  output_tokens integer,
  cache_read_tokens  integer,   -- 프롬프트 캐시 텔레메트리(실원가·히트율)
  cache_write_tokens integer,
  billable_tokens    integer,   -- 원가 가중 청구 스냅샷(가중치 변경 후 재감사)
  activity_date date   NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_user_id_desc_idx  ON public.messages (user_id, id DESC);
CREATE INDEX messages_user_actdate_idx  ON public.messages (user_id, activity_date);

CREATE TABLE public.greetings (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  context              text NOT NULL CHECK (context IN ('onboarding','home_enter','morning','evening','comeback')),
  content              text NOT NULL,
  activity_date        date NOT NULL,
  committed_message_id bigint REFERENCES public.messages(id) ON DELETE SET NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT greetings_user_ctx_date_uq UNIQUE (user_id, context, activity_date)
);

-- 주문 — 모든 구매의 단일 진입점 (DB_REFACTOR §B.2)
-- currency: KRW = IAP 건초 구매(실결제) / HAY = 상점 꾸미기 구매(재화 차감)
CREATE TABLE public.orders (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid    NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  currency     text    NOT NULL CHECK (currency IN ('KRW','HAY')),
  status       text    NOT NULL CHECK (status IN ('pending','paid','failed','refunded')),
  total_amount integer NOT NULL CHECK (total_amount >= 0),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_user_created_idx ON public.orders (user_id, created_at DESC);
CREATE TRIGGER orders_set_updated_at BEFORE UPDATE ON public.orders
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE public.order_items (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id   uuid    NOT NULL REFERENCES public.orders(id) ON DELETE CASCADE,
  product_id uuid    NOT NULL REFERENCES public.products(id) ON DELETE RESTRICT,
  quantity   integer NOT NULL DEFAULT 1 CHECK (quantity > 0),
  unit_price integer NOT NULL CHECK (unit_price >= 0)   -- 구매 시점 가격 스냅샷(가격정책 변동 대비)
);
CREATE INDEX order_items_order_idx   ON public.order_items (order_id);
CREATE INDEX order_items_product_idx ON public.order_items (product_id);

CREATE TABLE public.hay_transactions (
  id            bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  user_id       uuid    NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  type          text    NOT NULL CHECK (type IN (
                  'attendance','ad_reward','routine_reward','iap_purchase',
                  'subscription_grant','shop_purchase','refund_revoke','admin_adjustment')),
  amount        integer NOT NULL CHECK (amount <> 0),
  balance_after integer NOT NULL,
  order_id      uuid REFERENCES public.orders(id) ON DELETE SET NULL,  -- 구매 관련 원장만(iap_purchase·shop_purchase)
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX hay_transactions_user_created_idx ON public.hay_transactions (user_id, created_at DESC);
CREATE INDEX hay_transactions_order_idx        ON public.hay_transactions (order_id);

CREATE TABLE public.user_daily_stats (
  id                        bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  user_id                   uuid     NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  activity_date             date     NOT NULL,
  tokens_used               integer  NOT NULL DEFAULT 0,
  ad_reward_count           smallint NOT NULL DEFAULT 0,
  attendance_claimed_at     timestamptz,
  routine_reward_claimed_at timestamptz,
  morning_notified_at       timestamptz,   -- 아침 푸시 발송 멱등 마커(유저×활동일 1회)
  evening_notified_at       timestamptz,   -- 저녁 푸시 발송 멱등 마커
  CONSTRAINT user_daily_stats_user_date_uq UNIQUE (user_id, activity_date)
);

-- ─────────────────────────────────────────────────────────────
-- 4. 구독 · 결제 · 증정
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.subscriptions (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                 uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  plan                    text NOT NULL CHECK (plan IN ('monthly','yearly')),
  status                  text NOT NULL CHECK (status IN ('active','grace_period','expired','revoked')),
  original_transaction_id text NOT NULL UNIQUE,
  latest_transaction_id   text,
  purchased_at            timestamptz,
  expires_at              timestamptz,
  auto_renew_enabled      boolean NOT NULL DEFAULT true,
  environment             text,
  -- 상태 단조 기준(SOMA-372) — 이 시각 이하의 옛 상태 이벤트는 활성 구독을 되돌리지 않는다.
  last_event_at           timestamptz,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX subscriptions_user_idx ON public.subscriptions (user_id);
CREATE TRIGGER subscriptions_set_updated_at BEFORE UPDATE ON public.subscriptions
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE public.subscription_hay_grants (
  id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                     uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  plan                        text NOT NULL CHECK (plan IN ('monthly','yearly')),
  hay_transaction_id          bigint REFERENCES public.hay_transactions(id) ON DELETE SET NULL,
  granted_at                  timestamptz NOT NULL DEFAULT now(),
  -- 환불 회수 멱등(회수 완료 표식)
  revoked_at                  timestamptz,
  clawback_hay_transaction_id bigint REFERENCES public.hay_transactions(id) ON DELETE SET NULL,
  CONSTRAINT subscription_hay_grants_user_plan_uq UNIQUE (user_id, plan)
);

-- 실결제(현금) 기록 — IAP 건초 + 구독 결제(구매·갱신) 통합 (DB_REFACTOR §B.3)
-- 매출 집계 = payments 단일 테이블.
CREATE TABLE public.payments (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  -- CASCADE 사유: order/subscription 삭제는 회원탈퇴 CASCADE 경로뿐 — SET NULL이면 탈퇴 중 target_ck 위반
  order_id             uuid REFERENCES public.orders(id) ON DELETE CASCADE,          -- IAP 건초 주문과 1:1
  subscription_id      uuid REFERENCES public.subscriptions(id) ON DELETE CASCADE,   -- 구독 결제(구매·갱신)
  store                text NOT NULL,               -- 실제 스토어(app_store|play_store|…). 코드가 항상 명시
  store_transaction_id text NOT NULL UNIQUE,   -- 멱등 키(영수증 중복 지급 방지)
  amount               numeric(14,4),          -- 결제금액(원통화·무손실). 이벤트에 없으면 NULL
  currency             text,                   -- 구매 통화(ISO 4217). 미확인이면 NULL(KRW로 확정 금지)
  status               text NOT NULL CHECK (status IN ('paid','refunded')),
  paid_at              timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT payments_target_ck CHECK (order_id IS NOT NULL OR subscription_id IS NOT NULL)
);
CREATE INDEX payments_user_idx         ON public.payments (user_id);
CREATE INDEX payments_order_idx        ON public.payments (order_id);
CREATE INDEX payments_subscription_idx ON public.payments (subscription_id);
CREATE INDEX payments_store_idx        ON public.payments (store);

-- ─────────────────────────────────────────────────────────────
-- 5. 인벤토리 + 장착 — user_items (보유 + 장착 상태 통합)
--    equipped_slot NULL = 미장착. theme은 사용자마다 항상 1개 장착한다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.user_items (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  product_id    uuid NOT NULL REFERENCES public.products(id) ON DELETE CASCADE,
  source        text NOT NULL DEFAULT 'purchase' CHECK (source IN ('purchase','subscription','admin_grant')),
  order_id      uuid REFERENCES public.orders(id) ON DELETE SET NULL,
  equipped_slot text CHECK (equipped_slot IN ('theme','hat','glasses','neck','body')),
  equipped_at   timestamptz,
  acquired_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT user_items_user_product_uq UNIQUE (user_id, product_id),
  -- ERD §4.9 승계: 슬롯 일치를 DB가 강제(복합 FK — equipped_slot NULL이면 미평가)
  CONSTRAINT user_items_product_slot_fk
    FOREIGN KEY (product_id, equipped_slot) REFERENCES public.products(id, slot),
  CONSTRAINT user_items_equipped_ck CHECK (equipped_at IS NULL OR equipped_slot IS NOT NULL)
);
CREATE INDEX user_items_user_idx    ON public.user_items (user_id);
CREATE INDEX user_items_product_idx ON public.user_items (product_id);
CREATE INDEX user_items_order_idx   ON public.user_items (order_id);
-- 슬롯당 1개 장착
CREATE UNIQUE INDEX user_items_user_equipped_slot_uq
  ON public.user_items (user_id, equipped_slot) WHERE equipped_slot IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- 6. 일기
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.diaries (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  diary_date     date NOT NULL,
  source         text NOT NULL CHECK (source IN ('llm','preset','welcome','none')),
  preset_ment_id uuid REFERENCES public.moly_life_ments(id) ON DELETE SET NULL,
  content        text NOT NULL,
  weather        text NOT NULL CHECK (weather IN ('sunny','cloudy','rainy','windy')),
  published_at   timestamptz,
  first_read_at  timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT diaries_user_date_uq UNIQUE (user_id, diary_date)
);
CREATE INDEX diaries_user_published_idx ON public.diaries (user_id, published_at);
CREATE INDEX diaries_content_trgm_idx ON public.diaries USING gin (content gin_trgm_ops);

-- ─────────────────────────────────────────────────────────────
-- 7. 루틴
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.routines (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid     NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  name               text     NOT NULL,
  name_i18n          jsonb CONSTRAINT routines_name_i18n_obj_ck CHECK (name_i18n IS NULL OR jsonb_typeof(name_i18n) = 'object'),  -- 기본 루틴 다국어, object만(SOMA-346)
  frequency_per_week smallint NOT NULL,            -- 항상 요일 수(응답 하위호환용)
  days_of_week       smallint[] NOT NULL,          -- 지정 요일(ISO 1=월…7=일)
  reminder_enabled   boolean  NOT NULL DEFAULT false,
  reminder_time      time,
  deleted_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX routines_user_idx ON public.routines (user_id);
CREATE TRIGGER routines_set_updated_at BEFORE UPDATE ON public.routines
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 일기 생성 클레임 — 워커 틱 중첩 시 (유저,날짜) 일기 중복 LLM 생성 방지(SOMA-373).
-- 커밋된 행 기반 상호배제(세션 advisory lock은 풀링·pgbouncer와 불호환). 30분 만료로 크래시 회수.
CREATE TABLE public.diary_gen_claims (
  user_id     uuid        NOT NULL,
  target_date date        NOT NULL,
  claimed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, target_date)
);

CREATE TABLE public.routine_completions (
  id            bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  routine_id    uuid NOT NULL REFERENCES public.routines(id) ON DELETE CASCADE,
  user_id       uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  activity_date date NOT NULL,
  completed_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT routine_completions_routine_date_uq UNIQUE (routine_id, activity_date)
);
CREATE INDEX routine_completions_routine_idx ON public.routine_completions (routine_id);
CREATE INDEX routine_completions_user_idx    ON public.routine_completions (user_id);

-- ─────────────────────────────────────────────────────────────
-- 8. 알림 · 디바이스
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.user_notification_settings (
  user_id uuid    NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  type    text    NOT NULL CHECK (type IN ('morning_diary','evening_chat')),
  enabled boolean NOT NULL DEFAULT true,
  PRIMARY KEY (user_id, type)
);

CREATE TABLE public.user_devices (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  platform       text NOT NULL CHECK (platform IN ('ios', 'android')),
  push_token     text NOT NULL UNIQUE,
  last_active_at timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX user_devices_user_idx ON public.user_devices (user_id);

-- ─────────────────────────────────────────────────────────────
-- 9. 광고 SSV · 멱등키 (ERD 밖, 백엔드 신규)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.reward_ad_sessions (
  session_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  activity_date      date NOT NULL,
  ssv_transaction_id text UNIQUE,           -- SSV 도착 시 기록(재전송 멱등)
  granted            boolean NOT NULL DEFAULT false,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX reward_ad_sessions_user_idx ON public.reward_ad_sessions (user_id);

-- 대화 컨텍스트 상태(앵커 append-only + 정규화 기억 처리 좌표).
CREATE TABLE public.chat_contexts (
  user_id             uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  anchor_message_id   bigint NOT NULL DEFAULT 0 CHECK (anchor_message_id >= 0),
  memory_generation   bigint NOT NULL DEFAULT 0,       -- forget마다 +1 → stale 잡 결과 폐기
  memory_source_watermark bigint NOT NULL DEFAULT 0,   -- 대화 turn당 +1(memory_source_turns 배정)
  -- fact/insight의 실제 내용·source·상태 변경 트랜잭션에서만 정확히 +1(no-op·retry·재색인은 제외)
  relationship_profile_input_revision bigint NOT NULL DEFAULT 0,
  last_active_at       timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON public.chat_contexts FROM anon, authenticated;

CREATE TABLE public.idempotency_keys (
  user_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  key        text NOT NULL,
  response   jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);

-- RC 웹훅 내구 inbox(SOMA-372) — 엔드포인트가 event.id를 PK로 raw 커밋(중복 웹훅 멱등) 후
-- process_event가 소비. DB 오류 삼킴에 의한 구독·결제·건초 영구 유실 차단. 미결 pending·미해결
-- failed는 워커가 매 틱 관측(은폐 없음). FK 없음(RC 소유 식별자·유저 매핑은 payload 안).
CREATE TABLE public.revenuecat_events (
  event_id     text        PRIMARY KEY,               -- RC event.id(전역 유일)
  payload      jsonb       NOT NULL,                  -- 이벤트 원문(운영 수동 처리·재처리 근거)
  status       text        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','processed','failed')),
  attempts     integer     NOT NULL DEFAULT 0,        -- 예상 밖 예외 재시도 횟수(≥5 → failed)
  received_at  timestamptz NOT NULL DEFAULT now(),
  next_attempt_at timestamptz NOT NULL DEFAULT now(), -- 다음 재시도 예약(backoff) — 드레인 후보·정렬 기준(rotation)
  processed_at timestamptz,                           -- processed/failed 확정 시각
  last_error   text                                   -- 실패 사유 또는 no-op durable reason
);
CREATE INDEX revenuecat_events_status_idx ON public.revenuecat_events (status, received_at);
-- 드레인 후보 인덱스 — pending 중 next_attempt_at <= now() 스캔·정렬(집합 내부 rotation, SOMA-372).
CREATE INDEX revenuecat_events_status_next_attempt_idx ON public.revenuecat_events (status, next_attempt_at);

-- 인앱 문의(자유 텍스트). contact = 기프티콘 이벤트용 선택 연락처(이메일·전화·인스타 등).
CREATE TABLE public.feedback (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  message    text NOT NULL CHECK (char_length(message) <= 2000),
  contact    text CHECK (char_length(contact) <= 200),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feedback_user_idx ON public.feedback (user_id);

-- 잡 플랫폼(W7) — 대화 후속 처리(기억 추출 등)의 내구 큐. 큐 5종은 스키마가 아니라 queue 컬럼 값이며
-- 소비자 내부 슬롯으로만 분리한다. attempt는 **claim 시점**에 증가해(크래시로 finalize 못 한 잡도
-- 재클레임마다 카운트) 반드시 dead에 도달한다(poison job 무한루프 방지). finalize/heartbeat은
-- (id, state='running', lease_owner, lease_token) fencing으로만 쓴다 — lease 잃은 늦은 소비자가
-- 잘못 확정 못 함. terminal(succeeded/dead/cancelled)에서 같은 행을 ready로 되살리지 않고,
-- 운영 replay는 dedup_key='replay:{old_job_id}:{operation_id}'인 새 행으로만. dead 자동 삭제 없음.
CREATE TABLE public.async_jobs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  queue         text NOT NULL,                       -- critical|interactive_async|content|notification|maintenance
  job_type      text NOT NULL,
  user_id       uuid NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  dedup_key     text NOT NULL,
  replay_of     uuid NULL REFERENCES public.async_jobs(id), -- terminal 원본을 보존하는 replay 계보
  payload       jsonb NOT NULL,
  state         text NOT NULL DEFAULT 'ready'
    CHECK (state IN ('ready','running','succeeded','dead','cancelled')),
  priority      integer NOT NULL DEFAULT 100,        -- 작을수록 먼저
  available_at  timestamptz NOT NULL DEFAULT now(),  -- 재시도 backoff 예약 시각
  expires_at    timestamptz NULL,                    -- 경과 시 cancelled(처리 의미 없어진 잡)
  attempt       integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts  integer NOT NULL CHECK (max_attempts > 0),
  lease_owner   text NULL,
  lease_token   uuid NULL,
  lease_until   timestamptz NULL,
  result_code   text NULL,
  result_detail jsonb NULL,
  last_error_code text NULL,
  last_error_at timestamptz NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz NULL,
  UNIQUE (job_type, dedup_key),                      -- 멱등 키(구·신 producer 겹침 구간 합류점)
  CHECK (                                            -- lease 3종은 running일 때만 존재
    (state = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
    OR (state <> 'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL)
  )
);
CREATE INDEX async_jobs_claim_idx
  ON public.async_jobs (queue, priority, available_at, created_at) WHERE state='ready';
CREATE INDEX async_jobs_reclaim_idx
  ON public.async_jobs (queue, lease_until) WHERE state='running';
-- /health/queues 큐별 집계·oldest dead age(이관 게이트). dead 미삭제로 단조 증가하는 테이블의 풀스캔 방지.
CREATE INDEX async_jobs_state_queue_idx ON public.async_jobs (state, queue);
CREATE INDEX async_jobs_replay_of_idx ON public.async_jobs (replay_of) WHERE replay_of IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- 10. 정규화 기억 — 턴 단위 구조화 사실과 검색용 pgvector 파생 인덱스.
--     판정(ADD/REINFORCE/SUPERSEDE/KEEP_BOTH/IGNORE)은 LLM이 아니라 코드가 한다.
--     · fact의 (normalization_version, content_hash)와 marker의 (normalization_version,
--       normalized_hash)는 **같은 산출물**이다(공용 해시 함수 1개). forget은 fact 값을 그대로 복사.
--     · fact의 forgotten/superseded, insight의 invalidated/superseded는 terminal(되살리지 않는다).
--     · watermark는 대화 turn당 하나, message는 정확히 한 watermark에만 속한다.
--     · v1 추출 소스는 conversation_turn만(일기·루틴은 자체 watermark/closure 계약 전까지 제외).
--     상세 = docs/ARCHITECTURE-capi.md 12장(legacy 정규화 기억 — 폐기).
-- ─────────────────────────────────────────────────────────────
-- fact 임베딩용(무차원 vector — 차원 고정은 embedder 마이그레이션에서 별도 검증).
CREATE TABLE public.memory_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  kind text NOT NULL,                       -- profile|preference|relationship|event|emotion (코드 registry)
  canonical_text text NOT NULL,             -- 자연어 표면 — 저장 직전 naming.to_placeholder + 살균 강제
  subject text NULL,
  predicate text NULL,                      -- 코드 registry의 canonical key(cardinality single|multi)
  object_json jsonb NULL,                   -- predicate와 함께 있거나 함께 없다(스키마 검증)
  event_time timestamptz NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','superseded','forgotten')),
  importance double precision NOT NULL,
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  content_hash text NOT NULL,               -- = marker.normalized_hash와 같은 산출물
  normalization_version text NOT NULL,      -- 제자리 재해시 금지(구 normalizer는 registry에 영구 보관)
  superseded_by uuid NULL,
  embedding vector(1536) NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, id),                     -- 아래 복합 FK들이 user_id를 함께 태우기 위한 대상 키
  FOREIGN KEY (user_id, superseded_by) REFERENCES public.memory_facts(user_id, id) ON DELETE RESTRICT,
  CHECK ((status='active' AND valid_to IS NULL) OR status<>'active')
);
CREATE INDEX memory_facts_active_user_idx
  ON public.memory_facts(user_id, predicate, event_time) WHERE status='active';
CREATE INDEX memory_facts_hash_idx
  ON public.memory_facts(user_id, normalization_version, content_hash);
CREATE INDEX memory_facts_embedding_hnsw_idx
  ON public.memory_facts USING hnsw (embedding vector_cosine_ops)
  WHERE status='active' AND embedding IS NOT NULL;

-- 근거. FK가 user_id를 태우지 않으므로(messages PK=(id)) **코드가 트랜잭션 안에서
-- messages.user_id = fact.user_id를 반드시 검증한다** — DB 제약만으로는 타 유저 메시지를 못 막는다.
CREATE TABLE public.memory_evidence (
  fact_id uuid NOT NULL REFERENCES public.memory_facts(id) ON DELETE RESTRICT,
  source_type text NOT NULL CHECK (source_type='conversation_turn'),  -- v1은 이 값만
  source_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  source_excerpt_hash text NOT NULL,
  observed_at timestamptz NOT NULL,
  PRIMARY KEY (fact_id, source_type, source_id)
);

CREATE TABLE public.memory_insights (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  text text NOT NULL,
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','invalidated','superseded')),
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz NULL,
  derivation_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, id),
  CHECK ((status='active' AND valid_to IS NULL) OR status<>'active')
);

CREATE TABLE public.memory_insight_sources (
  user_id uuid NOT NULL,
  insight_id uuid NOT NULL,
  fact_id uuid NOT NULL,
  PRIMARY KEY (user_id, insight_id, fact_id),
  -- 복합 FK(user_id 동반) — 타 유저 fact를 근거로 다는 경로를 스키마가 막는다.
  FOREIGN KEY (user_id, insight_id) REFERENCES public.memory_insights(user_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, fact_id) REFERENCES public.memory_facts(user_id, id) ON DELETE RESTRICT
);

-- 망각 마커 = "잊어달라"의 영속 deny key. 검색·추출 hard filter가 모든 LLM 제안보다 먼저 본다.
-- expires_at은 항상 NULL(CHECK) — 잊은 사실이 만료로 되살아나지 않는다. fact_id FK는 DEFERRABLE
-- INITIALLY DEFERRED라 retention 삭제 시 marker를 마지막 statement로 지울 수 있다.
CREATE TABLE public.memory_forget_markers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  scope text NOT NULL CHECK (scope IN ('fact','predicate','all')),
  fact_id uuid NULL,
  normalized_hash text NULL,                -- = memory_facts.content_hash 복사본
  normalization_version text NULL,          -- = memory_facts.normalization_version 복사본
  predicate text NULL,
  memory_generation bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NULL,
  FOREIGN KEY (user_id, fact_id) REFERENCES public.memory_facts(user_id, id)
    ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
  CHECK (
    (scope='fact' AND fact_id IS NOT NULL AND normalized_hash IS NOT NULL
                  AND normalization_version IS NOT NULL AND predicate IS NULL)
    OR (scope='predicate' AND fact_id IS NULL AND normalized_hash IS NULL
                          AND normalization_version IS NULL AND predicate IS NOT NULL)
    OR (scope='all' AND fact_id IS NULL AND normalized_hash IS NULL
                    AND normalization_version IS NULL AND predicate IS NULL)
  ),
  CHECK (expires_at IS NULL)
);
CREATE INDEX memory_forget_markers_match_idx
  ON public.memory_forget_markers(user_id, scope, normalization_version, normalized_hash, predicate);

-- 소스 watermark — 대화 turn당 정확히 하나. representative_message_id는 그 turn을 시작한
-- inbound user message다. 이 메시지가 없는 turn(선발화만 등)은 추출 소스로 enqueue하지 않는다.
CREATE TABLE public.memory_source_turns (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  source_watermark bigint NOT NULL CHECK (source_watermark > 0),
  representative_message_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  committed_at timestamptz NOT NULL,
  PRIMARY KEY (user_id, source_watermark),
  UNIQUE (user_id, representative_message_id)
);

CREATE TABLE public.memory_source_turn_messages (
  user_id uuid NOT NULL,
  source_watermark bigint NOT NULL,
  message_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  PRIMARY KEY (user_id, source_watermark, message_id),
  UNIQUE (user_id, message_id),             -- 한 message는 정확히 한 watermark에만 속한다
  FOREIGN KEY (user_id, source_watermark)
    REFERENCES public.memory_source_turns(user_id, source_watermark) ON DELETE RESTRICT
);

-- forget이 닫은 소스 구간. 추출 배치의 중간 watermark가 하나라도 겹치면 **부분 publish 금지** —
-- 전체를 source_range_closed로 끝내고 열린 source만 새 generation job으로 다시 묶는다.
CREATE TABLE public.memory_source_closures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  source_kind text NOT NULL CHECK (source_kind='conversation_turn'),
  from_watermark bigint NOT NULL,
  through_watermark bigint NOT NULL,
  forget_operation_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (from_watermark <= through_watermark),
  UNIQUE (user_id, forget_operation_id, source_kind, from_watermark, through_watermark)
);
CREATE INDEX memory_source_closures_overlap_idx
  ON public.memory_source_closures(user_id, source_kind, from_watermark, through_watermark);

-- ─────────────────────────────────────────────────────────────
-- 11. 관계 프로필(W9) — 정규화 기억에서 파생한 **칸 고정** 프롬프트 투영.
--     stance / known_facts(≤5) / recent_threads(≤3) / inferred_tendencies(≤2), 렌더 ≤400토큰.
--     · locale당 published는 정확히 1개(부분 유니크 인덱스가 동시성까지 강제).
--     · invalidated/superseded는 terminal(되돌리는 UPDATE 경로 없음).
--     · source FK는 복합키(user_id 동반) — 타 유저 근거를 다는 경로를 스키마가 막는다.
--     상세 = docs/ARCHITECTURE-capi.md 12장(legacy 정규화 기억 — 폐기).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.relationship_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  version bigint NOT NULL CHECK (version > 0),
  locale text NOT NULL,                     -- i18n 버킷(ko|en|ja). 언어를 바꾸면 새 locale 행이 생긴다
  memory_generation bigint NOT NULL,
  relationship_profile_input_revision bigint NOT NULL,
  document_json jsonb NOT NULL,             -- 칸별 항목 + item_key + source_refs(edge와 양방향 일치)
  rendered_text text NOT NULL,              -- 프롬프트에 실리는 문자열(실명 금지 — {유저이름} placeholder)
  render_hash text NOT NULL,                -- 같은 값이면 새 version을 만들지 않는다
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','published','invalidated','superseded')),
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz NULL,
  UNIQUE (user_id, id),                     -- 아래 복합 FK가 user_id를 함께 태우기 위한 대상 키
  UNIQUE (user_id, locale, version),
  CHECK ((status='published' AND published_at IS NOT NULL) OR status<>'published')
);
CREATE UNIQUE INDEX relationship_profiles_one_published_idx
  ON public.relationship_profiles(user_id, locale) WHERE status='published';

-- 근거 간선 — document_json의 source_refs와 type/id/item_key까지 양방향으로 정확히 같아야
-- publish한다. 렌더 시점에도 근거의 active/marker 상태를 다시 대조한다.
CREATE TABLE public.relationship_profile_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  relationship_profile_id uuid NOT NULL,
  item_key text NOT NULL,                   -- 문서 항목의 불변 키(칸 안에서 항목을 식별)
  fact_id uuid NULL,
  insight_id uuid NULL,
  CHECK (num_nonnulls(fact_id, insight_id)=1),
  FOREIGN KEY (user_id, relationship_profile_id)
    REFERENCES public.relationship_profiles(user_id, id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, fact_id) REFERENCES public.memory_facts(user_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, insight_id) REFERENCES public.memory_insights(user_id, id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX relationship_profile_sources_fact_uq
  ON public.relationship_profile_sources(user_id, relationship_profile_id, item_key, fact_id)
  WHERE fact_id IS NOT NULL;
CREATE UNIQUE INDEX relationship_profile_sources_insight_uq
  ON public.relationship_profile_sources(user_id, relationship_profile_id, item_key, insight_id)
  WHERE insight_id IS NOT NULL;
-- RESTRICT 검사(근거 삭제 시 참조 탐색)와 역추적용 — 위 유니크 인덱스는 근거 id 단독 조회를 못 받는다.
CREATE INDEX relationship_profile_sources_fact_idx
  ON public.relationship_profile_sources(user_id, fact_id) WHERE fact_id IS NOT NULL;
CREATE INDEX relationship_profile_sources_insight_idx
  ON public.relationship_profile_sources(user_id, insight_id) WHERE insight_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- 12. 대화 요약 checkpoint(W11) — 길어진 대화의 오래된 구간을 요약해 남긴다.
--     다음 턴은 **가장 앞선 checkpoint 하나 + 그 이후 메시지**만 쓴다(앵커 리셋이 옛 구간을
--     통째로 버리던 문제).
--     · source_hash = (이전 checkpoint id·source_hash) + 이번 원본 메시지의 정렬된
--       (id, sender, kind, placeholder content)를 길이-prefix 직렬화한 SHA-256.
--     · UNIQUE(user_id, through_message_id, source_hash) + 잡 dedup key로 같은 입력은 한 번만 요약.
--     · through_message_id는 RESTRICT — 요약 경계가 되는 메시지는 사라질 수 없다.
--     상세 = docs/ARCHITECTURE-capi.md 8.2절.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE public.conversation_checkpoints (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  through_message_id bigint NOT NULL REFERENCES public.messages(id) ON DELETE RESTRICT,
  summary text NOT NULL,                    -- 저장 표면(실명 금지 — {유저이름} placeholder)
  version text NOT NULL,                    -- 요약기 계약 버전
  source_hash text NOT NULL,                -- 결정적 입력 지문
  -- 만들 때의 chat_contexts.memory_generation. 잊어줘가 세대를 올리므로, 조회는 **현재 세대와
  -- 같은 행만** 돌려준다. 잊어줘의 DELETE와 요약 INSERT가 겹치면 한쪽이 다른 쪽의 미커밋 행을
  -- 못 봐(READ COMMITTED) 삭제를 피한 행이 남는데, 잠금으로 막으면 챗과 락 순서가 반대라
  -- 교착이 난다. 지우는 대신 읽을 때 걸러 프롬프트에 안 실리게 한다.
  memory_generation bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, through_message_id, source_hash)
);
CREATE INDEX conversation_checkpoints_latest_idx
  ON public.conversation_checkpoints(user_id, through_message_id DESC);
CREATE INDEX conversation_checkpoints_live_idx
  ON public.conversation_checkpoints(user_id, memory_generation, through_message_id DESC);

-- ─────────────────────────────────────────────────────────────
-- 13. RLS — deny-default (심층 방어). 서버는 테이블 owner 롤이라 우회.
--     클라 데이터 경로는 전부 서버 API → anon/authenticated 직접 접근 차단.
--     (클라 직접 읽기가 필요해지면 여기에 own-row SELECT 정책 추가)
-- ─────────────────────────────────────────────────────────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'profiles','products','moly_life_ments','app_config',
    'messages','greetings','orders','order_items','hay_transactions','user_daily_stats',
    'subscriptions','subscription_hay_grants','payments',
    'user_items','diaries','routines','routine_completions',
    'user_notification_settings','user_devices','reward_ad_sessions','idempotency_keys',
    'chat_contexts','feedback','diary_gen_claims','revenuecat_events','async_jobs'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', t);
  END LOOP;
  -- 기억 테이블(W8)·관계 프로필(W9)·대화 요약(W11)은 유저 대화에서 뽑은 PII(와 그 파생)라
  -- RLS에 더해 클라 롤 권한도 회수한다(chat_contexts와 동일).
  FOREACH t IN ARRAY ARRAY[
    'memory_facts','memory_evidence','memory_insights','memory_insight_sources',
    'memory_forget_markers','memory_source_turns','memory_source_turn_messages',
    'memory_source_closures',
    'relationship_profiles','relationship_profile_sources',
    'conversation_checkpoints'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated;', t);
  END LOOP;
END $$;

+-- Dev rollout migration: conversation-centred recall, exact suppression, diary prologue,
-- active-turn CAS, typed diary references and bounded retention metadata.
-- Additive where possible. The legacy diary (user_id, diary_date) unique constraint is replaced
-- because welcome and a daily diary must coexist on the same display date.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.schema_migrations (
  migration_name text PRIMARY KEY,
  checksum_sha256 text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  applied_by text NOT NULL DEFAULT current_user
);
ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.schema_migrations FROM anon, authenticated;

-- Relationship origin is the first committed conversation, not auth/profile creation.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS relationship_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS relationship_started_timezone text,
  ADD COLUMN IF NOT EXISTS relationship_display_date date,
  ADD COLUMN IF NOT EXISTS next_diary_due_at timestamptz;

WITH first_turn AS (
  SELECT DISTINCT ON (m.user_id) m.user_id, m.created_at
  FROM public.messages m
  WHERE m.sender='user'
  ORDER BY m.user_id, m.id
)
UPDATE public.profiles p
SET relationship_started_at = f.created_at,
    relationship_started_timezone = p.timezone,
    relationship_display_date = (f.created_at AT TIME ZONE p.timezone)::date
FROM first_turn f
WHERE p.id=f.user_id AND p.relationship_started_at IS NULL;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='profiles_relationship_origin_ck'
      AND conrelid='public.profiles'::regclass
  ) THEN
    ALTER TABLE public.profiles ADD CONSTRAINT profiles_relationship_origin_ck CHECK (
      num_nonnulls(relationship_started_at, relationship_started_timezone, relationship_display_date)
      IN (0, 3)
    );
  END IF;
END $$;

-- Tenant candidate keys and committed-turn coordinates.
ALTER TABLE public.messages
  ADD COLUMN IF NOT EXISTS turn_seq bigint,
  ADD COLUMN IF NOT EXISTS turn_position smallint;
CREATE UNIQUE INDEX IF NOT EXISTS messages_user_id_id_uq ON public.messages(user_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS messages_user_id_id_sender_uq ON public.messages(user_id,id,sender);
CREATE UNIQUE INDEX IF NOT EXISTS messages_user_turn_position_uq
  ON public.messages(user_id,turn_seq,turn_position) WHERE turn_seq IS NOT NULL;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='messages_turn_position_ck'
      AND conrelid='public.messages'::regclass
  ) THEN
    ALTER TABLE public.messages ADD CONSTRAINT messages_turn_position_ck
      CHECK ((turn_seq IS NULL AND turn_position IS NULL)
             OR (turn_seq > 0 AND turn_position BETWEEN 0 AND 2));
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS routines_user_id_id_uq ON public.routines(user_id,id);
DELETE FROM public.routine_completions c
USING public.routines r
WHERE c.routine_id=r.id AND c.user_id<>r.user_id;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='routine_completions_user_routine_fk'
      AND conrelid='public.routine_completions'::regclass
  ) THEN
    ALTER TABLE public.routine_completions
      ADD CONSTRAINT routine_completions_user_routine_fk
      FOREIGN KEY (user_id,routine_id) REFERENCES public.routines(user_id,id) ON DELETE CASCADE;
  END IF;
END $$;

-- Diary: product kind and display/activity coordinates replace the single daily slot.
ALTER TABLE public.diaries
  ADD COLUMN IF NOT EXISTS kind text,
  ADD COLUMN IF NOT EXISTS activity_date date,
  ADD COLUMN IF NOT EXISTS display_date date,
  ADD COLUMN IF NOT EXISTS title text,
  ADD COLUMN IF NOT EXISTS author text NOT NULL DEFAULT 'capi',
  ADD COLUMN IF NOT EXISTS occurred_at timestamptz,
  ADD COLUMN IF NOT EXISTS occurred_timezone text,
  ADD COLUMN IF NOT EXISTS occurred_timezone_provenance text,
  ADD COLUMN IF NOT EXISTS primary_subject text,
  ADD COLUMN IF NOT EXISTS about_tags text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS content_version integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS record_status text NOT NULL DEFAULT 'published',
  ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

UPDATE public.diaries SET
  kind = CASE source WHEN 'welcome' THEN 'welcome' WHEN 'llm' THEN 'shared_day'
                     WHEN 'preset' THEN 'capi_day' ELSE NULL END,
  activity_date = CASE WHEN source IN ('llm','preset') THEN diary_date ELSE NULL END,
  display_date = diary_date,
  author = 'capi',
  occurred_timezone_provenance = COALESCE(occurred_timezone_provenance, 'legacy_unknown'),
  primary_subject = CASE WHEN source IN ('welcome','llm') THEN 'user' ELSE 'capi' END,
  about_tags = CASE WHEN source IN ('welcome','llm') THEN ARRAY['user']::text[]
                    WHEN source='preset' THEN ARRAY['capi']::text[] ELSE '{}'::text[] END,
  record_status = CASE WHEN source='none' THEN 'processed' ELSE 'published' END
WHERE display_date IS NULL OR kind IS NULL;

ALTER TABLE public.diaries ALTER COLUMN display_date SET NOT NULL;
ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq;
CREATE UNIQUE INDEX IF NOT EXISTS diaries_user_id_id_uq ON public.diaries(user_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS diaries_one_welcome_uq
  ON public.diaries(user_id) WHERE kind='welcome' AND deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS diaries_one_daily_uq
  ON public.diaries(user_id,activity_date)
  WHERE kind IN ('shared_day','capi_day') AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS diaries_user_display_cursor_idx
  ON public.diaries(user_id,display_date DESC,id DESC)
  WHERE record_status='published' AND deleted_at IS NULL;
DROP INDEX IF EXISTS public.diaries_content_trgm_idx;

CREATE TABLE IF NOT EXISTS public.diary_generation_results (
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  target_date date NOT NULL,
  status text NOT NULL CHECK (status IN ('no_entry')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,target_date)
);
INSERT INTO public.diary_generation_results(user_id,target_date,status,created_at)
SELECT user_id,diary_date,'no_entry',COALESCE(created_at,now())
FROM public.diaries WHERE source='none'
ON CONFLICT (user_id,target_date) DO NOTHING;
DELETE FROM public.diaries WHERE source='none';

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='diaries_kind_ck'
      AND conrelid='public.diaries'::regclass
  ) THEN
    ALTER TABLE public.diaries ADD CONSTRAINT diaries_kind_ck CHECK (
      (record_status='processed' AND kind IS NULL)
      OR (record_status IN ('draft','published') AND kind IN ('welcome','shared_day','capi_day'))
      OR (record_status='deleted')
    );
    ALTER TABLE public.diaries ADD CONSTRAINT diaries_kind_activity_ck CHECK (
      (kind='welcome' AND activity_date IS NULL)
      OR (kind IN ('shared_day','capi_day') AND activity_date IS NOT NULL)
      OR kind IS NULL
    );
    ALTER TABLE public.diaries ADD CONSTRAINT diaries_author_ck CHECK (author='capi');
  END IF;
END $$;

-- Chat revision, one active inference lease per user and exact idempotency semantics.
ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS context_revision bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_committed_turn_seq bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS prompt_cache_generation bigint NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS public.chat_active_turns (
  user_id uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  turn_seq bigint NOT NULL CHECK (turn_seq > 0),
  idempotency_key text NOT NULL,
  request_hash text NOT NULL,
  base_context_revision bigint NOT NULL,
  lease_token uuid NOT NULL,
  lease_until timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_active_turns_key_uq
  ON public.chat_active_turns(user_id,idempotency_key);

ALTER TABLE public.idempotency_keys
  ALTER COLUMN response DROP NOT NULL,
  ADD COLUMN IF NOT EXISTS request_hash text,
  ADD COLUMN IF NOT EXISTS response_schema_version bigint NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS reply_message_id bigint,
  ADD COLUMN IF NOT EXISTS terminal_status text NOT NULL DEFAULT 'succeeded',
  ADD COLUMN IF NOT EXISTS response_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS dedupe_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS redacted_at timestamptz;

UPDATE public.idempotency_keys
SET response_expires_at=COALESCE(response_expires_at,created_at+interval '24 hours'),
    dedupe_expires_at=COALESCE(dedupe_expires_at,created_at+interval '30 days'),
    reply_message_id=CASE
      WHEN response #>> '{reply,message_id}' ~ '^[0-9]+$'
      THEN (response #>> '{reply,message_id}')::bigint ELSE reply_message_id END
WHERE response_expires_at IS NULL OR dedupe_expires_at IS NULL OR reply_message_id IS NULL;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='idempotency_reply_message_fk'
      AND conrelid='public.idempotency_keys'::regclass
  ) THEN
    ALTER TABLE public.idempotency_keys ADD CONSTRAINT idempotency_reply_message_fk
      FOREIGN KEY (user_id,reply_message_id) REFERENCES public.messages(user_id,id)
      ON DELETE CASCADE;
    ALTER TABLE public.idempotency_keys ADD CONSTRAINT idempotency_terminal_status_ck
      CHECK (terminal_status IN ('succeeded','expired','redacted'));
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idempotency_reply_idx
  ON public.idempotency_keys(user_id,reply_message_id) WHERE reply_message_id IS NOT NULL;

-- Extraction provenance becomes tenant-safe and accepts user assertions only.
ALTER TABLE public.memory_facts ADD COLUMN IF NOT EXISTS learned_at_watermark bigint;
UPDATE public.memory_facts f SET learned_at_watermark=x.max_wm
FROM (
  SELECT e.fact_id,max(tm.source_watermark) max_wm
  FROM public.memory_evidence e
  JOIN public.memory_source_turn_messages tm ON tm.message_id=e.source_id
  GROUP BY e.fact_id
) x WHERE x.fact_id=f.id AND f.learned_at_watermark IS NULL;

ALTER TABLE public.memory_evidence
  ADD COLUMN IF NOT EXISTS user_id uuid,
  ADD COLUMN IF NOT EXISTS source_sender text,
  ADD COLUMN IF NOT EXISTS span_start integer,
  ADD COLUMN IF NOT EXISTS span_end integer,
  ADD COLUMN IF NOT EXISTS extractor_version text NOT NULL DEFAULT 'memory-extract-v1',
  ADD COLUMN IF NOT EXISTS prompt_version text NOT NULL DEFAULT 'memory-prompt-v1';
UPDATE public.memory_evidence e SET
  user_id=f.user_id,
  source_sender=m.sender
FROM public.memory_facts f, public.messages m
WHERE e.fact_id=f.id AND e.source_id=m.id
  AND (e.user_id IS NULL OR e.source_sender IS NULL);
DELETE FROM public.memory_evidence WHERE source_sender<>'user' OR user_id IS NULL;
UPDATE public.memory_facts f SET status='forgotten',valid_to=now(),updated_at=now()
WHERE f.status='active' AND NOT EXISTS (
  SELECT 1 FROM public.memory_evidence e WHERE e.fact_id=f.id
);
ALTER TABLE public.memory_evidence ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.memory_evidence ALTER COLUMN source_sender SET NOT NULL;
ALTER TABLE public.memory_evidence DROP CONSTRAINT IF EXISTS memory_evidence_pkey;
ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_pkey
  PRIMARY KEY (user_id,fact_id,source_type,source_id);

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_evidence_user_fact_fk'
      AND conrelid='public.memory_evidence'::regclass
  ) THEN
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_user_fact_fk
      FOREIGN KEY (user_id,fact_id) REFERENCES public.memory_facts(user_id,id) ON DELETE CASCADE;
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_user_message_fk
      FOREIGN KEY (user_id,source_id,source_sender)
      REFERENCES public.messages(user_id,id,sender) ON DELETE CASCADE;
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_user_sender_ck
      CHECK (source_sender='user');
    ALTER TABLE public.memory_evidence ADD CONSTRAINT memory_evidence_span_ck CHECK (
      (span_start IS NULL AND span_end IS NULL)
      OR (span_start >= 0 AND span_end > span_start)
    );
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_facts_quality_ck'
      AND conrelid='public.memory_facts'::regclass
  ) THEN
    ALTER TABLE public.memory_facts ADD CONSTRAINT memory_facts_quality_ck
      CHECK (importance BETWEEN 0 AND 1 AND confidence BETWEEN 0 AND 1);
    ALTER TABLE public.memory_facts ADD CONSTRAINT memory_facts_object_pair_ck
      CHECK ((predicate IS NULL)=(object_json IS NULL));
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS memory_facts_active_hash_uq
  ON public.memory_facts(user_id,normalization_version,content_hash) WHERE status='active';

-- Forget markers fence old learning; exact recall suppression is a separate message/span surface.
ALTER TABLE public.memory_forget_markers
  ADD COLUMN IF NOT EXISTS cut_watermark bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS future_learning text NOT NULL DEFAULT 'block';
UPDATE public.memory_forget_markers m
SET cut_watermark=c.memory_source_watermark
FROM public.chat_contexts c
WHERE c.user_id=m.user_id AND m.cut_watermark=0;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_forget_future_learning_ck'
      AND conrelid='public.memory_forget_markers'::regclass
  ) THEN
    ALTER TABLE public.memory_forget_markers ADD CONSTRAINT memory_forget_future_learning_ck
      CHECK (future_learning IN ('allow','block'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.memory_suppression_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  cut_watermark bigint NOT NULL CHECK (cut_watermark >= 0),
  future_learning text NOT NULL CHECK (future_learning IN ('allow','block')),
  scope text NOT NULL CHECK (scope IN ('fact','predicate','all')),
  reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.memory_recall_suppressions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  operation_id uuid NOT NULL REFERENCES public.memory_suppression_operations(id) ON DELETE CASCADE,
  message_id bigint NOT NULL,
  source_watermark bigint NOT NULL,
  span_start integer,
  span_end integer,
  source_hash text NOT NULL,
  reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE,
  CHECK ((span_start IS NULL AND span_end IS NULL)
         OR (span_start >= 0 AND span_end > span_start))
);
CREATE INDEX IF NOT EXISTS memory_recall_suppressions_lookup_idx
  ON public.memory_recall_suppressions(user_id,message_id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_recall_suppressions_operation_uq
  ON public.memory_recall_suppressions(
    user_id,operation_id,message_id,COALESCE(span_start,-1),COALESCE(span_end,-1)
  );

INSERT INTO public.memory_suppression_operations
  (id,user_id,cut_watermark,future_learning,scope,reason,created_at)
SELECT m.id,m.user_id,m.cut_watermark,m.future_learning,m.scope,'legacy_marker_backfill',m.created_at
FROM public.memory_forget_markers m
ON CONFLICT (id) DO NOTHING;

WITH affected AS (
  SELECT DISTINCT m.user_id,m.id operation_id,tm.source_watermark
  FROM public.memory_forget_markers m
  JOIN public.memory_facts f ON f.user_id=m.user_id AND (
    (m.scope='fact' AND f.id=m.fact_id) OR
    (m.scope='predicate' AND f.predicate=m.predicate)
  )
  JOIN public.memory_evidence e ON e.user_id=f.user_id AND e.fact_id=f.id
  JOIN public.memory_source_turn_messages tm
    ON tm.user_id=e.user_id AND tm.message_id=e.source_id
  WHERE m.scope IN ('fact','predicate') AND tm.source_watermark<=m.cut_watermark
  UNION
  SELECT m.user_id,m.id,tm.source_watermark
  FROM public.memory_forget_markers m
  JOIN public.memory_source_turn_messages tm
    ON tm.user_id=m.user_id AND tm.source_watermark<=m.cut_watermark
  WHERE m.scope='all'
)
INSERT INTO public.memory_recall_suppressions
  (user_id,operation_id,message_id,source_watermark,source_hash,reason)
SELECT a.user_id,a.operation_id,tm.message_id,a.source_watermark,
       encode(digest(COALESCE(msg.content,''),'sha256'),'hex'),'legacy_marker_backfill'
FROM affected a
JOIN public.memory_source_turn_messages tm
  ON tm.user_id=a.user_id AND tm.source_watermark=a.source_watermark
JOIN public.messages msg ON msg.user_id=tm.user_id AND msg.id=tm.message_id
ON CONFLICT DO NOTHING;

-- Episodic projection stores no duplicate raw text; query joins messages after suppression/hash validation.
CREATE TABLE IF NOT EXISTS public.memory_episodic_messages (
  user_id uuid NOT NULL,
  message_id bigint NOT NULL,
  source_watermark bigint NOT NULL,
  content_hash text NOT NULL,
  embedding vector(1536),
  embedding_model text NOT NULL,
  index_version text NOT NULL,
  suppression_generation bigint NOT NULL,
  embedding_repair_attempts smallint NOT NULL DEFAULT 0 CHECK (embedding_repair_attempts BETWEEN 0 AND 3),
  indexed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,message_id),
  FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_episodic_embedding_hnsw_idx
  ON public.memory_episodic_messages USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_episodic_missing_embedding_idx
  ON public.memory_episodic_messages(indexed_at) WHERE embedding IS NULL;

-- Diary provenance and suppression-safe recall projection.
CREATE TABLE IF NOT EXISTS public.diary_claim_sources (
  user_id uuid NOT NULL,
  diary_id uuid NOT NULL,
  message_id bigint NOT NULL,
  source_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,diary_id,message_id),
  FOREIGN KEY (user_id,diary_id) REFERENCES public.diaries(user_id,id) ON DELETE CASCADE,
  FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS public.diary_recall_documents (
  user_id uuid NOT NULL,
  diary_id uuid NOT NULL,
  search_text text NOT NULL,
  source_hash text NOT NULL,
  embedding vector(1536),
  embedding_model text NOT NULL DEFAULT 'text-embedding-3-small',
  suppression_generation bigint NOT NULL,
  index_version text NOT NULL,
  embedding_repair_attempts smallint NOT NULL DEFAULT 0 CHECK (embedding_repair_attempts BETWEEN 0 AND 3),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id,diary_id),
  FOREIGN KEY (user_id,diary_id) REFERENCES public.diaries(user_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS diary_recall_documents_text_trgm_idx
  ON public.diary_recall_documents USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS diary_recall_documents_embedding_hnsw_idx
  ON public.diary_recall_documents USING hnsw (embedding vector_cosine_ops)
  WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS diary_recall_missing_embedding_idx
  ON public.diary_recall_documents(updated_at) WHERE embedding IS NULL;

-- Persisted public diary cards and short-lived conversational focus.
CREATE TABLE IF NOT EXISTS public.chat_response_references (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  reply_message_id bigint NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 2),
  schema_version text NOT NULL DEFAULT 'diary-reference-v1',
  domain text NOT NULL DEFAULT 'diary' CHECK (domain='diary'),
  mode text NOT NULL CHECK (mode IN ('full_card','reopen_reference')),
  state text NOT NULL DEFAULT 'available' CHECK (state IN ('available','unavailable')),
  diary_id uuid,
  rendered_metadata jsonb NOT NULL DEFAULT '{}',
  redacted_at timestamptz,
  redaction_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (user_id,reply_message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE,
  FOREIGN KEY (user_id,diary_id) REFERENCES public.diaries(user_id,id) ON DELETE RESTRICT,
  UNIQUE (user_id,reply_message_id,ordinal),
  CHECK ((state='available' AND diary_id IS NOT NULL AND redacted_at IS NULL)
         OR (state='unavailable' AND diary_id IS NULL))
);
CREATE INDEX IF NOT EXISTS chat_response_references_reply_idx
  ON public.chat_response_references(user_id,reply_message_id,ordinal);

CREATE TABLE IF NOT EXISTS public.conversation_focus (
  user_id uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  domain text NOT NULL,
  facet text,
  reference_ids uuid[] NOT NULL CHECK (cardinality(reference_ids) BETWEEN 1 AND 3),
  context_revision bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  expires_turn_seq bigint NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Deletion serving barrier is intentionally not FK-bound to profiles so it survives account deletion.
CREATE TABLE IF NOT EXISTS public.privacy_subject_barriers (
  user_id uuid PRIMARY KEY,
  state text NOT NULL CHECK (state IN ('deleting','deleted')),
  operation_id uuid NOT NULL,
  high_watermark bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.privacy_ledger_events (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  operation_id uuid NOT NULL,
  user_id uuid NOT NULL,
  event text NOT NULL,
  high_watermark bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS privacy_ledger_user_idx
  ON public.privacy_ledger_events(user_id,id);

-- Durable job replay lineage and bounded payload retention.
ALTER TABLE public.async_jobs
  ADD COLUMN IF NOT EXISTS replay_of uuid REFERENCES public.async_jobs(id),
  ADD COLUMN IF NOT EXISTS replay_operation_id uuid,
  ADD COLUMN IF NOT EXISTS payload_schema_version text NOT NULL DEFAULT 'job-payload-v1',
  ADD COLUMN IF NOT EXISTS payload_hash text,
  ADD COLUMN IF NOT EXISTS payload_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS payload_redacted_at timestamptz;
UPDATE public.async_jobs SET
  payload_hash=COALESCE(payload_hash,encode(digest(payload::text,'sha256'),'hex')),
  payload_expires_at=COALESCE(payload_expires_at,
    CASE WHEN state='dead' THEN COALESCE(finished_at,created_at)+interval '7 days'
         WHEN state IN ('succeeded','cancelled') THEN COALESCE(finished_at,created_at)+interval '24 hours'
         ELSE NULL END)
WHERE payload_hash IS NULL OR payload_expires_at IS NULL;
CREATE INDEX IF NOT EXISTS async_jobs_replay_of_idx
  ON public.async_jobs(replay_of) WHERE replay_of IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS async_jobs_replay_operation_uq
  ON public.async_jobs(replay_of,replay_operation_id)
  WHERE replay_of IS NOT NULL AND replay_operation_id IS NOT NULL;

-- Composite provenance FKs that were previously app-only.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname='memory_source_turns_user_message_fk'
      AND conrelid='public.memory_source_turns'::regclass
  ) THEN
    ALTER TABLE public.memory_source_turns ADD CONSTRAINT memory_source_turns_user_message_fk
      FOREIGN KEY (user_id,representative_message_id)
      REFERENCES public.messages(user_id,id) ON DELETE CASCADE;
    ALTER TABLE public.memory_source_turn_messages ADD CONSTRAINT memory_source_turn_messages_user_message_fk
      FOREIGN KEY (user_id,message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE;
    ALTER TABLE public.conversation_checkpoints ADD CONSTRAINT conversation_checkpoints_user_message_fk
      FOREIGN KEY (user_id,through_message_id) REFERENCES public.messages(user_id,id) ON DELETE CASCADE;
  END IF;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'schema_migrations','chat_active_turns','chat_response_references','conversation_focus',
    'memory_suppression_operations','memory_recall_suppressions','memory_episodic_messages',
    'diary_generation_results',
    'diary_claim_sources','diary_recall_documents','privacy_subject_barriers','privacy_ledger_events'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;',t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated;',t);
  END LOOP;
END $$;

-- 저녁 푸시 개인화 — 전날 대화 기반 한 줄 문구를 로컬 05시 틱에서 사전 생성, 전날 첫 대화
-- 시각(15분 격자) 슬롯에 발송. 유저당 1행(사이클마다 덮어씀). body는 placeholder 상태만 저장.
-- send_slot 20:00 = 야간(20:00~익일 08:00 첫 대화) 코호트 — 기존 20시 저녁 푸시 분기에서
-- 인라인 처리(실패 시 같은 틱 디폴트 폴백). 재사용 한도(D+3) 정본은 anchor_date 날짜 산술,
-- sent_count는 통계 전용. (마이그레이션: 20260805_push_personalization.sql)
CREATE TABLE IF NOT EXISTS public.push_personalizations (
  user_id     uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  anchor_date date NOT NULL,
  send_slot   time NOT NULL CHECK (send_slot BETWEEN TIME '08:00' AND TIME '20:00'),
  body        text NOT NULL,
  language    text NOT NULL,
  source_kind text NOT NULL CHECK (source_kind IN ('diary','transcript')),
  generated_at timestamptz NOT NULL DEFAULT now(),
  sent_count  int NOT NULL DEFAULT 0,
  last_sent_on date
);
-- body = 유저 대화 파생 PII → RLS + 클라 롤 권한 회수(memory_*와 동일 등급 2).
ALTER TABLE public.push_personalizations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.push_personalizations FROM anon, authenticated;

COMMIT;
