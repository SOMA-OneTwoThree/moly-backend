# moly DB 최적화 — 최종 실행 로드맵 (v4)

- **작성일**: 2026-08-28 밤 (v4 — 4차 검증 반영)
- **성격**: 실행 문서. 근거·실측·검증 이력은 전부 `DB_OPTIMIZATION_ANALYSIS.md`(이하 "분석 문서")에 있다 — 항목 번호(#n)는 분석 문서 §8과 동일(#28·#30은 검증 라운드에서 추가된 신규 번호).
- **검증 이력**: 실측(63일 pg_stat_statements) → 2차 코드 검증(Opus 2렌즈, 분석 §9) → 3차 교차검증(Fable 3렌즈: 로드맵·API 계약·장기 증식, 분석 §9.5) → **4차 교차검증(Fable 4렌즈: SQL/DDL 실행 정합·retention 의미 보존·실패/롤백/동시성·문서 정합, 분석 §9.6)**. 4차에서 Phase 5-2 전면 재설계(`activity_date` 74% NULL — 3개 렌즈 독립 교차 확증) 포함 주요 결함 수 건이 수정됨.
- **대원칙**: 기존 서비스(기억·대화·일기·재화·푸시·탈퇴)의 **동작 의미와 API 계약을 바꾸지 않는다.** 500이 안 나는 것으로는 부족하다 — 잘 돌아가도 결과·순서·타이밍·데이터 의미가 달라지면 실패.

---

## 절대 금지 목록 (어느 Phase에서도 위반 불가)

| 금지 | 이유 (분석 문서 근거) |
|---|---|
| `relationship_events` 절단 | 관계 단계 영구 동결 — 스냅샷이 누적기가 아니라 전량 재계산+GREATEST (§9.1). 해금 조건: Phase 6의 누적기 리팩터 완료 |
| async_jobs의 dead 행 삭제 | 배포 게이트(`dead_total 증가=실패`) 무력화 (§9.1) |
| async_jobs의 replay 사슬 구성원(`replay_of IS NOT NULL`) 삭제 | 해소된 dead가 카운트에 되살아나 게이트 오탐 — 3렌즈 교차 확증 (§9.5 F1) |
| 회상 쿼리의 ANN(벡터 우선 LIMIT) 전환 | 유저당 ~15벡터 환경에서 회상 붕괴(기대값 0.04개) (§9.1). 재검토 트리거: Phase 6 표 |
| recall_diaries 벡터 선절단 | 주 경로는 벡터 없음 + 창 집계 절단으로 `no_content_match` 오판 (§9.1). 안전 재작성판은 Phase 4 #15c |
| mem0 delete 단순 일괄화(실패 시 전원 failed) | `failed`→`pending` 복구 코드 0곳 — 벡터 영구 고아 (§9.1) |
| **pending registry가 참조하는 committed 후보(candidate) 삭제** | pending 재판정이 후보 본문을 읽음(`mem0_jobs.py:504-508`) — 삭제 시 재판정 영구 dead + 그 기억 영구 회상 불가 (§9.6) |
| turn_context SAVEPOINT 전체 제거 | 트랜잭션 오염 → 챗 전체 500 (§9.1). 1쌍은 유지 |
| usage_ledger `open_call` 선커밋 제거 | 불변식 2(비용 은폐 금지) + cutover 게이트 의존 (§9.1) |
| **unknown_usage 원장 행을 상한 추정 없이 삭제** | 원본 삭제 순간 "미확정" 비용이 0원으로 확정 — 불변식 2 위반 (§9.6). 현재 6,836행 전부 상한 미기록 |
| `/health/queues` 시간 범위 제한 | 미해결 dead 은폐 (§9.1) |
| idempotency 삭제 술어 변형 | `dedupe_expires_at IS NOT NULL AND <= now()` 외 어떤 형태도 금지 — 상점 구매 키는 `dedupe_expires_at` 미지정 생성(NULL, `shop.py:272`) = 영구 보존이 계약 |
| 서비스 시간대의 비CONCURRENTLY 인덱스 생성/드랍·VACUUM FULL | 락 창 동안 회상 fail-open → 대화가 기억 없이 나감 (§9.2). **유일 예외**: Phase 2-3의 새벽 점검 창 절차(컨슈머 정지+공지 동반) |

---

## Phase 0 — 오늘 바로 (파일 수정만, 라이브 무접촉)

**⚠︎ 원장(checksum) 정책 (§9.6 F1)**: `20260805_memory_v2_tables.sql`·`20260804_memory_cutover_guard.sql`은 prod·dev `schema_migrations`에 checksum이 기록돼 있다(각각 5add934f…, e83d9dfd…). **파일만 편집하면 다음 `db/apply.py` 실행이 checksum 불일치로 전면 실패**(apply.py:42-49). 편집 후 반드시 prod·dev 원장의 해당 행 checksum을 새 값으로 UPDATE하는 절차를 함께 수행하고, 그 UPDATE 문을 본 문서 실행 기록에 남긴다.

| 항목 | 작업 | 완료 기준 |
|---|---|---|
| #5 | `db/schema.sql:522` 선두 `+` 제거 (원장 무관 — 부트스트랩 파일) | `psql -f` 파싱 통과 |
| #3 | `db/migrations/20260805_memory_v2_tables.sql:237`을 라이브와 동일한 `ON DELETE SET NULL (source_message_id)`로 + **prod·dev 원장 checksum 동기화** | 파일=라이브 FK 일치, 이후 apply.py 무오류 |
| #6 | `20260804_memory_cutover_guard.sql`에 `IF EXISTS(chat_contexts.memory_text 컬럼)` 하드 가드 삽입 (README 산문 아님) + **원장 checksum 동기화** | 컬럼 없는 DB에 적용해도 no-op |
| 신규 | dev DB(wywzjslvxwttxkecbyis)에 FK 수정 + `delete_user_memories` RPC 적용. **사전 확인**: dev PG 버전 ≥15 (`SET NULL (컬럼)` 구문은 PG15+) | dev=prod 스키마 정합 |

## Phase 1 — 운영 DDL 경로 정비 (이게 없으면 Phase 2~3 전부 막힘)

`db/apply.py`는 전체를 트랜잭션으로 감싸 `CREATE/DROP INDEX CONCURRENTLY`·`VACUUM FULL`을 실행할 수 없다(§9.2).

1. psql 직결(5432) 실행 런북 작성. 포함 사항:
   - **모든 DDL 세션 공통: `SET lock_timeout='2s'`** + 타임아웃 시 재시도 루프(§9.6 — FK 드랍은 messages에도 ACCESS EXCLUSIVE, postgres 롤에 timeout 미설정이라 무제한 대기 위험).
   - 실패 시 `INVALID` 인덱스 정리 절차 — 탐지 쿼리는 **vecs·public 스코프 한정**(`storage.objects`에 Supabase 내부 invalid 인덱스 1건이 원래 존재 — 건드리지 말 것).
   - pg_repack 절차: 확장 **현재 미설치** → `CREATE EXTENSION pg_repack`(사용 가능 버전 1.5.2), **CLI 버전 1.5.2 정확 일치** 필수, `-k/--no-superuser-check` + **`--no-kill-backend` 필수**(기본 동작은 60초 후 경합 백엔드 cancel→terminate = 라이브 쿼리 킬), 실패 시 잔존물(z_repack 트리거·repack.log_*) 정리는 `DROP EXTENSION pg_repack CASCADE` 후 재설치.
2. psql 직결로 실행한 DDL의 `schema_migrations` **수동 기록 절차** + **기존 파일 편집분의 checksum 동기화 절차**(Phase 0 정책과 동일 — apply.py:44-56의 검증과 정합 유지). ⚠︎ `promote_memory_v2.sql`류 운영 스크립트는 apply.py 가드를 우회하므로 런북 경로로만 실행.
3. 사전 확인 3건: prod DSN 포트(5432 세션 vs 6543 트랜잭션 — #8·#12의 분기점), `schema_migrations` 테이블 존재(확인 완료), pg_repack 준비(위 절차).
4. **#18**: `db/schema.sql`을 pg_dump 기반으로 재생성(현재 테이블 16개 누락) + cutover 잔재 정리(§6.4~6.5). 부트스트랩 경로 복구의 본체 — #5는 응급 처치일 뿐.

## Phase 2 — DB 즉효 3연타 (기대효과: DB 시간 ~40% 제거, ~400MB 회수)

순서 고정. 전부 psql 직결. **실측 구조(08-28 재측정)**: `vecs.moly_memories_v2` = heap 13MB + TOAST 251MB + HNSW 241MB ≈ 총 508MB — **2-2만으로 241MB 즉시 회수, repack의 실제 대상은 TOAST**.

| 순서 | 작업 | 조건 | 검증 |
|---|---|---|---|
| 2-1 (#2) | 스크럽 부분 인덱스 2개 `CONCURRENTLY` 생성: `async_jobs(payload_expires_at) WHERE payload_redacted_at IS NULL AND payload_expires_at IS NOT NULL`, `idempotency_keys(response_expires_at) WHERE response IS NOT NULL` | 인덱스 술어는 스크럽 쿼리(`jobs.py:359-371`) WHERE의 **부분집합(함의 관계)** — 플래너 사용 가능 확인 완료. async_jobs 쪽에 `state IN ('succeeded','dead','cancelled')`를 추가하면 더 작아짐(선택) | 스크럽 쿼리 mean 46ms → <1ms |
| 2-2 (#1) | `DROP INDEX CONCURRENTLY vecs.moly_memories_v2_hnsw_idx` | 회상은 지금도 seq scan(vecs `$eq`는 `metadata->'user_id' = …::jsonb`를 생성 — 기존 `->>` B-tree 못 탐, 08-28 EXPLAIN 재확증 §9.6). **드랍 직전 vecs 실제 술어 형태로 plain EXPLAIN 1회 게이트**. 실패 시 INVALID 잔존 → 동일 DROP 재실행으로 완결 | 벡터 upsert p95 60s → ms대. 회상 결과 диф 0 |
| 2-3 (#13) | `vecs.moly_memories_v2` pg_repack — TOAST 251MB가 실제 대상 (불가 시 새벽 02:00~03:30 KST VACUUM FULL + 컨슈머 일시 정지 + 점검 공지 — 실측 기준 소요 1~3분급이나 창은 여유 있게 유지) | 반드시 2-2 이후(재작성 대상 절반). `--no-kill-backend` 필수(Phase 1 런북). **"롤백" 아님**: 실패 시 원본 무손상 — 잔존물 정리 후 재시도 | 총크기 508MB → ~100MB. 직후 autovacuum 튜닝(Phase 5-6) |

2-2 롤백(필요시): `CREATE INDEX CONCURRENTLY`로 HNSW 재생성 — 라이브 ~14K행이라 분 단위·무차단. `SET maintenance_work_mem` 상향, CIC 실패 시 invalid 정리 후 재시도.

## Phase 3 — 인덱스 정리·연결·폴링 (기대효과: 유휴 쿼리 ~85%↓, 쓰기 증폭 완화)

**순서: #4 → #10 → #9 → #12 → #8 → #25** (#4가 #9 목록을 바꾼다 — 아래).

| 항목 | 작업 | 핵심 조건 |
|---|---|---|
| #4 | `conversation_checkpoints`의 단일컬럼 RESTRICT FK 제거(복합 CASCADE 유지) | 실행 직전 `pg_constraint`로 복합 FK 존재 재확인(08-28 실측 존재 확인됨). `lock_timeout='2s'` 필수(messages에 AE 락) |
| #10 | 제거 4건(CONCURRENTLY): `async_jobs_provider_claim_idx`(**claim_idx가 아님** — 방향 주의), `messages_user_id_desc_idx`, `chat_response_references_reply_idx`, `diary_recall_documents` HNSW+trgm | UNIQUE 제약 쪽은 절대 건드리지 않음. **⚠︎ idx_scan=0은 diary 2건뿐** — 나머지 3건은 플래너가 현재 사용 중(provider_claim 1.67M회 등)이며 "동일 키 대체 인덱스가 받는다"가 전제. **드랍 게이트**: 각 건마다 ① 사용 쿼리 식별+대체 인덱스로 EXPLAIN ② 드랍 직후 해당 쿼리 mean 감시(pg_stat_statements) |
| #9 | FK 인덱스 추가(CONCURRENTLY) — 5건: `mem0_memory_sources(user_id,source_message_id)`, `mem0_ingest_candidate_sources(user_id,source_message_id)`, `diary_claim_sources(user_id,message_id)`, `greetings(committed_message_id)`, `async_jobs(user_id)`. + `routine_completions(user_id,activity_date)`, `hay_transactions(user_id,id)`, registry 부분 인덱스는 **`mem0_memory_registry(provider_delete_state) WHERE provider_delete_state IN ('pending','failed')`로 명시**(`status` 컬럼은 없음 — §9.6) | 탈퇴 CASCADE 경로 우선. ~~`conversation_checkpoints(through_message_id)`~~ **제외** — #4가 지우는 RESTRICT FK 전용이었고 복합 FK 조회는 기존 `conversation_checkpoints_latest_idx`가 커버(§9.6) |
| #12 | prod 포트 확정 → `pool_size`/`max_overflow`/`pool_timeout` 명시 | **prod max_connections=60 실측(현재 ~22 사용)**. 상한 산정에 롤링 배포 중 구·신 프로세스 동시 생존(2배) 계수 필수 — 프로세스 합산 ≤ (60 − 내부 예약 − 관리 여유) / 2 |
| #8 | 컨슈머 유휴 백오프(빈 claim 1→최대 5~10초 지수, 잡 잡으면 즉시 복귀) + finalize 후속 enqueue 시 큐 루프 웨이크업(사슬 지연 방지) | LISTEN/NOTIFY는 채택하지 않음(6543 트랜잭션 풀링과 양립 불가). ~~reaper 10→30초~~ **철회** — 어느 검증도 통과하지 않은 무근거 처방(§9.6), lease 회수 지연 부작용 미평가 + #2 이후 스크럽이 싸져 실익도 낮음 |
| #25 | `scripts/run_memory_consumer.py:44-52` `_pending`에 `AND state IN ('ready','running','dead')` 한 줄 추가 | 신규 인덱스 불필요 — 기존 `async_jobs_state_queue_idx` 사용(§9.5 F2). 실행 전 EXPLAIN 1회 |

## Phase 4 — 앱 왕복 감소 + tick 재설계 (기대효과: 턴당 왕복 ~50→~35, LLM 호출당 3→2)

| 항목 | 작업 | 핵심 조건 |
|---|---|---|
| #23a | `_touch_last_active`를 `_PUBLISH` SET 절에 병합 — **`chat_turns.py:82`의 `_PUBLISH`**(동명 상수가 `contract_repo.py:33`에도 있음 — 혼동 주의) | `now()` 금지, 턴의 `:now` 파라미터(저녁 푸시 활동일 보존) |
| #11 | `ai_price_catalog` 프로세스 캐시(TTL 5분) + `app_config` 요청당 2회→1회 병합 | 읽기 시점을 유저 락(`chat.py:818`) 이후로 유지(TOCTOU). "왜 이 테이블만 캐시 예외인지" 주석 |
| #21 | mem0 ingest 배치화: `unnest()` 배열로 부모 INSERT 전량→자식 INSERT 전량 2문. **+ privacy_cleanup의 유저 벡터 열거를 영벡터 유사도 검색(`mem0_adapter.py:242-248`)에서 직접 SQL로 대체** — bounded(`WHERE id IN (SELECT … LIMIT :n)`) 유지 | `INSERT…RETURNING` CTE 금지(resume 근거 소실 — 과거 운영사고 재발) |
| #15a | memory_sweep `_apply` 배치화(enqueue·BUMP·upsert를 세트 기반으로) | 진범은 `job_finalize_timeout_s=5.0` — 배치화로 5초 안에 수렴시키는 것이 목표 |
| #15b | mem0 provider delete: `delete(전체 ids)` 1회 + 성공 시에만 전원 `deleted`, 실패 시 마킹 없이 `JobRetry` | 전원 failed 마킹 금지 |
| **#15c** | recall_diaries CTE 재작성 — LIMIT 없는 published 전량 실체화(§5.2 "최악") 해소. **절단 기준은 벡터가 아니라 `display_date DESC` 상한 + 별도 COUNT** | 주 경로(query 없음=벡터 없음)와 `no_content_match` 창 집계 의미 보존 — 벡터 선절단 금지 유지 |
| 신규(#28) | provider_delete `failed`→`pending` 복구 스캔을 memory_sweep에 추가 | 기존 failed 잔존물 회수 |
| #23b | usage_ledger `close_call`만 배치 flush + **stale-started reconciler 동봉**: 24h 넘은 `started` → catalog 상한 추정과 함께 `unknown_usage` 전환(최장 lease 180s ≪ 24h — in-flight 오탐 불가). 현재 이 변환 코드가 0곳이라 배치화가 유실 창(≤1분)을 처음 만든다(§9.6) | `open_call` 선커밋 유지, flush ≤1분 + graceful shutdown flush(**API 프로세스의 챗 lane 포함** — consumer만이 아님). **배포 전 기존 started 잔존 10건(8/6~) 수동 triage**(안 하면 Phase 5-2 경보가 첫날부터 상시 발화) |
| **#16+#24** | tick 재설계: **선행 — 타임존 필터 분리**(`WHERE timezone=ANY(:tzs)` **문자열 동등만**, SQL `AT TIME ZONE` 금지 — 이상 tz 1행이 틱 전체를 죽여 그날 일기·푸시 전멸. `:tzs`는 **hour 단위** 산출) → 이후 timezone을 페이징 쿼리에 포함, 유저당 세션 제거, LLM/푸시 호출 전 세션 close (§5.3) | RC 웹훅·sweep·하트비트는 유저 루프 밖 — 동반 수정 불필요(§9.2). timezone은 NOT NULL DEFAULT 'Asia/Seoul' |
| #22 | (선행: prod `CURRENT_TURN_CONTEXT_ENABLED` env 확인 — 기본 False면 **스킵**) SAVEPOINT 3쌍→1쌍 + CTE 병합 (왕복 8→3) | `turn_context.py:56-62` 주석 갱신 동반. 열화 시 DB 파생 4필드 동시 소실 수용을 명시 결정 |

## Phase 5 — 장기 수명주기: retention 잡 5종 (무한 증식 종식)

**실행 기반 확정**: pg_cron 미도입. 전부 **maintenance 큐 잡** + tick enqueue — 조건은 `hour==5`가 아니라 **`KST hour>=5 AND 오늘 잡 미존재`**(dedup_key=`{job_type}:{KST날짜}`가 1회 수렴 보장 — 워커가 05시대에 죽어 있어도 그날 중 self-heal, §9.6). 실패 관측은 기존 체계 그대로: dead→Slack, `/health/queues` 게이트, job_attempts. 배치당 커밋, 잔량은 `:{seq}` 연쇄 재enqueue(privclean 패턴). **retention 잡 priority=200(후순위)** — maintenance 큐 concurrency=1에서 탈퇴 청소(privclean)·sweep이 우선. 배치 크기는 timeout 60s·max_attempts 3 안에 1링크가 수렴하게 보수적으로.

선행 인덱스 3개(psql CONCURRENTLY, Phase 2-1과 같은 창 가능): `async_jobs(finished_at) WHERE state IN ('succeeded','cancelled')`, `idempotency_keys(dedupe_expires_at) WHERE dedupe_expires_at IS NOT NULL`, **`ai_usage_ledger(started_at)`** (~~activity_date~~ — 74% NULL이라 무용, §9.6).

실행 전 1회: job_type별 dedup 키 규약 전수 확인(14일 후 키 해방이 과거 의미를 재발화시키는 producer 없음 — 4차 검증 통과, 실행 직전 재확인만).

| 잡 | 주기 | 내용 | 절대 조건 |
|---|---|---|---|
| 5-1 `retention_idempotency_gc` | 일 1회 | `dedupe_expires_at IS NOT NULL AND <= now()` LIMIT 2000×10배치 | 술어 변형 금지. **승인 필요한 계약 변화 1건**: 30일 지난 키 재사용이 409→신규 처리(과금) — openapi `conversations.yaml`에 30일 경계·409 명문화 동반 |
| 5-2 `usage_ledger_rollup` | 일 1회 | **4차 재설계(§9.6 — 5중 교차 확증)**. 집계·삭제 축은 `(started_at AT TIME ZONE 'Asia/Seoul')::date`(**activity_date 금지** — 74% NULL: NULL 행은 영원히 안 지워지고, 롤업 UNIQUE 키의 NULL은 ON CONFLICT 미매칭→실행마다 중복 증식). 롤업 키 `(kst_date,provider,model,lane,purpose,status)` — status 축으로 failed 카운트 보존. **원자성: 배치 = 단일 문장** `WITH del AS (DELETE … WHERE ctid IN (SELECT ctid FROM … LIMIT :n FOR UPDATE) RETURNING *) INSERT INTO ai_usage_daily_rollup … SELECT 집계 FROM del ON CONFLICT DO UPDATE SET calls=rollup.calls+EXCLUDED.calls, …`(가산형 — 어느 지점에서 죽어도 각 행 정확히 1회 집계). 삭제 대상: `status IN ('succeeded','failed')` 90일 이전분 | `started` 행은 연령 무관 삭제 금지 — 7일 넘은 started 잔존은 경보(#23b reconciler가 24h에 수렴시키므로 평시 0). **`unknown_usage` 행은 삭제 제외(보존+경보)** — 상한 추정 기록이 실작동하기 전까지, 원본 삭제=미확정 비용의 0원 확정(불변식 2 위반). 첫 실행 후 롤업 합계=원본 합계 대조 필수 |
| 5-3 `retention_jobs_gc` | 일 1회 | `state IN ('succeeded','cancelled') AND finished_at < now()-'14d' AND replay_of IS NULL AND NOT EXISTS(SELECT 1 FROM async_jobs r WHERE r.replay_of=j.id)` LIMIT 2000×10배치 + 고아 `diary_gen_claims`/`chat_active_turns` 청소(둘 다 lease/steal 만료 기반 단명 뮤텍스 — `lease_until < now()-'7d'` 등 **만료 기준 술어 명시**, 4차 검증 안전 확인) | dead·replay 사슬 절대 제외(실측: 사슬 ~78행, 영구 보존 비용 무시 가능). job_attempts는 FK CASCADE 자동 |
| 5-4 `mem0_candidate_gc` | 일 1회 | `status='committed'` && **`memory_pipeline_states`와 user별 조인**으로 `turn_seq <= consolidated_through_turn_seq`(이 컬럼은 candidates가 아니라 pipeline_states 소속 — §9.6) && 14일 경과 && **`AND NOT EXISTS(SELECT 1 FROM mem0_memory_registry r WHERE r.user_id=c.user_id AND r.provider_memory_id=c.provider_memory_id AND r.semantic_status='pending')`** → 삭제(sources CASCADE). dead 후보는 90일 | `planned` 절대 비대상(durable plan). active/ambiguous/pending registry·그 sources·vecs 라이브 벡터는 불가침. **pending NOT EXISTS 절은 생략 불가** — 커서를 지나친 pending의 재판정(매시간 `_UNJUDGED_USERS`)이 후보 본문을 읽음: 삭제 시 candidate_text_missing → dead 무한 루프 + 그 기억 영구 pending(§9.6 잠복 결함, 현재 위험 0건이나 장애기와 겹치면 발화). 부수효과: 평문 candidate_text 잔존 무한→14일(§7 개인정보 보완) |
| 5-5 `retention_rc_events` | 월 1회 | processed 365일 경과 삭제. failed/pending 무기한 | 결제 감사 보존 |

**5-6 (Phase 2-3 직후)**: vecs `ALTER TABLE ... SET (autovacuum_vacuum_scale_factor=0.02, toast.autovacuum_vacuum_scale_factor=0.02)` — 정상 상태의 1차 수단은 repack이 아니라 autovacuum 평형(TOAST 251MB가 실제 팽창 축). `/health/deep`에 테이블 크기·vecs 팽창비·**retention 잡 최근 성공 시각(임계 ≥25h — 24h면 상시 오탐)** 추가.

## Phase 6 — 성장 단계별 트리거 (조건 도달 전 실행 금지, 도달 시 착수)

| 트리거 조건 | 실행 |
|---|---|
| vecs 라이브 >100K행(유저 ~7천) 또는 회상 `ORDER BY vec <=>` mean >300ms (현 50ms) | **`((metadata->'user_id'))` jsonb 표현식 B-tree** — vecs `$eq`가 생성하는 `metadata->'user_id' = …::jsonb` 술어와 정확 일치(`vecs/collection.py:934`, 08-28 EXPLAIN 확증 §9.6). ⚠︎ 기존 `moly_memories_v2_user_idx`(`->>` text)는 privacy/verify 스크립트 전용으로 회상 경로를 못 탄다 — 유지하되 혼동 금지. GIN(metadata)도 부적합(vecs는 `@>` 미사용). **완료 기준: 생성 후 vecs 실제 술어 형태 EXPLAIN으로 Index Scan 확인.** HNSW 재도입은 유저당 >2천 벡터 시에만(현 구조상 사실상 미도달) |
| vecs 총크기 > 3×(라이브행수×7KB) 또는 >300MB | pg_repack 재실행 (분기 점검, 02:00~03:30 KST, `--no-kill-backend`) |
| async_jobs 유입 >5만/일(≈1만 유저) | retention 14d→7d |
| ai_usage_ledger >10만행/월 | 롤업 창 90→30d |
| **relationship_events >100만행(≈5만 유저) 도달 전** | **누적기 리팩터 마감선**(#30, 신규): `user_relationship_states`를 증분 누적기로 + 경계 이전분 `baseline_*` 봉인 → 완료 시에만 events 90d 절단 해금 |
| messages >500만행 또는 1GB | #19 content 분리 착수(파티셔닝은 복합 FK 8개 해소 전 불가). 착수 시 checkpoint CASCADE 재고(§6.2) |
| ai_usage_ledger 삽입 >100만/월(≈10만 유저) | ledger 월 파티션(파티셔닝 적합 유일 테이블) — 이때만 pg_cron+pg_partman 재검토 |
| DB >8GB | Supabase 플랜·백업/PITR 재점검 |

## 의사결정 분리 항목 (기술 작업이 아니라 제품 결정)

- **#17 checkpoint**: "켜기"(프롬프트 의미 변화 — A/B 필수)와 "끄기 확정+죽은 기계장치 제거"(안전)를 분리 승인. `memory_generation` 컬럼은 **checkpoint·diary_recall 쿼리가 읽으므로** 컬럼 삭제는 별도 작업.
- **#20 SECURITY DEFINER 3함수 REVOKE**: 기술적으로 안전 확정(트리거는 CREATE 시점만 권한 검사, 동일 REVOKE가 bootstrap_user에서 검증됨). 실행 전 anon 경유 `/rest/v1/rpc/` 실호출 0건을 액세스 로그로 확인, **service_role 권한은 유지**. (#20 후반부 — moly-auth 탈퇴 경로의 v2 벡터 정리 — 는 **2026-08-28 완료**: `delete_user_memories` RPC 운영 적용 + moly-auth deleteMemories 교체, PR moly-auth#30 — 머지 여부만 확인.)
- **#19 messages content 분리**: 목적 재정의 후(#10과 무관한 축 — TOAST/폭 문제). Phase 6 트리거 도달 시.
- **#25 후반부 `relationship_profile_renders` 파생 테이블 제거**: 읽는 곳 3곳을 동시 정리할 때만(조건부 — 분석 §5.6).
- idempotency 30일 계약 변화(Phase 5-1): 사실상 클라 버그 시나리오에만 해당하나 명시 승인 필요. → **2026-08-29 사용자 승인 완료** — 배포 전 결정 사항 전부 종결.

## 실행 기록

**2026-08-28 밤 — Phase 0 실행** (사전 최종 검증 → 실행 → 사후 검증 체제):
- 사전 검증: 파일 3개 결함 원문 확인, checksum 알고리즘 실증(파일 sha256=prod 원장 값 일치), prod FK 현재값 `ON DELETE SET NULL (source_message_id)` 확인, prod에 memory_text·memory_mode 컬럼과 가드 트리거 잔존 확인(하드 가드는 미래/신규 DB용 방어).
- #5 ✅ `db/schema.sql:522` `+` 제거 — 로컬 임시 PG17에서 **전체 부트스트랩 실행 통과**(public 테이블 36개 생성, 에러 0). 테이블 16개 누락 문제는 #18(Phase 1) 스코프.
- #3 ✅ FK 파일 수정 + 재발 방지 주석 — 파일 문자열이 prod `pg_get_constraintdef` 출력과 일치.
- #6 ✅ 하드 가드(DO 블록 + `IF NOT EXISTS(chat_contexts.memory_text) → no-op`) — 로컬 PG17에서 **양 분기 실증**: 컬럼 없음→NOTICE+트리거 미설치, 컬럼 있음→설치+normalized 강제 NULL·downgrade 차단·legacy 보존 전부 원본과 동일.
- 원장 동기화 ✅ (prod, CAS UPDATE — 구 checksum 일치 조건부):
  - `UPDATE public.schema_migrations SET checksum_sha256='e74115d5cda7991a513ef4248f20a925fc7a42abf7589e804eca45f79b826658' WHERE migration_name='20260805_memory_v2_tables.sql' AND checksum_sha256='5add934f4551fa799eb37b6916db6bc834589b449d6efc9fe52a68200764d9cd';`
  - `UPDATE public.schema_migrations SET checksum_sha256='5fe3c61f5c01da47b941ef5ba6b1a8a61122b093ca9def186a1833ecd54ea7a1' WHERE migration_name='20260804_memory_cutover_guard.sql' AND checksum_sha256='e83d9dfd5dcc448ae27b0150ec631ac077c95efd686e4882d855d06db026e3ed';`
- dev DB 패치 ⛔ **블로커**: dev(wywzjslvxwttxkecbyis) 접속 수단 없음 — MCP 권한 없음 + `.env`에 dev DSN 부재(11행에 비밀번호 만료된 **prod** DSN이 들어 있음 — envfile fail-closed 가드가 쓰기사고는 차단하나 정리 필요). dev DSN 확보 후: FK ALTER + `delete_user_memories` RPC(정본은 prod `pg_get_functiondef` 추출본) + dev 원장 checksum 동기화.
- 참고: `delete_user_memories` 소스가 레포에 없음(PR moly-backend#212 미머지 추정) — 현 브랜치 `feat/evening-push-override`.
- **사후 독립 검증(Fable) 6/6 PASS**: 파일 diff 라인 단위, dollar-quote 4쌍, 원장=파일 해시 바이트 일치, prod FK 정합, 부수 피해 0. 부수 발견: `docs/`가 `.gitignore`(31행)에 있어 본 로드맵·분석 문서는 git 추적 밖.

**2026-08-28 밤 — Phase 1 진행**:
- ✅ 운영 DDL 런북 작성: **`db/RUNBOOK_PROD_DDL.md`** (lock_timeout 공통 설정, CIC/DROP/INVALID 정리, pg_repack 1.5.2·`--no-kill-backend`·잔존물 정리, VACUUM FULL 창, 원장 수동 기록+CAS 동기화 절차, INVALID 탐지 스코프 한정, #18 pg_dump 절차).
- ✅ 사전 확인 2/3: schema_migrations 존재, pg_repack 가용(1.5.2, 미설치).
- ⛔ 포트 확정: SSM 조회는 사람 권한 — 런북 §0-1 명령으로 확인 후 여기 기록할 것.
- ⛔ #18 pg_dump 재생성: 유효한 직결 DSN 확보 후(런북 §8).
- ⛔ (Phase 0 이월) dev DB 패치: dev DSN 확보 대기 (사용자 결정: 전체 작업 후 일괄).

**2026-08-29 00시대 KST — Phase 2 실행** (psql 직결 = `.env.prod` Session pooler 5432, 사용자 제공·6543→5432 정정):
- 사전 최종 검증 ✅: INVALID 0건, statement_timeout 기본 2min 발견→세션 0으로 해제, 2-2 EXPLAIN 게이트(vecs 실제 `->` 술어 = Seq Scan) 통과, 회상 диф 스냅샷(상위 유저 3명 × top40, 거리+id 결정적 정렬) 사전 확보.
- 2-1 ✅ `async_jobs_scrub_idx`·`idempotency_keys_scrub_idx` CIC 생성(0.3초, 각 32kB, valid) — 실제 스크럽 CTE plain EXPLAIN에서 **양쪽 모두 인덱스 사용 확인**(Bitmap/Index Scan).
- 2-2 ✅ `DROP INDEX CONCURRENTLY vecs.moly_memories_v2_hnsw_idx`(0.16초, 드랍 직전 idx_scan=0 재확인) — **총 485MB→255MB(-230MB)**. **회상 диф 0**(전후 id 순서까지 완전 일치).
- 5-6 선적용 ✅: vecs 본체+TOAST `autovacuum_vacuum_scale_factor=0.02`(pg_class·toast reloptions 확인) — repack 전까지 TOAST 재팽창 억제 목적의 순서 앞당김(무해·보호적).
- 원장 ✅: `db/migrations/20260829_phase2_scrub_indexes_hnsw_drop.sql`(psql 직결 전용 헤더+게이트 기록) 작성 후 prod 원장 INSERT(9266f26b…).
- 2-3 ⏸ **보류**: pg_repack CLI 로컬 부재(homebrew 포뮬러 없음 — 소스 빌드 필요). 잔여 대상은 TOAST 251MB. 옵션: (a) pg_repack 1.5.2 소스 빌드 후 온라인 실행, (b) 새벽 창 VACUUM FULL(1~3분, 컨슈머 정지 필요 — EC2 접근 필요), (c) autovacuum 평형 관찰 후 재평가.
- 사후 관찰 항목: 스크럽 mean 46ms→<1ms(다음날 pg_stat_statements), 벡터 upsert p95 60s→ms대(mem0 ingest 지연), autovacuum 동작.

**2026-08-29 00시대 KST — Phase 3 DDL 실행 (#4→#10→#9)** (psql 직결, 교차검증 2렌즈 선행):
- 사전 최종 검증 ✅: 실행 정합 렌즈(결함 2건 수정 반영 — ① 세션 SET 3문을 실행 스크립트에 실문장으로 포함 ② CIC `IF NOT EXISTS`+INVALID 스킵 함정 → 사후 indisvalid 일괄 검사 추가) + 의미 보존 렌즈(결함 0 — 개별 messages DELETE 경로 코드 0곳·23503 의존 0곳, recall_diaries는 행별 표현식 평가라 HNSW/trgm 액세스 경로 불가, 탈퇴 CASCADE messages 참조 FK 9건 전수 커버 확인).
- #4 ✅ `conversation_checkpoints_through_message_id_fkey`(RESTRICT) DROP — 복합 CASCADE·profiles CASCADE 2건 잔존 확인. RESTRICT가 막던 라이브 시나리오 부재 + 탈퇴 CASCADE 트리거 순서 의존 잠복 위험 제거.
- #10 ✅ 5건 드랍: `async_jobs_provider_claim_idx`(eligible_at/provider/model/lane 참조 코드 0곳, claim EXPLAIN=claim_idx 불변), `messages_user_id_desc_idx`(sender_uq Backward IOS 대체), `chat_response_references_reply_idx`(동일 키 UNIQUE가 IOS로 대체 — 드랍 후 EXPLAIN 확인), diary HNSW 25MB+trgm 11MB(idx_scan=0+코드 확증).
- #9 ✅ 8건 CIC 생성(FK 실정의 접두사 일치, partial 2건은 RI 내부 조회가 IS NOT NULL 함의): mem0_memory_sources·mem0_ingest_candidate_sources·diary_claim_sources·greetings·async_jobs(user_id)·routine_completions·hay_transactions·registry `IN ('pending','failed')`.
- 사후 ✅: INVALID 0건(public·vecs), claim 플랜 불변, reply 조회 UNIQUE IOS 전환 확인.
- 원장 ✅: `db/migrations/20260829_phase3_index_rework.sql` 작성 후 prod INSERT(7bd59c27…).
- 관찰 후보(스코프 외): `routine_completions_user_idx`가 신규 (user_id,activity_date)에 완전 포섭되어 잉여 — 차기 정리 후보. `user_interaction_contract_items`(541행) FK 미커버는 수천 행 도달 시 재평가.
- 2-3 추가 경과: pg_repack 1.5.2 CLI **소스 빌드 완료**(로컬, 서버 가용 버전과 정확 일치). 단 `CREATE EXTENSION pg_repack`이 자동 승인 분류기에 차단되어 실행 보류 — 사용자 결정 대기.
- 사후 관찰 항목(#10 게이트 ②): claim·(user_id,id DESC) 조회 mean 감시(다음날 pg_stat_statements).

**2026-08-29 — Phase 4 코드 구현 완료** (전 스위트 1564 passed, 교차검증 2렌즈 통과):
- #23a ✅ `_touch_last_active` 제거 → `_PUBLISH` SET 병합(`:now` 파라미터 — now() 금지 준수). 의미 렌즈: last_active 읽기 3곳 전수 확인, CAS 실패 시 최종 상태 동일.
- #11 ✅ `load_price` 프로세스 캐시(TTL 5분, at 지정 시 우회, None도 캐시) + app_config 게이팅·에이전트 키 병합 1 SELECT(유저 락 이후 유지).
- #21 ✅ mem0 ingest unnest 배치(stage 2문·register 3문, RETURNING CTE 금지 준수 — 자식은 부모 조인) + privacy_cleanup 벡터 열거를 영벡터 유사도→직접 SQL(`metadata->>'user_id'` 인덱스, bounded LIMIT 유지)로 대체. asyncpg 배열 바인딩 실검증(빈 배열 포함).
- #15a ✅ sweep `_apply` 세트 기반(enqueue_many·replay_dead_many·BUMP 배치·bounded). upsert(미문서 일기)는 정상 상태 0건이라 함수 재사용 유지.
- #15b ✅ provider delete 전체 ids 1회 + 실패 시 마킹 없이 JobRetry(failed 마킹 제거 — vecs delete는 단일 트랜잭션이라 부분 성공 불가 확인).
- #28 ✅ failed→pending 회수 스캔(sweep, bounded)+재enqueue(시간 칸 dedup). 조기 반환 조건에 failed 존재 검사 포함.
- #15c ✅ recall_diaries 재작성: 본문(TOAST) 실체화를 최종 ≤limit행으로 한정, 카운트는 counts CTE(전체 기준 불변), 절단은 매치=점수순/비매치=display_date DESC 두 갈래. **prod 실측 диф 0**(유저 3명 × 주경로·매치·무매치 — id·순서·카운트 완전 일치). 유일한 의도 편차: sub-threshold 유사도 채움 행 선별이 유사도순→최신순(로드맵 명시 승인).
- #23b ✅ close_call 버퍼+30s flusher(운영 회귀 스위치 usage_close_flush_enabled) + graceful shutdown flush 3곳(API lifespan / consumer 드레인 **후** / tick 단명 프로세스 finally — 뒤 2곳은 동시성 렌즈가 잡은 결함 [상-1][중-1] 수정분). completed_at=close 시각 보존. stale-started reconciler(24h, 동종 completed 실측 최대비용 상한, 기존 ai_usage_ledger_open_idx 커버 확인). ⚠️ **prod 기존 10건 수동 triage는 분류기 차단으로 보류 — db/PENDING_MANUAL_PROD_OPS.md**.
- #16+#24 ✅ tick tz 사전 필터(파이썬 ZoneInfo 판정 — SQL AT TIME ZONE 금지 준수, distinct tz 33종/1,050명 실측 무해) + 페이징 쿼리 tz 포함 + 유휴 틱 유저 루프 스킵. notify 아침·저녁 푸시 전 세션 commit(커넥션 미보유). RC드레인·하트비트·billable은 루프 밖 불변. **보류**: diary_generation의 LLM 전 세션 close — rollback이 ORM 객체를 expire시켜 async lazy-load 사고 위험, 별도 정밀 작업으로 이월.
- #22 ⏭ 스킵: config 기본 False + prod app_config override 없음 실측(.env.example도 false). 스킵은 어느 경우든 안전.
- 관측 변화: tick counts["users"] 의미가 전체 유저→후보 tz 유저로 변경(슬랙 요약 읽을 때 참고).

**2026-08-29 — Phase 5 구현 + DDL 실행**:
- DDL ✅: `ai_usage_daily_rollup` 테이블(apply.py --env prod --commit --allow-prod, 원장 자동 기록) + 선행 인덱스 3개 CIC(`async_jobs_finished_gc_idx`·`idempotency_keys_dedupe_gc_idx`·`ai_usage_ledger_started_idx`, INVALID 0, 원장 bc8f2514…). reconciler용 인덱스는 기존 `ai_usage_ledger_open_idx`가 커버(추가 불요 판정).
- 코드 ✅: `worker/retention_jobs.py` 5종(배치당 커밋, ctid+SKIP LOCKED, MAX_BATCHES=10 + `:{seq}` 연쇄, priority=200) + tick `enqueue_daily`(KST hour>=5, `{job_type}:{KST날짜}` dedup, 월간 rc는 1일 `:{YYYY-MM}`) + /health/deep 5-6(테이블 크기·vecs 팽창비·retention 최근 성공 25h/월간 32일 임계) + openapi 30일 멱등 창 명문화(5-1 계약 변화 — **배포 전 사용자 승인 필요 항목**).
- 정정 2건: 5-2 status 실값은 `('completed','failed')`(로드맵 표기 'succeeded'는 async_jobs 용어 혼입), 삭제 술어는 KST 자정 경계의 timestamptz 상수 비교(의미 동치 + (started_at) 인덱스 sargable).
- 전 스위트 1581 passed.

**2026-08-29 — Phase 5 교차검증(로컬 PG17 실증) 결과 반영**:
- SQL 5종 전부 실증 통과: 5-2 합계 완전 보존(삭제 코호트 calls/tokens/cost 사전 합계와 롤업 일치)·멱등·가산형(중단 시점 롤업+잔존=원본)·KST 자정 경계 정확, started/unknown_usage/최근분 전량 잔존. 5-3 dead·replay 사슬·참조 원본 보존, attempts CASCADE 0. 5-4 pending 참조·planned·커서 밖 보존, `FOR UPDATE OF c SKIP LOCKED` 문법 실행 확인. 5-1/5-5 만료·processed만. 신설 인덱스 3개 Index Cond 사용 확인. dedup 키 해방(14일) 재발화 producer 전수 조사 — 없음.
- [중-1] 수정 ✅: /health/deep retention stale 판정을 async_jobs 이력→**app_config 기록 기반**으로 교체(`monitoring:retention_last_success:{job_type}`, 핸들러 5종이 성공 시 기록). 이력 기반은 5-3의 14일 GC가 월간 rc 잡 성공 증거를 지워 매달 후반 상시 503 + 배포~다음달 1일 상시 503 두 경로가 구조적이었다. 첫 실행 전(None)은 stale 비판정(미기록 고장은 dead→Slack 담당).
- [하-1] 수정 ✅: vecs_bytes_per_row에 reltuples>0 가드(ANALYZE 전 -1 → 음수 노출 방지).
- 회귀 테스트 4건 추가, 전 스위트 **1585 passed**.

**2026-08-29 — 분류기 차단분 사용자 직접 실행**:
- #23b 사전 triage ✅: stale started 10건 → unknown_usage (`UPDATE 10` — 기대값 일치).
- 2-3 pg_repack ✅: `CREATE EXTENSION pg_repack`(1.5.2) + 소스 빌드 CLI 1.5.2로 vecs.moly_memories_v2 온라인 재작성 — **255MB → 119MB**(행 14,179 보존, `-k --no-kill-backend`).
- 5-6 vecs autovacuum ✅: `20260829_phase56_vecs_autovacuum.sql` — 본체·TOAST 양쪽 scale_factor 0.02 반영 확인(pg_class reloptions), 원장 sha 2b390941…. DROP EXTENSION pg_repack까지 완료.

**2026-08-29 — 배포 전 최종 교차검증(3렌즈 병렬, 전부 통과·신규 결함 0건)**:
- 의미 보존: 대상 20개 파일 정독 — 승인 편차 3건(5-1 30일 창 / tick counts 의미 / 회상 채움 최신순) 외 의미 변화 없음. tz 게이트 밖 유지 블록 6개 라인 단위 확인, retention 술어의 서비스 데이터 불침범 재확인.
- 동시성/장애: 이전 수정 3건(wake 재생성·tick flush·드레인-flush 순서) 올바름 재확인, lifespan 타임아웃 유실은 reconciler 수렴으로 설계된 degradation.
- 정합성: DDL↔코드 완전 일치(드랍 인덱스·FK 참조 0건), openapi 30일↔코드 삼자 일치, 상점 멱등 키(만료 NULL) GC 영구 제외 확인, health↔_record_success 키 일치.
- 마이그레이션 잔재: **서비스 로직(app/·worker/)에 일회성 잔재 없음** — 오늘 코드는 전부 상시 운영 로직. db/migrations/*.sql은 원장 checksum 대조 원본이라 유지 필수. PENDING_MANUAL_PROD_OPS.md는 완료 기록으로 보관.
- 이월 관찰 2건(오늘 변경과 무관한 기존 quirk): memory_sweep result_detail 카운터가 항상 0으로 기록됨(집계 시점 문제, 관측치만 영향) / retention SQL 실행 검증은 로컬 실증 1회로 수행(CI 실PG 통합 테스트는 별도 결정).

**2026-08-29 — prod 최종 감사 + dev DB 일괄 패치 완료**:
- prod 읽기 전용 감사 10항목 전부 ✅(INVALID 0, 인덱스 13생성/6드랍 대조, 원장 해시 5/5 일치, stale started 0, repack 잔재 0, vecs 119MB·0.02, 핵심 플랜 3종, 커넥션 16/60). 관찰: unknown_usage 6,867건은 보존 대상(이상 아님, 증가 추이만 볼 것).
- PR #213 머지(CI green — lint F401 1건 수정 후). 팀 공유·트러블슈팅 문서 → ~/dev/troubleShooting/.
- dev(wywzjslvxwttxkecbyis) 일괄 패치 ✅: `.env` dev DSN 정비(빈 중복 라인·인라인 주석 제거) 후 — ① FK 핫픽스(컬럼 한정 SET NULL) ② delete_user_memories RPC+권한 ③ 원장 CAS(20260805: dev 구해시 8a366d06→e74115d5) ④ 20260829 마이그레이션 5건(CONCURRENTLY 3건 psql 직결+수동 등재, 2건 apply.py --env dev) ⑤ 사후 검증: INVALID 0, 드랍 잔존 0/생성 11/11, RESTRICT FK 제거, rollup PK 일치, reloptions 본체·TOAST 0.02, 원장 6행 로컬 해시 전부 일치.
- 관찰: 20260804_memory_cutover_guard.sql은 dev 원장에 원래 없음(dev 미적용 이력) — 행 추가 안 함(apply.py 가드와 무충돌).

## 검증 체크리스트 (각 Phase 완료 시)

1. `pg_stat_statements` 대상 쿼리 mean/calls 전후 비교 (분석 문서 §2 수치가 baseline)
2. 회상 диф 테스트: 동일 유저·동일 질의로 변경 전후 회상 결과 집합 일치 (Phase 2·4 후 **및 Phase 5-4 첫 실행 후** 필수)
3. `/health/queues` `dead_total` 불변 확인 (Phase 5-3 첫 실행 직후 — F1 회귀 감지)
4. **#10 각 드랍**: 직전 대체 인덱스 EXPLAIN + 직후 해당 쿼리 mean 감시 (Phase 3)
5. **Phase 5-2 첫 실행 직후**: 롤업 합계 = 삭제 전 원본 합계 대조(비용 보존 증명), NULL-키 중복 행 0건 확인
6. 탈퇴 E2E 1회 (Phase 3 #4·#9 후)
7. Supabase advisor 재실행 (unused index·unindexed FK 잔량 확인)
