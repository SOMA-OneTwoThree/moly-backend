-- moly-backend 스키마 (Supabase / PostgreSQL)
-- 단일 소스 = app/models/*.py (코드가 실제로 읽고 쓰는 형태). 설계 근거 = docs/ERD.md.
-- enum은 모델이 String 매핑(asyncpg 바인딩 마찰 회피) → DB도 text + CHECK 제약으로 검증.
-- 클라 직접 쓰기 없음(전부 서버 API 경유) → RLS deny-default(심층 방어). 서버는 owner 롤이라 RLS 우회.
-- 실행 전제: 빈 public 스키마(생성만 있음 — 기존 테이블 정리는 docs/DB_RESET_RUNBOOK.md).
-- 실행: psql "<conn>" -f db/schema.sql  (또는 python db/apply.py)

BEGIN;

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

-- 대화 컨텍스트 상태(앵커 append-only + 기억 스냅샷). 기억 평문 사본 → 민감(RLS + REVOKE 아래).
CREATE TABLE public.chat_contexts (
  user_id             uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  anchor_message_id   bigint NOT NULL DEFAULT 0 CHECK (anchor_message_id >= 0),
  memory_text         text,
  memory_refreshed_at timestamptz,
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

-- ─────────────────────────────────────────────────────────────
-- 10. RLS — deny-default (심층 방어). 서버는 테이블 owner 롤이라 우회.
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
END $$;

COMMIT;
