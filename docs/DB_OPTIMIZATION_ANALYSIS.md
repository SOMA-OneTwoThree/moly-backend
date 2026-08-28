# moly DB 최적화 분석 리포트

- **작성일**: 2026-08-28
- **대상**: Supabase `moly-db` (qkgjlgzsharnilxnkytd, ap-northeast-2, PostgreSQL 17.6)
- **성격**: 분석 전용 — 이 문서는 아무것도 변경하지 않았다. 모든 수정 제안은 별도 작업으로 진행한다.
- **데이터 출처** (3원 교차 검증):
  1. **라이브 DB 실측** — `pg_stat_statements`(2026-06-26 리셋, **63일치**), `pg_stat_user_tables/indexes`, Supabase performance/security advisor
  2. **코드 접근 패턴 분석** (에이전트) — moly-backend `app/`, `worker/` + moly-auth
  3. **스키마 설계 분석** (에이전트) — `db/schema.sql`(869줄), migrations 41개, cutover, ORM 모델 29개
- **♻︎ 2차 재검증 (08-28 저녁, 21:40 KST)**: 독립 에이전트 3개(스키마·워커·데이터접근) 재분석 + 라이브 DB 재실측. 주요 수치 전부 재확인됨(DB 903MB, HNSW 230MB idx_scan=0, 스크럽 4.4h, 폴링 2,100만회). 변경·추가된 내용은 ♻︎ 표기 — §5.6(신규), §6.1(정정), 로드맵 21~25(신규)

---

## 0. 요약 — 핵심 발견 Top 7

| # | 발견 | 실측 근거 | 심각도 |
|---|---|---|---|
| 1 | **벡터 테이블 하나가 DB의 54%** — `vecs.moly_memories_v2`가 485MB/903MB. 그중 **230MB HNSW 인덱스는 한 번도 사용된 적 없음**(idx_scan=0), TOAST 236MB도 churn으로 부풀어 있음(라이브 데이터는 ~84MB 추정) | §3.1 | 🔴 |
| 2 | **벡터 INSERT가 DB 시간 최대 소비처(누적 ≈5.1시간)** — HNSW 유지비용 때문에 1행 309ms, 12행 배치 최대 60초. 읽기는 그 인덱스를 안 쓰므로 **순수 낭비** | §2.2 | 🔴 |
| 3 | **retention 스크럽이 단일 쿼리 1위(누적 4.4시간)** — 10초마다 `async_jobs`(31MB)+`idempotency_keys`(22MB) 풀스캔 UPDATE. 부분 인덱스 없음, 행 삭제도 없음 | §2.1 | 🔴 |
| 4 | **행을 지우는 보존 정책이 0개** — `async_jobs` 87.9K행(99.99% 종료 상태), `job_attempts` 70.6K, `ai_usage_ledger` 68K, `idempotency_keys` 32.2K(98% 만료) 전부 무한 증가 | §3.2 | 🔴 |
| 5 | **빈 폴링이 트랜잭션의 대부분** — 63일간 BEGIN 6,100만·ROLLBACK 3,300만. 큐 6개×1초 폴링 + 10초 reaper = 컨슈머 프로세스당 하루 ~52만 트랜잭션이 거의 전부 no-op | §2.3 | 🟠 |
| 6 | **회원탈퇴 500 버그와 동일 패턴의 복합 FK SET NULL이 한 곳 더 남아 있음** — `user_interaction_contract_items`. 지금은 쓰기 경로가 없어 잠복 상태, 데이터가 들어가는 순간 탈퇴가 다시 깨짐 | §6.1 | 🔴 |
| 7 | **`db/schema.sql`이 실행 불가** — 522행에 diff 잔재 `+` 1글자. 신규 DB 부트스트랩(dev·CI·재해복구) 경로가 죽어 있고, 살려도 테이블 16개가 누락된 상태(마이그레이션에만 존재) | §6.4 | 🔴 |

**전체 그림**: 쿼리 설계 자체(앵커+하드캡 대화 윈도우, FOR UPDATE SKIP LOCKED 큐, 테넌트 복합 FK)는 잘 돼 있다. 낭비는 (a) **읽히지 않는 벡터 인덱스의 쓰기 비용**, (b) **지워지지 않는 운영 테이블**, (c) **인덱스 없는 주기 풀스캔**, (d) **캐시 0개로 인한 반복 조회** 네 곳에 집중돼 있다.

---

## 1. 라이브 DB 현황 실측

- **DB 총 크기**: 903MB / **캐시 히트율**: 99.90% (버퍼는 아직 여유)
- **통계 윈도우**: 2026-06-26 ~ 08-28 (63일)
- **유저 규모**: profiles 1,048 / 대화 유저 960 / messages 66,801행

### 크기 상위 테이블 (2026-08-28 실측)

| 테이블 | 총크기 | 힙 | 인덱스 | TOAST | 행수 | 특이점 |
|---|---|---|---|---|---|---|
| `vecs.moly_memories_v2` | **485 MB** | 12 MB | **233 MB** | **236 MB** | 14,048 | ins 38.8K / del 24.7K churn |
| `diary_recall_documents` | 105 MB | 10 MB | 37 MB | 57 MB | 7,157 | HNSW 25MB + trgm 11MB **둘 다 idx_scan=0** |
| `async_jobs` | 53 MB | 31 MB | 23 MB | – | 87,904 | seq_scan 337K(=스크럽), upd 243K |
| `messages` | 45 MB | 24 MB | 21 MB | – | 66,801 | **upd 74K 중 HOT 7건** — 인덱스 6개 전부 재작성 |
| `ai_usage_ledger` | 38 MB | 21 MB | 17 MB | – | 68,049 | LLM 호출당 1행, 삭제 없음 |
| `idempotency_keys` | 27 MB | 22 MB | 5.5 MB | – | 32,215 | seq_scan **339K**(=스크럽), 98% 만료 |
| `job_attempts` | 26 MB | 13 MB | 13 MB | – | 70,598 | 시도당 1행, 삭제 없음 |
| `mem0_ingest_candidates` | 21 MB | 11 MB | 10 MB | – | 19,086 | 본문 이중 저장(§4.3) |
| `mem0_memory_registry` | 19 MB | 10 MB | 8.9 MB | – | 19,086 | upd 142K (행당 7.4회) |

운영/관측 계열(`async_jobs`+`job_attempts`+`ai_usage_ledger`+`idempotency_keys`) 합계 **≈144MB, DB의 16%**가 "끝난 일의 기록"이다.

---

## 2. DB 시간을 어디에 쓰고 있나 (`pg_stat_statements` 63일)

### 2.1 🔴 1위: retention 스크럽 — 누적 4시간 24분

```
WITH scrub_jobs AS (UPDATE async_jobs SET payload='{}'... WHERE state IN (...) AND payload_redacted_at IS NULL AND payload_expires_at<=now())
   , scrub_idempotency AS (UPDATE idempotency_keys SET response=NULL ... WHERE response_expires_at<=now())
```
- **호출 339,546회 × 평균 46.7ms = 15,862초**. 호출당 6,682블록 접근.
- 발생 지점: `worker/consumer.py:341-344` reaper 루프(`job_reaper_interval_s=10.0`, `app/config.py:110`)가 **10초마다** `app/services/jobs.py:359-371` 실행.
- 원인: `payload_expires_at`·`response_expires_at`·`payload_redacted_at` 어디에도 인덱스가 없어 **두 테이블 풀스캔**. 실측으로 `async_jobs` seq_scan 337K, `idempotency_keys` seq_scan 339K가 스크럽 호출수와 정확히 일치한다.
- 구조적 문제: 스크럽은 컬럼만 비우고 **행은 영원히 남으므로** 스캔 대상이 계속 커진다. 테이블 성장에 비례해 워커 틱이 선형으로 느려지는 중.

### 2.2 🔴 2위: 벡터 upsert — 누적 ≈5.1시간, 건당 최대 60초

`INSERT INTO vecs.moly_memories_v2 ... ON CONFLICT (id) DO UPDATE` 계열 쿼리군 합산:

| 배치 크기 | 호출수 | 평균 소요 | 블록/호출 |
|---|---|---|---|
| 1행 | 13,326 | **310ms** | 1,981 |
| 2~5행 | ~5,300 | 0.7~2.5초 | 4K~10K |
| 6~9행 | ~900 | 2.8~17초 | 12K~20K |
| 10행+ | ~340 | 최대 **60초** | ~26K |

