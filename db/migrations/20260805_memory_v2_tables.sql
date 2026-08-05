-- 적용: python db/apply.py db/migrations/20260805_memory_v2_tables.sql --commit
-- 기억 재설계 4단계 (docs/capi-memory-ARCHITECTURE.md 15장 4번) — v2 테이블 additive 생성.
--
-- 이 마이그레이션은 **아무것도 지우지 않는다.** legacy 정규화 기억 테이블은 그대로 두고 새 구조를
-- 옆에 만든다. shadow(5단계) → cutover(10~11단계) → DROP(13단계) 순서로만 제거한다.
--
-- source 좌표는 `(user_id, turn_seq)` 하나다(13.3절). 기존 memory_source_watermark는 전환 동안
-- mapping 검증에만 쓰고 cutover 뒤 제거한다.
BEGIN;

-- ─────────────────────────────────────────────────────────────
-- 1. 사용자별 파이프라인 커서 (13.3절)
--    ingest와 consolidation은 별도 cursor지만 **둘 다 연속적**이다. 숫자 +1을 가정하지 않고
--    source table의 다음 MIN(turn_seq)로 전진한다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.memory_pipeline_states (
  user_id                     uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  -- 성공 chat Phase B가 source row와 같은 transaction에서 전진시킨다.
  source_through_turn_seq     bigint NOT NULL DEFAULT 0 CHECK (source_through_turn_seq >= 0),
  ingest_through_turn_seq     bigint NOT NULL DEFAULT 0 CHECK (ingest_through_turn_seq >= 0),
  consolidated_through_turn_seq bigint NOT NULL DEFAULT 0
    CHECK (consolidated_through_turn_seq >= 0),
  -- stage lease — 외부 호출 동안 advisory lock을 잡지 않기 위한 CAS 좌표(9.2절).
  active_job_id               uuid NULL,
  stage_token                 uuid NULL,
  lease_until                 timestamptz NULL,
  revision                    bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
  -- 삭제 세대. 잡 payload의 epoch와 다르면 finalize하지 않는다(12.3절).
  privacy_epoch               bigint NOT NULL DEFAULT 0 CHECK (privacy_epoch >= 0),
  repair_generation           integer NOT NULL DEFAULT 0 CHECK (repair_generation >= 0),
  -- shadow 진입 시 historical upper를 고정하고 bootstrap 완료 전 live turn 선행 처리를 막는다(15장 5번).
  bootstrap_status            text NOT NULL DEFAULT 'legacy'
    CHECK (bootstrap_status IN ('legacy','collecting','ready')),
  historical_upper_turn_seq   bigint NULL CHECK (historical_upper_turn_seq >= 0),
  -- 사용자별 전환 모드(15장 4번). v2 응답 사용 여부를 이 값이 가른다.
  mode                        text NOT NULL DEFAULT 'legacy'
    CHECK (mode IN ('legacy','shadow','v2')),
  updated_at                  timestamptz NOT NULL DEFAULT now(),
  -- cursor는 뒤로 가지 않는다. consolidation이 ingest를 앞지를 수 없다.
  CHECK (ingest_through_turn_seq <= source_through_turn_seq),
  CHECK (consolidated_through_turn_seq <= ingest_through_turn_seq)
);

CREATE INDEX IF NOT EXISTS memory_pipeline_states_mode_idx
  ON public.memory_pipeline_states (mode, bootstrap_status);
-- freshness SLO 관측 — 아직 안 따라잡은 사용자만 스캔.
CREATE INDEX IF NOT EXISTS memory_pipeline_states_lag_idx
  ON public.memory_pipeline_states (user_id)
  WHERE consolidated_through_turn_seq < source_through_turn_seq;

-- ─────────────────────────────────────────────────────────────
-- 2. ingest 후보 — provider 호출 **전에** 결정 UUID와 함께 stage한다(9.2절).
--    provider 성공 직후 crash에서도 같은 UUID로 수렴시키기 위한 durable plan이다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mem0_ingest_candidates (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  turn_seq               bigint NOT NULL,
  candidate_hash         text NOT NULL,
  schema_version         text NOT NULL,
  extractor_version      text NOT NULL,
  normalizer_version     text NOT NULL,
  -- UUIDv5(collection namespace, user:turn:hash:schema) — retry가 같은 행으로 수렴한다.
  provider_memory_id     uuid NOT NULL,
  -- 정책 검증을 통과한 정규화 텍스트. 1,000 bytes/160 token hard cap은 코드가 강제하고
  -- 초과분은 중간 절단이 아니라 reject다.
  candidate_text         text NOT NULL,
  temporal_proposal_json jsonb NULL,
  event_started_at       timestamptz NULL,
  event_ended_at         timestamptz NULL,
  event_time_precision   text NULL,
  resolved_timezone      text NULL,
  status                 text NOT NULL DEFAULT 'planned'
    CHECK (status IN ('planned','committed','dead')),
  repair_generation      integer NOT NULL DEFAULT 0,
  scrubbed_at            timestamptz NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  -- 복합 FK 대상(자식이 다른 사용자를 가리키지 못하게)
  UNIQUE (id, user_id),
  -- 같은 generation의 중복 plan 차단
  UNIQUE (user_id, turn_seq, candidate_hash, schema_version, repair_generation)
);

CREATE INDEX IF NOT EXISTS mem0_ingest_candidates_open_idx
  ON public.mem0_ingest_candidates (user_id, turn_seq) WHERE status = 'planned';

-- 후보의 근거 span. 사용자 발화만 독립 근거이며 assistant는 context_only다(9.4절).
CREATE TABLE IF NOT EXISTS public.mem0_ingest_candidate_sources (
  candidate_id         uuid NOT NULL,
  user_id              uuid NOT NULL,
  source_message_id    bigint NOT NULL,
  source_sender        text NOT NULL,
  source_content_hash  text NOT NULL,
  evidence_start_utf8  integer NOT NULL,
  evidence_end_utf8    integer NOT NULL,
  authority            text NOT NULL CHECK (authority IN ('explicit_user','confirmed_user')),
  confidence           double precision NULL CHECK (confidence >= 0 AND confidence <= 1),
  created_at           timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (candidate_id, user_id)
    REFERENCES public.mem0_ingest_candidates(id, user_id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, source_message_id, source_sender)
    REFERENCES public.messages(user_id, id, sender) ON DELETE CASCADE,
  CHECK (source_sender = 'user'),
  CHECK (0 <= evidence_start_utf8 AND evidence_start_utf8 < evidence_end_utf8),
  UNIQUE (candidate_id, source_message_id, evidence_start_utf8, evidence_end_utf8)
);

-- ─────────────────────────────────────────────────────────────
-- 3. provider registry — mem0가 candidate-add-only라 **활성 상태를 우리가 관리**한다(9.4절).
--    본문·임베딩을 복제하지 않고 provider id의 수명과 source만 기록한다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.mem0_memory_registry (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                   uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  provider                  text NOT NULL,
  collection_version        text NOT NULL,
  provider_memory_id        uuid NOT NULL,
  source_turn_seq           bigint NOT NULL,
  content_hash              text NOT NULL,
  -- 사건 시각. source 발화 시각과 다르며 근거 없으면 NULL/ambiguous로 둔다(9.4절).
  event_started_at          timestamptz NULL,
  event_ended_at            timestamptz NULL,
  event_time_precision      text NULL,
  resolved_timezone         text NULL,
  temporal_resolver_version text NULL,
  semantic_status           text NOT NULL DEFAULT 'pending'
    CHECK (semantic_status IN
      ('pending','active','duplicate','superseded','ambiguous','excluded','rejected_policy')),
  provider_delete_state     text NOT NULL DEFAULT 'kept'
    CHECK (provider_delete_state IN ('kept','pending','deleted','failed')),
  provider_deleted_at       timestamptz NULL,
  conflict_group_id         uuid NULL,
  duplicate_of_registry_id  uuid NULL,
  superseded_by_registry_id uuid NULL,
  classification_version    text NULL,
  schema_version            text NOT NULL,
  revision                  bigint NOT NULL DEFAULT 0,
  -- source edge에서 결정적으로 재계산하는 projection. provider vector가 지워져도 남는다.
  last_confirmed_at         timestamptz NULL,
  source_count              integer NOT NULL DEFAULT 0 CHECK (source_count >= 0),
  max_source_confidence     double precision NULL,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  -- provider id가 collection 사이에서 전역 unique라고 가정하지 않는다.
  UNIQUE (user_id, provider, collection_version, provider_memory_id),
  UNIQUE (id, user_id)
);