- 원인: 행마다 **HNSW 인덱스 삽입 + 1536차원 벡터(≈6KB) TOAST 쓰기**. HNSW는 삽입 시 그래프 탐색을 하므로 배치가 커질수록 초선형으로 느려진다.
- **그런데 그 HNSW 인덱스(`moly_memories_v2_hnsw_idx`, 230MB)는 읽기에서 한 번도 사용된 적이 없다**(idx_scan=0, advisor도 unused 판정). 회상 쿼리가 `WHERE metadata->'user_id' = ...` 필터를 먼저 걸기 때문에 플래너가 HNSW를 못 타고, 실측 10,721회 seq scan으로 전 행 거리 계산을 한다(호출 10,642회·평균 49.9ms짜리 `ORDER BY vec <=>` 쿼리와 일치).
- **결론: 이 인덱스는 읽기 이득 0, 쓰기 비용 5시간+, 저장 비용 230MB인 순수 부채다.** 14K 행 규모에서는 브루트포스 50ms가 이미 허용 범위이므로, (a) 인덱스 드랍이 즉효약이고, (b) 장기적으로는 쿼리를 HNSW-호환 형태(벡터 우선 `ORDER BY <=> LIMIT n` + 후필터, pgvector 0.8+ iterative scan)로 바꾸는 게 정석이다.
- `mem0_recall.py`의 회상은 `_PROVIDER_FETCH=40` top-k라 반환량 자체는 건전하다. 문제는 스캔 방식이지 적재량이 아니다.

### 2.3 🟠 3위: 빈 폴링 — 컨슈머 프로세스당 하루 ~52만 트랜잭션

| 쿼리 | 63일 호출수 | 초당 |
|---|---|---|
| BEGIN | 61,012,616 | 11.2 |
| ROLLBACK | 32,958,103 | 6.1 |
| COMMIT | 28,054,500 | 5.2 |
| claim CTE (`state='ready'`) | 20,786,501 | 3.8 |
| reaper 3단계 UPDATE ×3종 | 각 ~2,027,779 | 각 0.37 |
| profiles SELECT 2종 | 2,412,179 | 0.44 |
| `pgbouncer.get_auth` | 309,176 | — |

- `worker/consumer.py:303-328`: 큐 6개 × `job_idle_sleep_s=1.0` 폴링. 각 폴링이 세션 열기 + pre-ping + BEGIN + claim + COMMIT/ROLLBACK 왕복.
- 개별 쿼리는 0.05ms로 싸지만, **연결 왕복·트랜잭션 오버헤드·pgbouncer 부하·WAL 노이즈**가 상시 바닥 부하를 만든다. ROLLBACK이 COMMIT보다 많은 DB는 그 자체로 폴링 지배 신호다.
- 완화: 큐별 지수 백오프(빈 결과 1→5초) 또는 LISTEN/NOTIFY. enqueue 지점이 코드로 통제되므로 NOTIFY 도입이 쉽다.

### 2.4 기타 유의 쿼리

- `queue depth` 카운트(`SELECT count(*) FILTER ...`): 2,079회 × **106ms** — `async_jobs`가 커질수록 악화.
- `vecs.memories`(legacy) 대상 쿼리 1,462회 × 247ms — 현재 테이블은 삭제됐고 과거 통계. 라이브에는 `moly_memories_v2`만 존재함을 확인.
- `job_attempts` INSERT 72K × 7ms — 시도당 1행 기록 자체가 부하는 아니나 삭제가 없어 §3.2로 누적.

---

## 3. 스토리지 낭비

### 3.1 🔴 벡터 저장소 — 485MB 중 실효 데이터는 ~100MB 미만

- 14,048행 × (벡터 6KB + metadata 평균 347B) ≈ **84~90MB**가 이론적 라이브 데이터.
- 나머지 ≈400MB의 구성: HNSW 230MB(미사용) + TOAST 236MB(38.8K 삽입/24.7K 삭제 churn로 인한 사장 공간 — autovacuum이 TOAST 청크를 회수해도 파일 축소는 안 됨).
- 조치: HNSW 드랍 → `VACUUM FULL`(또는 `pg_repack`) 시 **400MB 안팎 회수** 예상. DB가 절반으로 준다.

### 3.2 🔴 무한 증가 + 삭제 없음 (실측 확인)

| 테이블 | 현재 | 증가율 | 실측 상태 |
|---|---|---|---|
| `async_jobs` | 87.9K행/53MB | 잡당 1행 | succeeded 69,189 + cancelled 18,695 + dead 19 = **99.99%가 종료 상태**, 최고령 8/6 |
| `job_attempts` | 70.6K행/26MB | 시도당 1행 | 삭제 0. `job_attempts_queue_idx`(4.7MB)는 63일간 **4회** 사용 |
| `ai_usage_ledger` | 68K행/38MB | **LLM 호출당 1행** (messages보다 빠름) | 삭제 0. `ai_usage_ledger_request_idx`(6.2MB) **0회** 사용 |
| `idempotency_keys` | 32.2K행/27MB | 채팅 요청당 1행 | **31,521행(98%)이 response 만료**, 10,184행이 dedupe 만료. `dedupe_expires_at` 기반 DELETE 코드가 **존재하지 않음** — 만료 개념만 있고 집행이 없다 |
| `relationship_events` | 16K행 | **턴당 1행** | `user_relationship_states`가 스냅샷을 이미 보유 — 리플레이 검증 기간 이후 절단 가능 |
| `revenuecat_events` | 25행 | 웹훅당 1행(payload 원문) | 아직 작음, 정책만 필요 |
| `messages` | 66.8K행/45MB | 턴당 2행 | 삭제 경로 자체가 없음 + `conversation_checkpoints.through_message_id`의 RESTRICT FK가 삭제를 구조적으로 금지(§6.2) |

권장 보존선: `async_jobs`/`job_attempts` 종료 후 7~14일, `ai_usage_ledger` 90일 후 일 단위 롤업, `idempotency_keys`는 `dedupe_expires_at` 집행, `relationship_events` 30일. 전부 절단용 인덱스가 이미 있거나 1개 추가로 충분하다.

### 3.3 🟠 미사용·중복 인덱스 (쓰기 증폭 + 공간)

**미사용 (advisor + idx_scan=0 실측 교차 확인):**

| 인덱스 | 크기 | 비고 |
|---|---|---|
| `moly_memories_v2_hnsw_idx` | **230 MB** | §2.2 — 드랍 1순위 |
| `diary_recall_documents_embedding_hnsw_idx` | 25 MB | 구조적으로 사용 불가 — `recall_diaries.py:24-61`이 거리를 SELECT 투영으로만 씀 |
| `diary_recall_documents_text_trgm_idx` | 11 MB | 동일 쿼리에서 ILIKE가 CTE 내부라 미사용 |
| `ai_usage_ledger_request_idx` | 6.2 MB | 사용 0 |
| `job_attempts_queue_idx` | 4.7 MB | 63일간 4회 |
| `payments_subscription_idx`, `payments_store_idx`, `privacy_ledger_user_idx`, `memory_pipeline_states_lag_idx`, `mem0_memory_registry_conflict_idx` | 소형 | 사용 0 |

**중복 (스키마 분석 + 실측):**

- `messages`: `messages_user_id_desc_idx`(4.2MB)는 `messages_user_id_id_uq`(5.3MB)와 완전 동치(B-tree 역방향 스캔). 동일 prefix (user_id, id) 인덱스가 3개. **messages는 UPDATE 74K 중 HOT이 7건**이라 매 갱신마다 6개 인덱스를 전부 재작성하는 테이블이므로 중복 제거 효과가 크다. [schema.sql:130 vs :577]
- `async_jobs`: `async_jobs_claim_idx`(16.9M scans)와 `async_jobs_provider_claim_idx`(1.6M scans)가 같은 부분조건(state='ready')·같은 선두 컬럼. 후자의 `provider/model/lane/eligible_at` 컬럼은 **읽는 코드가 0건**(미구현 설계 잔해). 하나로 합칠 것. [schema.sql:456 vs 20260805_memory_v2_tables.sql:370]
- `chat_response_references_reply_idx` — 784행 UNIQUE 제약과 컬럼·순서 완전 동일. [schema.sql:788]

### 3.4 🟡 본문 이중 저장

- 기억 본문이 `mem0_ingest_candidates.candidate_text`(Postgres)와 `vecs.moly_memories_v2.metadata->>'text'`(벡터측) **양쪽에** 저장된다(`app/services/mem0_pipeline.py:170-188`). 회상은 provider payload만 읽으므로 candidate_text는 재시도 resume 전용인데 GC가 없다.
- `user_interaction_contracts.document_json` ↔ `user_interaction_contract_items` 테이블도 같은 내용 이중 보관.
- products.assets(JSONB) ↔ iOS RoomTheme.swift 번들 폴백 이중화 — 한쪽만 바뀌면 조용히 어긋남.