-- 검색 후 registry join — active/ambiguous만 남긴다.
CREATE INDEX IF NOT EXISTS mem0_memory_registry_searchable_idx
  ON public.mem0_memory_registry (user_id, semantic_status, source_turn_seq)
  WHERE semantic_status IN ('active','ambiguous');
-- provider delete backlog 관측(삭제가 늦으면 active가 굶는다).
CREATE INDEX IF NOT EXISTS mem0_memory_registry_delete_backlog_idx
  ON public.mem0_memory_registry (provider_delete_state, updated_at)
  WHERE provider_delete_state = 'pending';
CREATE INDEX IF NOT EXISTS mem0_memory_registry_conflict_idx
  ON public.mem0_memory_registry (conflict_group_id) WHERE conflict_group_id IS NOT NULL;

-- source edge = hydration·tombstone 제외·rerank·감사의 DB 정본. provider metadata는 보조 사본이다.
CREATE TABLE IF NOT EXISTS public.mem0_memory_sources (
  registry_id          uuid NOT NULL,
  user_id              uuid NOT NULL,
  source_turn_seq      bigint NOT NULL,
  source_message_id    bigint NOT NULL,
  source_sender        text NOT NULL,
  evidence_start_utf8  integer NOT NULL,
  evidence_end_utf8    integer NOT NULL,
  source_content_hash  text NOT NULL,
  source_occurred_at   timestamptz NOT NULL,
  source_activity_date date NOT NULL,
  authority            text NOT NULL CHECK (authority IN ('explicit_user','confirmed_user')),
  confidence           double precision NULL CHECK (confidence >= 0 AND confidence <= 1),
  extractor_version    text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (registry_id, user_id)
    REFERENCES public.mem0_memory_registry(id, user_id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, source_message_id, source_sender)
    REFERENCES public.messages(user_id, id, sender) ON DELETE CASCADE,
  CHECK (source_sender = 'user'),
  CHECK (0 <= evidence_start_utf8 AND evidence_start_utf8 < evidence_end_utf8),
  UNIQUE (registry_id, source_message_id, evidence_start_utf8, evidence_end_utf8)
);

CREATE INDEX IF NOT EXISTS mem0_memory_sources_date_idx
  ON public.mem0_memory_sources (user_id, source_activity_date);

-- ─────────────────────────────────────────────────────────────
-- 4. 사용자별 대화 계약 (6.3절) — locale-neutral 정본 + locale render projection.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.user_interaction_contracts (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  version          integer NOT NULL CHECK (version > 0),
  locale           text NOT NULL,
  document_json    jsonb NOT NULL,
  rendered_text    text NOT NULL,
  render_hash      text NOT NULL,
  status           text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','published','superseded','rejected')),
  source_watermark bigint NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  published_at     timestamptz NULL,
  UNIQUE (user_id, locale, version),
  UNIQUE (id, user_id)
);

-- 사용자·locale별 published는 정확히 1개(DB가 강제).
CREATE UNIQUE INDEX IF NOT EXISTS user_interaction_contracts_published_uq
  ON public.user_interaction_contracts (user_id, locale) WHERE status = 'published';