---

## 4. 기억(메모리) 시스템 적재 방식 평가

### 잘된 점 (유지할 것)

- **대화 윈도우**: 앵커 + `LIMIT 120` 하드캡(`app/services/chat.py:254-257`) — 전량 적재 없음.
- **회상**: `needs_recall` 게이트로 인사·맞장구는 임베딩 호출 자체를 스킵, top-k 40 → 상대거리 컷 → 최종 8개(`mem0_recall.py:35-36, 229-239`).
- **임베딩 쓰기**: 턴당이 아니라 청크당(≤20턴당 ≤24벡터) 배치.
- **회상 병렬화**: Phase 1 DB 작업과 `asyncio.ensure_future`로 오버랩 — 유저 락을 늘리지 않음.
- **컨슈머 규율**: LLM/외부 호출 전 세션 close가 전 워커에서 일관됨(유일 예외: tick §5.3).

### 문제점

1. **HNSW를 읽기가 못 쓰는 쿼리 형태** (§2.2) — 회상·recall_diaries 둘 다.
2. **checkpoint(요약)가 꺼져 있음**: `context_checkpoint_enabled=False`(`app/config.py:65`). 앵커 앞 이력이 요약 없이 프롬프트에서 사라지고 디스크엔 영구 잔존 — "요약해서 줄인다"는 효과가 현재 0.
3. **죽은 기계장치**: `chat_contexts.memory_generation`은 읽기만 있고 쓰는 코드가 없음(조인이 영구 no-op). 시간 해석 컬럼군(`event_started_at` 등 6종)도 읽기만 존재.
4. **캐스트 조인이 인덱스 무력화**: `reconsolidate_jobs.py:41-57`의 `ON v.id = r.provider_memory_id::text` — 활성 유저당 하루 1회 벡터 테이블 전체 해시 조인.
5. **memory_sweep 자기강화 루프**: LIMIT 50이 출력만 제한하고 작업량은 제한 못 하는 스캔 5종 + 최대 300문장 `_apply` 트랜잭션이 5초 타임아웃과 충돌 → 실패 → reaper 재큐 → 같은 무거운 스캔 반복(`worker/memory_sweep_jobs.py:65-305`).
6. **mem0 삭제 직렬 외부 호출**: 최대 50건 × 6초를 60초 잡 타임아웃 안에서 — 구조적으로 타임아웃→재큐 루프(`worker/mem0_jobs.py:706-720`). `delete`는 리스트를 받으므로 1회 호출로 접힌다.

---

## 5. 애플리케이션 쿼리 패턴 낭비

### 5.1 핫 패스: POST /chat/messages

- Phase 1(유저 락 구간)에서만 **SELECT 22~26회**. 개별 read 엔드포인트는 3~6회로 건전 — 부하는 챗에 집중.
- `app_config`를 요청당 2회 조회(`chat.py:912` + `gating.py:47`) — 키 목록 병합으로 1회 가능.
- `chat_contexts` 2회, routines+completions도 build_context와 get_routines 툴에서 각각 중복 조회.
- LLM 호출 1건당 `ai_usage_ledger` INSERT+UPDATE에 **전용 커넥션 2개·커밋 2회**, 그리고 매번 `ai_price_catalog`(연 몇 회 변경) SELECT — 캐시 없음(`usage_ledger.py:381-447`).

### 5.2 무제한/오배치 쿼리

- **`recall_diaries.py:24-61` (최악)**: LIMIT 없는 CTE가 유저의 published 일기 전량(본문 포함)을 실체화 → 행마다 벡터 거리 + ILIKE 2회 + 윈도우 2개 → 마지막에 LIMIT 5. 1년 유저 = 툴 호출마다 365회 거리 계산.
- `routine.py:167`: 루틴 전체 체크 이력을 읽고 Python에서 30일로 절단.
- `diary.py:225,307`: 목록 API가 본문 전체를 가져와 60자 프리뷰만 사용.
- `api/health.py:149-150`: user_id 없는 count가 부분 인덱스 조건(`'active','ambiguous'`)과 어긋나(쿼리는 `'pending'`) 헬스 폴링마다 registry seq scan.
- N+1: `shop.py:451`(슬롯 5회 SELECT→IN 1회), `chat_references.py:176`(참조당 flush), `diary_recall_repo.py:105`(메시지 id당 INSERT).

### 5.3 워커 tick (15분 주기)

- `profiles` **전량 순회**(시간대 필터 없음) 후 대부분 no-op(`worker/tick.py:342-357`).
- 최상위 N+1: 페이징으로 이미 읽은 유저를 **유저마다 새 세션 + `session.get(Profile)`**로 재조회 — timezone 문자열 하나 때문(`tick.py:396-398, 124-133`).
- **tick만 LLM/푸시 호출을 DB 세션을 쥔 채 관통** — 유저당 최대 120초 커넥션 점유(`tick.py:124-194`).
- 일기 시각의 4틱 모두 `diary_gen_claims` upsert+DELETE(실작업은 1틱만).

### 5.4 커넥션·풀 설정