CREATE TABLE IF NOT EXISTS public.user_interaction_contract_items (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contract_id       uuid NOT NULL,
  user_id           uuid NOT NULL,
  item_key          text NOT NULL,
  section           text NOT NULL CHECK (section IN
    ('address_policy','communication_style','comfort_style','boundaries',
     'relationship_frame','durable_commitments')),
  -- typed dial 값. 사용자 raw directive를 그대로 저장·주입하지 않는다(6.2절).
  value_json        jsonb NOT NULL,
  -- 서버 template이 만든 렌더 결과.
  rendered_text     text NOT NULL,
  authority         text NOT NULL
    CHECK (authority IN ('explicit_user','confirmed','repeated_observation')),
  confidence        double precision NULL CHECK (confidence >= 0 AND confidence <= 1),
  effective_from    timestamptz NOT NULL DEFAULT now(),
  effective_to      timestamptz NULL,
  status            text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','superseded','rejected')),
  source_message_id bigint NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (contract_id, user_id)
    REFERENCES public.user_interaction_contracts(id, user_id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, source_message_id)
    REFERENCES public.messages(user_id, id) ON DELETE SET NULL,
  UNIQUE (contract_id, item_key)
);

CREATE INDEX IF NOT EXISTS user_interaction_contract_items_active_idx
  ON public.user_interaction_contract_items (user_id, section) WHERE status = 'active';

-- ─────────────────────────────────────────────────────────────
-- 5. 관계 상태 — LLM 서술이 아니라 event/state로 **결정적으로** 계산한다(7.2절).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.user_relationship_states (
  user_id                 uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  -- 시작일의 정본은 profiles다. 여기엔 계산 편의를 위한 사본만 둔다.
  relationship_started_at timestamptz NULL,
  active_days             integer NOT NULL DEFAULT 0 CHECK (active_days >= 0),
  successful_turns        bigint NOT NULL DEFAULT 0 CHECK (successful_turns >= 0),
  -- stage 계산에만 쓰는 값(하루 최대 10턴 누적). raw 통계와 분리한다.
  qualifying_turns        bigint NOT NULL DEFAULT 0 CHECK (qualifying_turns >= 0),
  last_interaction_at     timestamptz NULL,
  relationship_stage      text NOT NULL DEFAULT 'new'
    CHECK (relationship_stage IN ('new','acquainted','familiar','close')),
  stage_rule_version      text NOT NULL DEFAULT 'relationship-v1',
  latest_event_id         bigint NULL,
  -- CAS용. 일반 counter 갱신은 이것만 올린다.
  version                 bigint NOT NULL DEFAULT 0,
  -- stable prefix 교체 트리거. stage/rule이 바뀔 때만 올린다(매 턴 바뀌는 숫자는 제외).
  prompt_revision         bigint NOT NULL DEFAULT 0,
  updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.relationship_events (
  id            bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  user_id       uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  event_type    text NOT NULL,
  source_id     text NULL,
  activity_date date NOT NULL,
  occurred_at   timestamptz NOT NULL,
  delta         jsonb NULL,
  -- 결정적 중복 차단(같은 turn/day가 두 번 집계되지 않게).
  dedup_key     text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, dedup_key)
);

CREATE INDEX IF NOT EXISTS relationship_events_replay_idx
  ON public.relationship_events (user_id, activity_date, id);

-- ─────────────────────────────────────────────────────────────
-- 6. checkpoint v2 — cumulative window와 daily digest를 같은 source 계약으로(8.2절).
--    기존 테이블에 additive 컬럼만 추가한다.
-- ─────────────────────────────────────────────────────────────
ALTER TABLE public.conversation_checkpoints
  ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'window'
    CHECK (kind IN ('window','daily_digest')),
  ADD COLUMN IF NOT EXISTS segment_from_message_id bigint NULL,
  ADD COLUMN IF NOT EXISTS segment_through_message_id bigint NULL,
  ADD COLUMN IF NOT EXISTS coverage_from_message_id bigint NULL,
  ADD COLUMN IF NOT EXISTS coverage_through_message_id bigint NULL,
  ADD COLUMN IF NOT EXISTS previous_checkpoint_id uuid NULL,
  ADD COLUMN IF NOT EXISTS locale text NULL,
  ADD COLUMN IF NOT EXISTS source_started_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS source_ended_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS activity_date_from date NULL,
  ADD COLUMN IF NOT EXISTS activity_date_to date NULL,
  -- job이 먼저 끝나도 anchor를 즉시 줄이지 않는다(8.3절). hard threshold에서 CAS로 활성화.
  ADD COLUMN IF NOT EXISTS publish_state text NOT NULL DEFAULT 'published'
    CHECK (publish_state IN ('ready','published','superseded'));

-- 같은 source 범위의 window를 둘 이상 동시에 published하지 않는다.
CREATE UNIQUE INDEX IF NOT EXISTS conversation_checkpoints_published_window_uq
  ON public.conversation_checkpoints (user_id, coverage_through_message_id)
  WHERE kind = 'window' AND publish_state = 'published';
-- daily digest는 날짜 PK로만 조회한다(별도 semantic index를 만들지 않는다).
CREATE UNIQUE INDEX IF NOT EXISTS conversation_checkpoints_daily_uq
  ON public.conversation_checkpoints (user_id, activity_date_from)
  WHERE kind = 'daily_digest';

-- pending checkpoint/anchor 좌표(8.3절) — "생성 중"과 "published anchor"를 구분한다.
ALTER TABLE public.chat_contexts
  ADD COLUMN IF NOT EXISTS anchor_revision bigint NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS pending_anchor_message_id bigint NULL,
  ADD COLUMN IF NOT EXISTS pending_plan_revision bigint NULL,
  ADD COLUMN IF NOT EXISTS checkpoint_job_id uuid NULL,
  ADD COLUMN IF NOT EXISTS checkpoint_source_hash text NULL;

-- ─────────────────────────────────────────────────────────────
-- 7. legacy 비노출 tombstone (14.3절)
--    새 대화형 forget 기능이 아니다. 과거에 사용자가 잊어달라고 한 source를 v2가 되살리지
--    않게 하는 **전환 장벽**이다. 하나라도 매핑되지 않으면 그 사용자의 cutover를 막는다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.legacy_recall_tombstones (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  source_message_id   bigint NULL,
  diary_id            uuid NULL,
  source_operation_id uuid NULL,
  reason              text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  -- 메시지 또는 일기 중 정확히 하나를 가리킨다.
  CHECK ((source_message_id IS NULL) <> (diary_id IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS legacy_recall_tombstones_message_uq
  ON public.legacy_recall_tombstones (user_id, source_message_id)
  WHERE source_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS legacy_recall_tombstones_diary_uq
  ON public.legacy_recall_tombstones (user_id, diary_id) WHERE diary_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────
-- 8. provider 공유 backoff (13.2절)
--    같은 provider/model/lane의 429를 한 host가 겪으면 다른 host도 재시도 폭풍을 만들지 않는다.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.provider_backoffs (
  provider      text NOT NULL,
  model         text NOT NULL,
  lane          text NOT NULL,
  blocked_until timestamptz NOT NULL,
  reason        text NULL,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, model, lane)
);

-- ─────────────────────────────────────────────────────────────
-- 9. async_jobs routing 컬럼 (13.2절)
--    circuit-open 행을 일단 claim한 뒤 handler가 되돌리면 attempt와 lease가 오염된다.
--    claim SQL이 anti-join으로 거르도록 컬럼을 추가한다.
-- ─────────────────────────────────────────────────────────────
ALTER TABLE public.async_jobs
  ADD COLUMN IF NOT EXISTS provider text NULL,
  ADD COLUMN IF NOT EXISTS model text NULL,
  ADD COLUMN IF NOT EXISTS lane text NULL,
  ADD COLUMN IF NOT EXISTS eligible_at timestamptz NULL;

CREATE INDEX IF NOT EXISTS async_jobs_provider_claim_idx
  ON public.async_jobs (queue, priority, eligible_at, available_at, provider, model, lane)
  WHERE state = 'ready';

-- RLS deny-default — 서버(owner 롤)만 접근.
ALTER TABLE public.memory_pipeline_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mem0_ingest_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mem0_ingest_candidate_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mem0_memory_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mem0_memory_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_interaction_contract_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_relationship_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.relationship_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.legacy_recall_tombstones ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.provider_backoffs ENABLE ROW LEVEL SECURITY;

COMMIT;