- `create_async_engine`에 **pool_size/max_overflow/timeout 전부 미지정** → 기본 5+10=프로세스당 15. EC2 2대×(API+consumer)+워커 = 이론상 ~85 커넥션, 캡 없음(`app/core/db.py:41-51`, INFRA.md TODO #18).
- **DSN이 `pooler.supabase.com:5432`(session 모드)인데 코드는 transaction 풀링(6543) 전제**로 `statement_cache_size=0`을 걸고 문서도 6543이라 기술 — **prod 값 확인이 최우선**. session 모드면 15커넥션이 각각 실제 백엔드를 점유한다.
- vecs용 sync 풀(3)이 챗·워커 프로세스마다 별도 생성.
- 에이전트 툴 동시성 상한(64)이 DB 풀(15)과 어디서도 대조되지 않음.
- advisor: Auth 서버 커넥션이 절대값 10으로 고정 — 인스턴스 증설 시 percentage 방식 전환 필요.

### 5.5 캐싱 — 전무

`lru_cache`는 `get_settings()` 1건뿐. `app_config`(9행), `products`(20행), `ai_price_catalog`(4행) 같은 정적 소테이블이 요청/호출마다 DB를 친다. "프로세스 캐시를 두지 않는다"는 명시적 설계(`agent/config.py:6-8`)지만, 그렇다면 최소한 요청 내 중복 제거(2회→1회)와 짧은 TTL(30초)은 안전하다.

### 5.6 ♻︎ 2차 분석 보강 — 라운드트립·중복 저장 추가 발견

**턴당 총 왕복 ~50회** (Phase 1 28~30 + Phase 2 ~20). §5.1의 22~26회는 SAVEPOINT 왕복을 빼고 센 값이다.

- **`turn_context.build_context` 하나가 11왕복**: 항목별 fail-open용 `begin_nested()` 3개 = SAVEPOINT/RELEASE 6왕복 + 쿼리 5개(`app/services/turn_context.py:47-180`). 킬스위치(`current_turn_context_enabled`)로 통째로 꺼지는 부가 기능이므로, CTE 1~2쿼리 + Python try/except로 압축 가능.
- **`statement_cache_size=0`**(pgbouncer 호환, `app/core/db.py:48-49`) 때문에 모든 쿼리가 매번 재파싱 — 왕복 수 감소가 곧 파싱 비용 감소로 직결. `pool_pre_ping=True`는 짧은 세션마다 `SELECT 1`을 추가해 실왕복 ~1.5배.
- **mem0 ingest 잡 내부 N+1**: `_STAGE_CANDIDATE`/`_CANDIDATE_SOURCE`/`_REGISTER_PENDING`/`_MEMORY_SOURCE`를 후보×근거 루프로 실행 — 잡당 최대 **~170왕복**(`worker/mem0_jobs.py:365-411`). 전부 정적 SQL + `ON CONFLICT DO NOTHING`이라 executemany/`unnest()` 배열 파라미터로 ~5회로 압축 가능.
- **`memory_sweep` 재개 enqueue도 row-by-row**(`worker/memory_sweep_jobs.py:238-273`) — dedup 키 유지한 bulk INSERT로 전환 가능.
- **`privacy_cleanup`/provider 삭제 비효율**: ① 유저 벡터 열거를 **영벡터 유사도 검색**으로 수행(`mem0_adapter.py:242-248`) — 메타데이터 필터만 필요한 작업에 최악의 접근. `WHERE metadata->>'user_id'=:uid` 직접 SQL로 대체(단 bounded 계약 유지: `WHERE id IN (SELECT … LIMIT :n)`). ② provider 삭제·`_MARK_DELETED` 건별 실행은 `mem0_jobs.py:708-720`에만 존재(⚠️ 검증 정정: `privacy_jobs.py:71-96`에는 건별 DELETE가 없음 — 초판의 사실 오류).
- **`chat_contexts` 같은 행을 한 턴에 2~3회 UPDATE**(acquire→`_touch_last_active`→`_PUBLISH`) — 턴당 dead tuple 2~3개(§1의 upd 36.5K와 일치). `_touch_last_active`(`chat.py:310-317`)를 `_PUBLISH`(`chat_turns.py:82-89`) SET 절에 병합하면 무비용 제거.
- **`relationship_profile_renders`는 저장할 이유가 없는 파생 데이터**: `(stage, active_days, language)`의 순수 함수 출력(3줄 텍스트)을 revision마다 행으로 적재하고 매 턴 `ORDER BY … LIMIT 1` 조회(`relationship_projector.py:92-110`, `chat.py:868`). 챗 프로세스에서 렌더하면 테이블+매턴 쿼리가 사라짐.
- **`/health/queues`의 `_STATS_SQL`이 시간 범위 없는 `WITH RECURSIVE`로 async_jobs 전체 스캔**(`app/services/jobs.py:700-714`) — §2.4 큐뎁스 106ms의 정체. `created_at > now()-'7 days'` 범위 추가 또는 헬스 대상을 `/health/ready`로 한정.
- **`job_telemetry` + heartbeat = 잡당 +4~6왕복**: record_start/outcome 각각 새 세션+commit(`worker/consumer.py:235,279`), heartbeat 30초마다 1회. claim/finalize 트랜잭션에 병합 여지.
- **diary_recall 임베딩이 일기 발행/수정마다 재생성**(`app/services/diary_recall_repo.py:144`) — `source_hash`·모델 불변이면 스킵 가능.
- **LATERAL 상관 서브쿼리 3곳**(`mem0_recall.py:160-163`, `mem0_registry_repo.py:25-28,44-47`) — 현재 N이 작아(12~40) 무해하나 `mem0_memory_sources(registry_id, source_occurred_at DESC)` 인덱스 유무를 확인할 것(회상은 1.5초 예산의 유저 대면 경로).

---

## 6. 스키마 리스크 (변경 전 반드시 알아야 할 것)

### 6.1 ♻︎ 회원탈퇴 500 — 라이브는 핫픽스 완료, 마이그레이션 파일은 여전히 위험

**라이브 DB 재실측(08-28 저녁, pg_constraint 직접 확인)**: `user_interaction_contract_items`의 복합 FK는 이미 **`ON DELETE SET NULL (source_message_id)`**(PG15+ 컬럼 지정형)로 핫픽스 적용돼 있다 — user_id는 건드리지 않으므로 탈퇴 시 더는 터지지 않는다.

**남은 문제**: `20260805_memory_v2_tables.sql:237`은 여전히 구형 `ON DELETE SET NULL`(전 컬럼 NULL 시도) 그대로다. **신규 환경 부트스트랩/재해복구 시 버그가 그대로 재도입**된다. 수정: 파일을 라이브와 동일한 `SET NULL (source_message_id)` 형태로 갱신. 또한 **dev DB(wywzjslvxwttxkecbyis)에는 FK 수정·`delete_user_memories` RPC가 미적용** 상태다(8/28 후속 미해결 항목).

### 6.2 🔴 같은 컬럼에 RESTRICT+CASCADE 이중 FK

`conversation_checkpoints.through_message_id` — [schema.sql:477] RESTRICT와 [schema.sql:849-850] CASCADE가 동시 존재. RESTRICT가 항상 이기므로 **checkpoint 행이 있는 유저는 메시지 삭제(탈퇴 CASCADE 포함)가 막힌다**. 게다가 단일 컬럼 쪽 FK는 인덱스도 없어 검사마다 풀스캔. 둘 중 하나(실용적으론 RESTRICT 쪽) 제거 필요. 유사 사례: `user_items`의 CASCADE+NO ACTION 상충 [schema.sql:260 vs 268-269].

### 6.3 🔴 인덱스 없는 FK — 탈퇴/삭제 시 행마다 풀스캔

Supabase advisor 실측 16건 = 스키마 분석 9건과 일치. 탈퇴는 auth.users 삭제 한 방의 전체 CASCADE인데, messages 수만 행이 지워질 때 아래 테이블들을 **행마다 풀스캔**한다 — 탈퇴 지연/타임아웃의 직접 원인:

`greetings.committed_message_id`, `diaries.preset_ment_id`, `subscription_hay_grants` 2건, `chat_response_references(user_id,diary_id)`(RESTRICT라 더 아픔), `diary_claim_sources(user_id,message_id)`, `mem0_memory_sources`·`mem0_ingest_candidate_sources`의 message FK, `user_interaction_contract_items` 2건, `routine_completions`, `user_items`, `conversation_checkpoints.through_message_id`, `async_jobs.user_id`.

또한 `mem0_memory_sources`·`mem0_ingest_candidate_sources`는 **PK가 없다**(advisor).

### 6.4 🔴 schema.sql 실행 불가 + 드리프트

- [schema.sql:522] 선두 `+`(diff 잔재) 1글자 → `psql -f`도 `db/apply.py`도 즉사. **신규 환경 부트스트랩 경로가 죽어 있음.**
- 고쳐도 **테이블 16개가 schema.sql에 없다**(ai_usage_ledger, job_attempts, mem0_* 6종, relationship_* 3종 등 — 전부 마이그레이션에만 존재). 적용 순서는 README의 산문 목록에만 있고 prod엔 schema_migrations 테이블도 없음.
- 권장: prod에서 `pg_dump --schema-only`로 schema.sql 재생성 + 순번 있는 마이그레이션 원장 도입.

### 6.5 🟠 prod 전환 함정 (마이그레이션 순서)

- `20260804_memory_cutover_guard.sql`의 chat_contexts 트리거가 **존재하지 않는 컬럼**(memory_text)을 참조 — prod에 순서대로 적용하면 트리거 DROP되는 11번째 파일 전까지 **모든 대화 쓰기가 실패**한다. prod에선 이 파일을 건너뛸 것.
- `cutover/promote_memory_v2.sql`을 apply.py로 돌리면 BEGIN/COMMIT이 제거돼 "확인 후 커밋" 가드가 무시됨.
- `bootstrap_user()`가 6개 파일에서 재정의 — 살아있는 버전이 적용 순서에만 의존. 이 함수는 카탈로그 상태가 나쁘면 **회원가입 자체를 실패**시키는 단일 장애점이기도 하다.

### 6.6 🟡 기타

- LLM/유저 입력 TEXT 상한 CHECK 부재(messages.content, candidate_text 등 8곳) — 정책이 feedback에만 적용돼 비일관.
- 집계 키가 자유 문자열: `payments.store`, `ai_usage_ledger.purpose`, `async_jobs.queue/job_type` — CHECK 없음(오타 큐 = 영원히 안 소비되는 잡).
- `mem0_memory_registry` 자기참조 3종(duplicate/superseded/conflict) FK 없음 — dangling 참조를 DB가 못 막음.
- `hay_transactions`: 쿼리는 `(user_id, id)` 키셋 페이징인데 인덱스는 `(user_id, created_at)` — 어긋남.
- `routine_completions_user_idx(user_id)` — 호출부 5곳 전부 `+activity_date` 필터. 복합 인덱스 1개로 해결.
- **messages 파티셔닝은 사실상 불가** — 8개 테이블이 (user_id, id) 복합 FK로 참조 중이라 파티션 키(activity_date)와 양립 불가. 장기적으로는 content 분리(얇은 messages 유지) 방향.
- 죽은 자산: `user_schedules`(dispatcher off), `shadow_prompt_traces`·`provider_backoffs`(참조 0건), `profiles.next_diary_due_at`, `memory_extract.py`(존재하지 않는 모듈 import).

---

## 7. 보안·개인정보 (advisor 실측 + 코드 교차)

- **RLS enabled + 정책 0개가 public 45개 테이블 전부** — service_role 전용 접근이라 기능상 문제는 없으나, anon/authenticated로 PostgREST가 열리는 순간의 방어선이 "정책 없음=전부 거부" 하나뿐이다. 의도 문서화 또는 명시적 deny 정책 권장.
- **SECURITY DEFINER 함수 3개가 anon 실행 가능**: `create_privacy_barrier_for_profile()`, `handle_new_user()`, `normalize_profile_language()` — `/rest/v1/rpc/`로 노출. EXECUTE 회수 필요.
- **탈퇴 시 벡터 정리 경로 불일치**: moly-auth `service.ts:22`가 legacy `"memories"` 테이블을 지우는데 그 테이블은 **존재하지 않음**(라이브 확인) — 이 삭제는 항상 no-op이고 실패는 console.warn으로 삼켜진다. 실제 정리는 백엔드 `worker/privacy_jobs.py`의 v2 경로가 담당. **라이브 실측 결과 고아 벡터 0건 / 고아 candidate 0건** — 현재까지 실피해는 없으나, moly-auth 단독 경로로 탈퇴가 처리되는 케이스가 생기면 평문·벡터가 남는다. service.ts의 컬렉션명 갱신 + 백엔드 privacy job 호출 보장이 필요.
- privacy `_REDACT`(`app/services/privacy.py:31-49`)는 idempotency_keys·chat_response_references·async_jobs만 다루고 mem0_* 평문(`candidate_text`)은 profiles CASCADE에만 의존. 준비된 `scrubbed_at` 컬럼은 쓰는 코드 0건.
- `pg_trgm`·`vector` 확장이 public 스키마에 설치(advisor WARN), 함수 2개 search_path 미고정, Auth leaked-password protection off.

---

## 8. 최적화 로드맵

### 즉시 (1~2줄 수정, 리스크 낮음, 효과 큼)

| # | 작업 | 예상 효과 |
|---|---|---|
| 1 | `moly_memories_v2_hnsw_idx` DROP (14K행은 브루트포스 50ms로 충분) | 벡터 upsert 310ms~60s → ms대, DB 시간 ~5시간/63일 제거, 230MB 회수 |
| 2 | 스크럽용 부분 인덱스 2개 생성 (`async_jobs(payload_expires_at) WHERE payload_redacted_at IS NULL AND payload_expires_at IS NOT NULL`, `idempotency_keys(response_expires_at) WHERE response IS NOT NULL`) | 단일 쿼리 1위(4.4시간/63일) 소멸 |
| 3 | ♻︎ `20260805_memory_v2_tables.sql:237`을 라이브와 동일한 `SET NULL (source_message_id)`로 갱신 (라이브는 핫픽스 완료 확인) | 신규 환경에서 탈퇴 500 재도입 차단 |
| 4 | `conversation_checkpoints` RESTRICT FK 제거(CASCADE만 유지) | 탈퇴/메시지 삭제 차단 해제 |
| 5 | `db/schema.sql:522`의 `+` 제거 | 부트스트랩 복구 |
| 6 | ♻︎ (§9.3-6 정정 반영) `20260804_memory_cutover_guard.sql` **파일 내 `IF EXISTS(chat_contexts.memory_text)` 하드 가드** 삽입 — README 산문·apply.py 스킵 아님. 편집 시 원장 checksum 동기화(§9.6 D1) | 대화 전멸 사고 예방 |

### 단기 (반나절~하루)

| # | 작업 | 예상 효과 |
|---|---|---|
| 7 | ⚠️ 행 삭제 retention — **§9+§9.5 검증 반영 최종판**: async_jobs는 succeeded/cancelled만 + **`replay_of IS NULL AND NOT EXISTS(replay 자식)` = replay 사슬은 통째로 영구 보존**(♻︎♻︎ leaf-first 초안은 /health의 dead 해소 판정을 끊어 배포 게이트 오탐 — 3개 렌즈 교차 확증, §9.5 F1), **dead 보존**, job_attempts는 CASCADE 자동, idempotency_keys는 `dedupe_expires_at IS NOT NULL AND <= now()` 술어로만+배치 분할(30일 지난 키 재사용이 409→신규 처리로 바뀌는 계약 변화 승인 필요), ai_usage_ledger 90일 롤업(`status='started'` 행은 연령 무관 삭제 금지). **relationship_events 절단 금지** 유지 | 무한 증가 중단 (♻︎ 파일 축소는 별도 repack 필요 — "140MB 회수"는 과대 표현이었음) |
| 8 | 컨슈머 폴링 백오프(빈 큐 1→5초 지수) 또는 LISTEN/NOTIFY | 트랜잭션 ~85% 감소 (하루 52만→수만) |
| 9 | FK 인덱스 추가(§6.3의 16건 중 messages/탈퇴 경로 우선 6건) + `routine_completions(user_id,activity_date)` + `hay_transactions(user_id,id)` + registry `pending` 커버 | 탈퇴 시간 대폭 단축, 루틴/지갑 쿼리 인덱스화 |
| 10 | 중복 인덱스 제거: `messages_user_id_desc_idx`, ♻︎ **`async_jobs_provider_claim_idx` 쪽을 제거**(claim_idx 유지 — 초판의 통합 방향은 반대였음, §9.1), `chat_response_references_reply_idx`, `diary_recall_documents` HNSW+trgm | messages/async_jobs 쓰기 증폭 완화, ~45MB 회수 |
| 11 | `ai_price_catalog` 메모이제이션(TTL 5분) + `app_config` 요청당 2회→1회 | LLM 호출당 커넥션 2→1, 챗 요청당 SELECT -2 |
| 12 | DSN 포트(5432 vs 6543) 확인 + `pool_size`/`max_overflow` 명시 + 프로세스 합산 상한 산정 | 커넥션 고갈 리스크 제거 |
| 13 | `vecs.moly_memories_v2` `pg_repack`(또는 새벽 VACUUM FULL) | TOAST ~150MB 추가 회수 |

### 중기 (설계 변경 동반)

| # | 작업 | 근거 |
|---|---|---|
| 14 | ♻︎ **현 제안대로 실행 금지** — 벡터 우선 LIMIT+후필터는 유저당 평균 ~15벡터 환경에서 회상이 사실상 0건으로 붕괴(전역 top-40 중 해당 유저 기대값 0.04개, §9.1). ♻︎♻︎♻︎ 성장 시 처방은 GIN이 아니라 **`((metadata->'user_id'))` jsonb 표현식 B-tree**(vecs `$eq`는 `@>`가 아닌 `->` 동등을 생성 — GIN·기존 `->>` 인덱스 모두 못 탐, §9.6). 트리거 조건은 로드맵 Phase 6 | §2.2, §9.1, §9.6 |
| 15 | `recall_diaries` CTE 재작성(후보 선절단), `memory_sweep` 스캔 5종+5초 타임아웃 충돌 해소, mem0 삭제 배치화 | §4, §5.2 |
| 16 | tick 재설계: timezone을 페이징 쿼리에 포함, 유저당 세션 제거, LLM/푸시 호출 전 세션 close | §5.3 |
| 17 | checkpoint 활성화 여부 결정 — 켜지 않을 거면 관련 죽은 기계장치(memory_generation 등) 제거 | §4.2 |
| 18 | schema.sql 재생성(pg_dump) + 순번 있는 마이그레이션 원장 + cutover 잔재 정리 | §6.4~6.5 |
| 19 | messages 장기 전략: content 분리 후 파티셔닝 또는 아카이빙(현 구조로는 파티셔닝 불가를 팀이 인지할 것) | §6.6 |
| 20 | SECURITY DEFINER 3함수 EXECUTE 회수, moly-auth 탈퇴 경로의 v2 벡터 정리 보장 | §7 |

### ♻︎ 단기 추가 (2차 분석에서 확정된 저비용·고효과 항목)

| # | 작업 | 예상 효과 |
|---|---|---|
| 21 | mem0 ingest/sweep/privacy 루프 → 배치화. ♻︎ 조건: `unnest()`로 **부모 INSERT 전량 → 자식 INSERT 전량** 2문 유지, `INSERT…RETURNING` CTE로 접기 **금지**(충돌 행이 RETURNING에서 빠져 resume 시 근거 소실 — `mem0_jobs.py:124-127`의 과거 사고 재발) | ingest 잡당 왕복 170→5 |
| 22 | ♻︎ `turn_context` SAVEPOINT **3쌍→1쌍**(전체를 하나로 감쌈) + CTE 병합. 전부 제거는 금지 — SAVEPOINT는 fail-open이 아니라 **트랜잭션 오염 차단**(`turn_context.py:60-62`), 없애면 항목 1건 오류가 챗 500으로 확대. **선행: prod의 `CURRENT_TURN_CONTEXT_ENABLED` env 확인**(기본 False — 꺼져 있으면 이 항목 이득 0) | 턴당 왕복 8→2 (3번째 SAVEPOINT는 플래그 off라 현재 미실행 — 실측 11이 아닌 8) |
| 23 | `_touch_last_active`를 `_PUBLISH`에 병합(♻︎ `now()` 아닌 `:now` 파라미터 필수 — 저녁 푸시 활동일 계산 보존) + ♻︎ usage_ledger는 **`close_call`만** 배치 flush. `open_call` 선커밋은 유지 — 불변식 2("유실 호출을 0원으로 숨기지 않는다", `usage_ledger.py:9-11`)와 cutover 게이트가 이 행 존재에 의존 | 턴당 왕복 -1, LLM 호출당 왕복 3→2 |
| 24 | tick 타임존 필터 선행 분리. ♻︎ 조건: **문자열 동등 `WHERE timezone=ANY(:tzs)`만**(SQL `AT TIME ZONE` 금지 — 이상 tz 1행이 틱 전체를 죽여 그날 일기·푸시 전멸), `:tzs`는 **hour 단위**로 산출(15분×4틱 재시도 구조 보존), 이상 tz 관측 로그 대체 유지 | 15분마다 전 유저 N명 순회 → 해당 시각대 유저만 |
| 25 | ♻︎♻︎ 재수정(§9.5 F2 — 1차 수정판도 오진이었음): 106ms 쿼리의 진범은 `_STATS_SQL`이 아니라 **`scripts/run_memory_consumer.py:44-52`의 `_pending`**(WHERE에 state 조건이 없어 seq scan; `_STATS_SQL`은 이미 라이브 `state_queue_idx`+`replay_of` 부분 인덱스로 커버). 수정: `_pending`에 `AND state IN ('ready','running','dead')` 한 줄 추가(FILTER가 그 3개만 세므로 의미 100% 동일) — **신규 인덱스·시간 범위 둘 다 불필요**. diary_recall 임베딩 스킵은 이미 구현돼 있어 삭제(`diary_recall_repo.py:55-59`). `relationship_profile_renders` 제거는 읽는 곳 3곳(`chat.py:868`, `verify_cutover_gate.py:130`, `dev.py:213`) 동시 정리 조건부 | 큐뎁스 106ms 제거 (모니터링 의미 보존) |

---

## 9. ♻︎ 안전성 교차검증 결과 (2026-08-28 밤, Opus 크리틱 에이전트 2개 · READ-ONLY 코드 대조)

검증 기준: **"에러가 안 나는가"가 아니라 "기억·대화·일기·재화·푸시의 동작 의미가 보존되는가"**. 문서 주장을 믿지 않고 전 항목을 코드로 재확인했다. 위 로드맵 표의 ♻︎ 표기는 이 검증 결과가 이미 반영된 것이다.

### 9.1 ❌ 원안대로 실행하면 의미가 깨지는 항목 (수정판으로만 진행)

| 항목 | 무엇이 깨지나 | 근거 | 수정판 |
|---|---|---|---|
| #7 `relationship_events` 30일 절단 | **관계 단계 영구 동결.** `user_relationship_states`는 누적기가 아니라 매번 전량 재계산 후 `GREATEST()` — 절단하면 재계산값이 저장값을 영원히 못 넘어 active_days/stage가 고정되고, 프롬프트에 실리는 관계 문구가 달라짐. 에러 없이 침묵 | `relationship_projector.py:34-76`, `relationship.py:52-57` | 증분 누적기 리팩터 전까지 **절단 금지** (16K행 = 용량 기여 미미) |
| #7 async_jobs 단순 DELETE | ① `replay_of` FK가 NO ACTION이라 replay 자식이 남아 있으면 **DELETE 전체 롤백**. ② dead 삭제는 배포 게이트(`dead_total 증가=배포 실패` 운영 규칙) 무력화 | `schema.sql:432`, `health.py:163-164`, `verify_cutover_gate.py:77-79` | succeeded/cancelled만, leaf-first(`NOT EXISTS` 자식), **dead 보존**(19건뿐) |
| #10 claim 인덱스 통합 방향 | `provider_claim_idx`는 2번째 키 `eligible_at`이 전 코드에서 미사용(항상 NULL)이라 claim의 `ORDER BY priority, available_at, created_at`을 못 태움 — 살릴 쪽은 `claim_idx` | `jobs.py:240-254`, `schema.sql:456` vs `20260805_memory_v2_tables.sql:370` | **provider판을 삭제**, claim_idx 유지 |
| #14 회상 ANN 전환 | 유저당 평균 ~15벡터인데 전역 top-40 선절단 → 해당 유저 기억 기대값 0.04개 = **회상 붕괴**. 상대 컷(`out[0].distance` 기준)도 통째로 이동 | `mem0_recall.py:35,234`, 실측 14K행/960유저 | 지금 실행 금지. #1(HNSW 드랍)만으로 충분 |
| #15 recall_diaries 벡터 선절단 | 주 사용 경로("일기 보여줘")는 query 없음=벡터 자체가 없음. `count(*) OVER()` 창 집계가 잘려 `no_content_match` 오판→본문 미반환. 임베딩 NULL 일기 실종 | `agent/tools/recall_diaries.py:85,94-96`, `recall_diaries.py:54-56,27-28` | 절단 기준은 벡터가 아니라 `display_date DESC` 상한 + 별도 COUNT |
| #15 mem0 delete 일괄화(단순형) | 1건 실패 시 배치 전원 `failed` — **`failed`→`pending` 복구 코드가 0곳**이라 벡터 영구 고아화 | `mem0_jobs.py:673-686`, CHECK `20260805_memory_v2_tables.sql:130-131` | 성공 시에만 전원 `deleted`, 실패 시 마킹 없이 `JobRetry`(pending 유지) |
| #22 SAVEPOINT 전체 제거 | SAVEPOINT는 **트랜잭션 오염 차단**용 — 제거 시 내부 SELECT 1건 오류가 세션을 aborted로 만들어 챗 요청 전체 500 | `turn_context.py:60-62`, `chat.py:935→949` | 3쌍→**1쌍**으로 축소 (왕복 6→2) |
| #23 usage_ledger 전면 배치 flush | 불변식 2("유실 호출을 0원으로 숨기지 않는다") 파괴 + cutover 게이트(`status='started'` 카운트)가 영구 0=측정불능인데 통과로 보임 | `usage_ledger.py:9-11,381-405`, `verify_cutover_gate.py:82-84` | `open_call` 선커밋 유지, **close_call만** 배치 |
| #25 /health 시간 범위 | 7일 넘은 **미해결 dead**(가장 나쁜 dead)가 카운트에서 실종 — 코드 주석이 명시적으로 금지한 은폐 재발 | `jobs.py:689-699` | 시간 범위 대신 state 부분 인덱스 |

### 9.2 ⚠️ 조건부 항목의 핵심 전제

- **#1 HNSW 드랍**: 회상 결과 **100% 동일 증명됨** — vecs `$eq` 필터가 생성하는 쿼리는 ♻︎♻︎♻︎ `metadata->'user_id' = …::jsonb`(단일 화살표 jsonb 동등, `vecs/collection.py:934` — 초판의 `@>` 서술은 오류)이고, 기존 B-tree는 `->>`(text) 표현식(`moly_memories_v2_user_idx` — privacy/verify 스크립트 전용)이라 이 술어를 못 타 지금도 seq scan(08-28 plain EXPLAIN 양형 대조로 재확증, §9.6). vecs 클라이언트는 인덱스 부재 시 warn만 하고 진행(이미 인덱스를 None으로 인식 중). **단 `DROP INDEX CONCURRENTLY` 필수** — 일반 DROP은 60초 upsert 뒤에 줄 서서 그동안 회상이 3초 예산 초과→빈 목록 fail-open(대화가 기억 없이 나감).
- **#2/#9/#10/#13 공통 차단 요소**: `db/apply.py:22-39`가 전체를 트랜잭션으로 감싸 **CONCURRENTLY·VACUUM FULL 실행 불가**. psql 직결 런북 + `schema_migrations` 수동 기록 절차가 선행돼야 함.
- **#4**: prod에 복합 CASCADE FK(`schema.sql:849`)가 실제 존재하는지 `pg_constraint` 확인 후 RESTRICT 제거 (라이브 확인 완료: 존재함 — 08-28 실측 §6.2 참조).
- **#8**: `interactive_async`·`critical`·`notification` 큐는 producer 0건 — 백오프 지연이 유저에게 보이지 않음. ingest 체인(180초 지연 설계)도 +5초는 허용 범위. 단 **사슬 한 마디마다 백오프가 붙어** 백필 시 누적됨 → finalize 후속 enqueue 시 큐 루프 웨이크업 권장. **LISTEN/NOTIFY는 pgbouncer transaction 풀링(6543)과 양립 불가** — #12(포트 확정) 후 결정, 사실상 백오프만 채택 권장.
- **#11**: `ai_usage_ledger`를 읽는 앱 코드 0건(게이팅·알림 모두 `user_daily_stats` 사용) — 가격 캐시 5분 지연은 내부 회계에만 영향. app_config 병합 시 **읽기 시점을 유저 락 이전으로 앞당기지 말 것**(tokens_used TOCTOU 방어).
- **#17**: "checkpoint 켜기"(프롬프트 의미 변화, A/B 필요)와 "죽은 기계장치 제거"(안전)를 **분리 승인**할 것. 단 `memory_generation` 컬럼은 checkpoint·diary_recall 쿼리가 읽으므로 컬럼 삭제는 별도 작업.
- **#20**: 트리거 함수 EXECUTE는 CREATE TRIGGER 시점에만 검사 — REVOKE해도 가입 정상(동일 REVOKE가 `bootstrap_user`에 이미 적용돼 검증됨). **service_role 권한은 유지**(moly-auth self-heal이 직접 호출).
- **#24**: RC 웹훅·sweep·하트비트는 유저 루프 밖이라 타임존 필터와 무관(동반 누락 없음). timezone은 NOT NULL DEFAULT 'Asia/Seoul'.
- **#7 idempotency 상점 키**: 상점 구매 키는 `dedupe_expires_at` 미지정 생성(NULL — `app/services/shop.py:272`, 30일 세팅은 `chat.py:1344`뿐) → **NULL 영구 보존이 계약**. 삭제 술어 고정의 근거(♻︎♻︎♻︎ §9.6에서 근거 위치 보강).

### 9.3 초판 문서의 정정된 오류

1. `privacy_jobs.py:71-96`의 "건별 registry DELETE" — **실재하지 않음** (건별 실행은 `mem0_jobs.py:708-720`뿐).
2. diary_recall 임베딩 재생성 — **이미 조건부 스킵 구현됨**(`_UPSERT_DOCUMENT`의 `IS DISTINCT FROM` CASE). 로드맵에서 제거.
3. turn_context 왕복 11회 → 실제 8회(3번째 SAVEPOINT는 플래그 off로 미실행). **`current_turn_context_enabled` 자체가 기본 False·env 전용** — prod 값 확인 전까지 #22 이득은 미확정.
4. §6.6 "routine_completions 호출부 5곳 전부 +activity_date" → 4곳(`routine.py:167` statistics는 routine_id만 필터).
5. §3.2 "user_relationship_states가 스냅샷 보유→절단 가능" → **틀림** (9.1 참조).
6. cutover_guard 위험 범위: memory_text는 이 파일이 추가하는 컬럼이 아니라 **이후 마이그레이션이 DROP하는 legacy 컬럼** — 컬럼이 이미 없는 현 라이브에 적용해도 대화 전멸. README 산문 대신 **파일 내 `IF EXISTS(컬럼)` 하드 가드** 권장.

### 9.4 검증 반영 최종 실행 순서

1. **오늘 바로 (파일 수정만)**: #5(schema.sql `+` 제거) → #3(FK 파일 수정) → #6(cutover_guard 하드 가드)
2. **운영 DDL 경로 정비**: psql 직결 런북 + schema_migrations 수동 기록 절차 (없으면 #1·#2·#9·#10·#13 전부 막힘)
3. **DB 즉효 3연타**: #2(스크럽 부분 인덱스, CONCURRENTLY) → #1(HNSW DROP CONCURRENTLY) → #13(pg_repack/새벽 VACUUM FULL + 점검 창)
4. **#10 수정판** + #4 → **#12(포트·풀 확정)** → #8(백오프만)
5. **#9(대상 6건 이름 확정 후)**, #11, #23 전반부, #15 memory_sweep 배치화, #21
6. **#7 분해 진행**: idempotency(술어 고정)·ai_usage_ledger(롤업) 먼저 → async_jobs(수정판) → relationship_events는 **보류**
7. **재설계 후에만**: #14, #15(recall_diaries·mem0 delete), #22(prod 플래그 확인 후), #25 수정판
8. **의사결정 분리**: #17, #19

> ♻︎♻︎ §9.4는 §9.5 반영 전 버전이다. **실행 시에는 `docs/DB_OPTIMIZATION_ROADMAP.md`(최종 로드맵)를 따를 것.**

### 9.5 ♻︎♻︎ 3차 교차검증 (2026-08-28 밤, Fable 3렌즈: 로드맵 재검증 · API 계약 · 장기 증식)

수정판 로드맵을 세 독립 렌즈로 재검증한 결과. **위 §8 표에는 이미 반영됨(♻︎♻︎ 표기).**

**F1 — #7 replay 사슬 (MAJOR, 3개 렌즈 독립 교차 확증)**: leaf-first 삭제안조차 위험. `/health/queues`가 dead의 "해소됨"을 `succeeded AND replay_of IS NOT NULL` 행 존재로 판정(`jobs.py:700-714`)하는데, 성공한 replay 잡이 정확히 삭제 1순위 leaf다. 지우면 해소된 dead가 카운트에 되살아나 배포 게이트 오탐(8/9 사고 건은 이미 14일 경과 — **첫 retention 실행 즉시 발생**). 확정: 삭제 술어에 `replay_of IS NULL AND NOT EXISTS(자식)` — replay 사슬은 통째로 영구 보존(수십 행, 용량 영향 0).

**F2 — #25 1차 수정판도 오진 (MAJOR)**: §2.4의 106ms 쿼리는 `_STATS_SQL`이 아니라 `scripts/run_memory_consumer.py:44-52`의 `_pending`(state 조건 부재 → seq scan). `_STATS_SQL`은 이미 인덱스 커버. 수정: `_pending`에 state 조건 한 줄 — 신규 인덱스 불필요.

**F3 — §9.4 실행 순서 누락 5건**: #16/#18/#20/#23후반부/#24가 순서에 빠져 있었음. (♻︎♻︎♻︎ 4차 검증이 "최종 로드맵에서 해소"가 허위였음을 발견 — v3 로드맵에는 #20·#23b만 반영돼 있었고 **#16/#18/#24는 누락**돼 있었다. v4에서 #18→Phase 1, #16+#24→Phase 4로 편입 완료, §9.6.)

**F4 및 기타 정정**: ① #7의 "~140MB 회수"는 과대 — DELETE는 성장 중단이지 파일 축소가 아님(축소는 repack). ② #22의 왕복 감소는 8→2가 아니라 8→3, 실패 시 소실 범위는 DB 파생 4필드뿐(Python 계산 필드는 생존) — 수용 가능 확정, 단 `turn_context.py:56-62` 주석 갱신 동반. ③ #23 close_call 배치는 flush ≤1분 + graceful shutdown flush 조건(cutover 게이트가 15분 넘은 started를 미수렴으로 셈). ④ idempotency 30일 만료 후 재사용 계약 변화(409→신규 처리·과금)는 설계 의도 부합이나 명시 승인 필요 + openapi `conversations.yaml`에 30일 경계·409 미기재 상태. ⑤ `db/apply.py:44-56`은 schema_migrations 원장을 이미 기록·검증함 — §6.4의 "원장 도입" 서술은 "psql 직결 실행분의 수동 기록 절차 마련"으로 정정. ⑥ #10 messages_desc_idx 제거·#21 unnest 배치 내 중복·#2 술어 일치·순서 의존성은 전부 재확인 통과.

### 9.6 ♻︎♻︎♻︎ 4차 교차검증 (2026-08-28 밤, Fable 4렌즈: SQL/DDL 실행 정합 · retention 의미 보존 · 실패/롤백/동시성 · 문서 정합)

v3 로드맵을 4개 독립 렌즈로 재검증. 결과는 **로드맵 v4에 전량 반영**(본 절은 기록용). prod 카탈로그 READ-ONLY 실측 + 양형 EXPLAIN 대조 포함.

**A. Phase 5-2 롤업 전면 재설계 (MAJOR — 3렌즈 독립 교차 확증: SQL·retention·failure)**
- `ai_usage_ledger.activity_date`가 **68,360행 중 50,647행(74%) NULL** — background 레인 8곳 중 6곳(checkpoint/reconsolidate/contract/mem0×2 등)이 LedgerContext에 activity_date를 안 넘김(세팅은 `chat.py:1031`·`diary_generation.py:297`뿐). 결과: ① `activity_date < cutoff` 삭제는 NULL에 영원히 미매치 — 테이블 3/4이 안 지워져 목적 미달. ② 롤업 UNIQUE 키의 NULL은 ON CONFLICT 영구 미매칭 — 실행마다 중복 행 증식. ③ 선행 인덱스 `(activity_date)`도 무용. **수정: 축을 `(started_at AT TIME ZONE 'Asia/Seoul')::date`로**(started_at NOT NULL), 인덱스도 `(started_at)`.
- **원자성**: "upsert 후 DELETE" 2커밋 분리 시 크래시-재시도에서 이중집계(가산형) 또는 과소집계(덮어쓰기형) — dedup_key는 같은 잡 행의 재실행을 못 막음. **수정: 배치 = 단일 문장** `WITH del AS (DELETE … LIMIT :n FOR UPDATE RETURNING *) INSERT … SELECT 집계 FROM del ON CONFLICT DO UPDATE SET calls=+EXCLUDED…`(가산형 exactly-once).
- **불변식 2**: unknown_usage 6,836행 전부 `cost_micro_usd`·상한 추정 NULL — 원본 삭제 순간 미확정 비용이 0원으로 확정. **수정: unknown_usage 삭제 제외(보존+경보)** + 롤업 키에 status 축 추가(failed 카운트 보존). 관련: stale `started`→`unknown_usage` 변환 코드가 **0곳**(불변식의 이행 경로 부재) → #23b에 24h reconciler 동봉, 기존 started 잔존 10건(8/6~) 선제 triage(안 하면 5-2 경보 첫날부터 상시 발화).

**B. Phase 5-4 잠복 결함 (MAJOR-잠복)**: 후보 GC 술어에 pending registry 의존성 검사 부재. consolidate 커서(`GREATEST` 전진)가 dead 턴을 지나친 pending의 재판정(`_UNJUDGED_USERS` 매시간)은 후보 본문을 읽음(`_CANDIDATE_TEXTS`, mem0_jobs.py:504-508) — 후보 삭제 시 `candidate_text_missing` → 재판정 영구 dead 루프 + 그 기억 영구 pending(회상 불가, vecs 사본은 코드가 안 읽어 복구 불가). 현재 위험 0건(pending 0)이나 pending 14일+ 잔존(8/9 사고급)과 겹치면 발화. **수정: `NOT EXISTS(pending registry 참조)` 절 필수** + 금지 목록에 등재. 또 `consolidated_through_turn_seq`는 candidates가 아니라 `memory_pipeline_states` 소속 — 조인 명시(문면 그대로는 컬럼 미존재 오류).

**C. vecs 인덱스 술어 판정 (두 렌즈 상충 → 직접 EXPLAIN으로 판정)**: `((metadata->>'user_id'))` B-tree는 **이미 prod 존재**(`moly_memories_v2_user_idx`, 마이그레이션 20260805_mem0_v2_collection.sql:22 — idx_scan 95는 privacy/verify 스크립트의 `->>` 쿼리). 그러나 vecs `$eq`는 `metadata->'user_id' = …::jsonb`를 생성(collection.py:934) — **양형 plain EXPLAIN 대조 결과 `->` 형태는 Seq Scan, `->>` 형태만 Index Scan**. 즉 "회상은 지금도 seq scan"은 유효(#1 드랍 안전 재확증)하고, v3 Phase 6의 `->>` 처방은 이중 오류(이미 존재 + 연산자 불일치). **수정: `((metadata->'user_id'))` jsonb 표현식 인덱스 + 생성 후 vecs 술어 형태 EXPLAIN을 완료 기준으로.** §9.2 #1의 `@>` 서술, §8 #14의 GIN 처방도 동일 근거로 정정.

**D. 실행 경로·런북 (failure 렌즈)**
1. **Phase 0 checksum 충돌**: 편집 대상 2파일이 prod·dev 원장에 checksum 기록됨(5add934f…, e83d9dfd…) — 파일만 고치면 이후 apply.py 전면 실패(apply.py:42-49). 편집 후 원장 checksum 동기화 절차를 Phase 0·1에 명기.
2. pg_repack: 확장 미설치(가용 1.5.2) → CREATE EXTENSION + CLI 버전 정확 일치 + **`--no-kill-backend` 필수**(기본이 경합 백엔드 kill) + 실패 잔존물(z_repack 트리거·repack.log_*) 정리 절차. INVALID 인덱스 탐지는 vecs·public 스코프 한정(storage.objects에 Supabase 내부 invalid 1건 상존).
3. 실측 구조 갱신: heap 13MB + TOAST 251MB + HNSW 241MB ≈ 508MB — 2-2 드랍만으로 241MB 즉시 회수, repack 실대상은 TOAST. VACUUM FULL fallback 소요는 1~3분급(90분 창은 과대 산정이었음).
4. DDL 공통 `SET lock_timeout='2s'` + 재시도(#4 FK 드랍은 messages에도 AE 락, postgres 롤 timeout 무설정 실측). #12: max_connections=60 실측 — 롤링 배포 구·신 2배 계수 필수.
5. tick 조건을 `hour>=5 AND 오늘 잡 미존재`로(워커 다운 시 silent skip 방지 — dedup이 1회 수렴 보장하므로 공짜 self-heal). /health/deep retention 임계 ≥25h. retention 잡 priority 200(maintenance 큐 concurrency=1에서 탈퇴 청소 우선).

**E. 로드맵 정합 (consistency 렌즈)**: ① §9.5 F3의 "최종 로드맵에서 해소"가 허위 — #16/#18/#24 미수록이었음 → v4 편입(#18→Phase 1, #16+#24→Phase 4, #24 안전조건 전문 포함). ② #15c(recall_diaries 안전 재작성판)·#21 privacy 영벡터→직접 SQL·#25 후반부(profile_renders 조건부)가 미배정 → v4 편입. ③ #20 후반부(moly-auth 벡터 정리)는 **2026-08-28 별도 세션에서 이미 완료**(delete_user_memories RPC 운영 적용 + moly-auth#30) — v4에 완료 표기. ④ 무근거 처방 2건 정리: "reaper 10→30초" 철회(어느 라운드도 미검증, lease 회수 지연 부작용 미평가), 5-3 동봉 청소는 retention 렌즈가 안전 확인(lease 기반 단명 뮤텍스)하되 만료 기준 술어 명시 조건. ⑤ 참조 오류 정정: autovacuum 튜닝에 5-6 번호 부여, "§9.5 F8"→§6.2, 금지 목록 shop.py:272 근거, #17 "checkpoint·diary_recall" 복원, #10 "미사용" 전제 정정(idx_scan 0은 diary 2건뿐 — 나머지는 대체 인덱스 게이트 필수), 2-1 "정확 일치"→"함의 관계".

**F. 4차에서 의미 보존이 재입증된 것들**: 5-3 술어 vs /health 재귀 사슬·`_STALLED` replay 억제·dedup 키 해방(전 producer 키 규약이 단조/날짜 스코프)·5-1 상점 키 보존(261행)·5-5 결제 감사·#22/#23a/#8 롤링 배포 공존성·2-1 술어 함의·5-4 planned 불가침 — 전부 통과. replay 사슬 실측: 자식 47·최대 깊이 3(영구 보존 비용 ~78행).

**API 계약 렌즈 결론**: 전 엔드포인트→테이블 매핑 후 대조 결과, 수정판 준수 시 응답 스키마·상태코드·정렬 계약이 바뀌는 항목 0건(암묵 정렬 의존도 전수 0건 — 모든 목록 API에 명시 ORDER BY 존재). 유일한 계약 변화는 위 ④(승인 항목).

**장기 증식 렌즈 결론**: pg_cron 미도입(무감시 제2 실행면 — 실패 관측 출구가 워커 쪽에만 있음: dead→Slack, /health 게이트, job_attempts). retention 잡 5종을 maintenance 큐 + tick KST 05시 enqueue(`{job_type}:{KST날짜}` dedup)로 설계. 상세는 최종 로드맵 문서 Phase 5·6.

---

## 10. 부록 — 원자료 위치

- pg_stat_statements 상위 쿼리·테이블/인덱스 실측: 본문 §1~§3 표 (2026-08-28 09:00 KST 기준)
- Supabase advisor: performance 28건(unindexed FK 16, no PK 2, unused index 10), security 51건(RLS no-policy 45, SECURITY DEFINER 6종 등)
- 코드 근거는 전부 본문에 `파일:행` 형식으로 인라인 표기
- 관련 문서: `docs/DB_REFACTOR.md`, `docs/ERD.md`, `db/migrations/README.md`, `moly-infra/docs/INFRA.md`(TODO #18 풀 설정)
