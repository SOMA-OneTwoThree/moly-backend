# 캐피 대화·기억 아키텍처

> 상태: **현행 구현 기술서** (2026-08-06 기준, 브랜치 `fix/cutover-blockers` 코드·Dev DB 대조 완료)
>
> 이 문서는 구 `capi-memory-ARCHITECTURE.md`, `agentic-chat-ARCHITECTURE.md`,
> `agentic-chat-IMPLEMENTATION.md` 세 문서를 하나로 합치면서 **실제 코드와 다른 진술을 전부
> 코드 쪽으로 고쳐 쓴 것**이다. 문서와 코드가 어긋나면 코드가 진실이고, 이 문서를 고친다.
> 설계했으나 구현하지 않았거나 제거한 항목은 12장에 모아 명시한다.
>
> 전체 백엔드 구조는 `ARCHITECTURE.md`, 데이터 계약은 `ERD.md`, HTTP 계약은
> `openapi/openapi.yaml`이 소유한다. 이 문서는 그중 **대화 런타임·도구 루프·기억 v2·잡 플랫폼**을
> 상세히 기술한다. 기억 v2와 그 운영값은 **Dev 서버·Dev DB에서 검증 중**이며, prod에는 legacy
> 기억 구조가 없고 v2 백필·cutover도 아직 적용되지 않았다.

---

## 1. 개요와 불변식

캐피의 대화 시스템은 세 축으로 구성된다.

1. **대화 런타임** — 한 턴 안에서 제한된 읽기 도구만 호출하고, DB 쓰기와 무거운 추출·요약은
   최종 확정 트랜잭션과 비동기 잡으로 분리한다.
2. **컨텍스트·기억** — 코드 소유의 전역 페르소나, 사용자별 대화 계약, 결정적 관계 상태,
   최근 원문, 대화 요약 checkpoint, mem0 기반 장기 의미기억, 도메인 도구 조회를 권위가 다른
   계층으로 분리한다.
3. **내구 잡 플랫폼** — PostgreSQL `async_jobs` 기반 lease/fencing 큐. 결제·기억·콘텐츠·알림·
   유지보수를 논리 큐로 분리해 한 레인의 장애가 다른 레인에 전파되지 않게 한다.

"에이전틱"은 무제한 자율 루프가 아니라 **모델이 필요한 컨텍스트를 고르되 서버가 권한, 도구,
횟수, 시간, 비용, 쓰기 시점을 통제하는 bounded orchestration**이다.

깨지면 회귀인 핵심 불변식:

1. LLM·도구를 기다리는 동안 열린 DB 트랜잭션·유저 락은 0개다(SOMA-374).
2. agent phase(외부 호출 구간)의 durable write는 0개다. 유저 메시지·응답·usage·quota·멱등
   응답·후속 잡은 Phase 2 한 트랜잭션으로 확정한다.
3. 같은 `(user_id, idempotency_key)`는 최종 결과를 한 번만 만든다.
4. 모든 도구 조회는 서버가 주입한 `ToolContext.user_id` 범위 안이다. 모델 인자에 user id,
   SQL, 임의 필터를 두지 않는다.
5. **닉네임 비식별 저장** — LLM 입력에는 현재 이름을 주되 저장 직전 `naming.to_placeholder`,
   출력·재투입 시 `naming.render`. 어떤 저장 표면에도 실명 스템을 남기지 않는다.
6. egress 순서 고정(ko): 메타 프리앰블 제거 → 외래문자 결정적 제거 → 부호·물음표 정제 →
   placeholder 변환. i18n은 `i18n.resolve` 버킷(None→ko, 미지원→en)만 사용하고
   `language == "ko"` 하드코딩을 금지한다.

권위 순서(충돌 시 위가 이김):

```text
제품 안전 규칙
→ 캐피 코어 페르소나(코드 소유, 사용자 데이터로 수정 불가)
→ 현재 사용자의 명시적 발화와 published interaction contract
→ 서버가 조회한 현재 도메인 상태(장착·루틴·재화)
→ 결정적 관계 상태
→ 최근 원문
→ checkpoint 요약과 mem0 기억
→ 캐피의 과거 추측
```

도구 결과·기억·checkpoint 본문은 **untrusted data**다. 그 안의 지시는 실행하지 않으며,
`memory.sanitize_text`(NFKC + 제어문자·bidi·대괄호 제거)는 표현 정리일 뿐 보안 경계가 아니다.

---

## 2. 채팅 요청 1회의 흐름

`app/services/chat.py::post_message`. HTTP 요청-응답 완성본 하나(스트리밍·WS 없음).

### 2.1 멱등 replay

- 요청마다 `request_hash(text, greeting_id, diary_references)`를 만든다. 같은 키를 다른
  요청에 재사용하면 `IDEMPOTENCY_KEY_REUSED`(409).
- `idempotency_keys` 행이 있으면 저장된 응답을 스키마 재검증 후 그대로 반환한다. 비호환
  저장 행은 삭제하지 않고 fail-closed 500으로 보존한다(지우면 재시도가 새 턴이 되어 이중
  차감된다).
- 응답 본문 보존은 24시간(`response_expires_at`), 그 뒤 30일은 본문 없는 dedupe
  tombstone(`dedupe_expires_at`)이 같은 키의 새 턴 생성을 막고
  `IDEMPOTENCY_REPLAY_UNAVAILABLE`(409)을 돌려준다.

### 2.2 Phase 1 — snapshot (짧은 txn + 유저 advisory lock, DB 쓰기 없음)

1. `privacy.ensure_subject_active` — 삭제 장벽에 걸린 사용자는 진입 자체를 거부.
2. 유저 advisory lock(xact 범위) → `chat_turns.acquire`로 **active-turn lease** 확보
   (`chat_active_turns`: user당 1행, `turn_seq` 단조 증가, lease TTL은
   `max(15s, agent_turn_deadline_s + 10s)`). 동시 요청은 같은 키+같은 body만 replay 대상이고
   다른 요청은 conflict다.
3. gating으로 quota 확인. `tokens_remaining`이 해석 불가면 free 한도로 fail-closed.
4. 스칼라 전량 캡처(커밋 후 ORM 접근 금지): activity_date, 닉네임, 언어, 리뷰 상태 등.
5. `chat_contexts.anchor_message_id`(대화 앵커), `memory_pipeline_states`(v2 모드),
   published interaction contract 텍스트(실패 시 빈 계약으로 fail-open), v2 관계 render
   (mode=v2일 때만) 로드.
6. **v2 기억 회상 태스크를 여기서 미리 띄운다**(자체 세션·자체 커넥션). 회상은 임베딩+벡터검색
   약 490ms(dev 실측)라 직렬로 부르면 통째로 deadline에서 빠진다. Phase 1의 남은 DB 작업과
   겹쳐 돌리고, mode가 v2가 아니면 태스크는 즉시 빈 문자열로 끝난다(호출 0).
7. checkpoint 요약(킬스위치 on일 때만), agent 설정 snapshot(3.4절), focus block,
   현재 턴 컨텍스트 블록(킬스위치 on일 때만), 대화 배열 조립(`_context` — 앵커 이후 메시지 +
   현재 입력 in-memory 결합, 절대날짜 표식), greeting_id 검증·내용 로드.
8. `commit()` — lease만 남기고 락·커넥션 반납. 이후 LLM 구간 DB 점유 0.

### 2.3 외부 호출 구간 (DB 커넥션 0)

- 회상 태스크를 `asyncio.wait_for(1.5s)` 경계로 거둔다. 내부 타임아웃을 믿지 않고 경계에서
  통째로 자른다. 실패·타임아웃은 빈 기억이다.
- 시스템 프롬프트와 휘발 블록을 조립(3장)하고, agent 킬스위치·카나리를 통과하면 도구 루프
  (4장), 아니면 단발 `llm.generate` 호출. LLM per-request timeout은
  `min(llm_timeout_s(60), 남은 deadline)`.
- 외부 호출 실패는 저장 0인 클린 재시도다 — lease를 즉시 회수하고 예외를 올린다.
- egress 백스톱(ko만): `strip_leading_meta`(메타 프리앰블 제거, 발동 로그) →
  `strip_foreign_ko`(**결정적 제거** — 과거의 한자·가나 복원 LLM 재호출은 하드 데드라인·최대
  2회 호출 계약과 충돌해 제거했다) → `_clean_reply`(부호·되묻기 물음표) →
  `naming.to_placeholder`.

### 2.4 Phase 2 — finalize (짧은 txn + 유저락 재획득)

한 트랜잭션에 다음을 순서대로 확정한다.

1. `chat_turns.verify_publish` — lease token + base context revision CAS. 멱등 중복이 먼저
   확정했으면 그 응답을 반환(이중 저장 방지).
2. **grounding 재검증** — 모델이 고른 `selected_refs`/`focus_ref`를
   `chat_references.validate_selected`로 소유권·published·suppression까지 재확인. 하나라도
   무효면 grounded 부분을 조용히 제거하지 않고 **응답 전체를 locale별 안전 문구로 교체**한다.
3. 선발화 커밋 — `populate_existing=True` fresh read로 소유·미커밋을 재확인한 뒤 유저
   메시지보다 먼저 1회 insert(`turn_position=0`), `committed_message_id` 연결. 실제 커밋분만
   현재 닉네임 렌더로 응답에 echo.
4. 유저 메시지(`turn_position=1`) 저장.
5. **관계 시작 확정 + welcome 프롤로그** — `profiles.relationship_started_at/timezone/
   display_date`가 없으면 이 턴에서 확정하고,
   `diary_service.ensure_welcome_for_first_committed_turn`이 같은 트랜잭션에서 welcome 일기를
   멱등 생성한다(과거 결함으로 누락된 사용자도 같은 경로로 복구).
6. 앵커 리셋이 있으면 저장, 캐피 응답(`turn_position=2`) 저장 — 턴 내 **모든** LLM 호출의
   토큰 합계와 billable을 메시지 행에 남긴다.
7. `_record_memory_v2` — shadow/v2 사용자만 source 커서 전진 + 관계 event append + (bootstrap
   완료·커서가 따라잡은 경우) `mem0_ingest` 잡 enqueue. legacy 사용자는 no-op.
8. 앵커 리셋 턴이면 대화 요약 checkpoint 잡 enqueue(킬스위치 on일 때만).
9. 원가 가중 billable을 `user_daily_stats`에 원자 증분(RETURNING으로 응답 값 일치), 리뷰 노출
   판정, `finish_publish`(context revision 증가 + lease 해제), diary reference card 영속
   (4.3절), 멱등 응답 저장(당시 렌더값 고정), commit.

### 2.5 회계와 quota

- `TurnUsage`가 턴 내 모든 `LlmCall`(chat 또는 tool_decide·tool_final)을 합산한다.
  `turn_usage_v2_enabled=False`(롤백)면 주 호출만 차감하고 나머지는 계측만.
- 사용자 quota는 **원가 가중 billable weighted unit**이다. provider별 가중치
  (`_billable`이 model prefix로 선택):
  - OpenAI GPT-5.6: 출력 6.0 · 캐시 읽기 0.1 · 캐시 쓰기 1.25 (2026-08-03 공식 요금표,
    전 tier 입력 대비 동일 비율. 캐시 쓰기는 무료가 아니다 — 5.6 이전 기준으로 되돌리지 말 것)
  - Anthropic(dormant): 출력 5.0 · 읽기 0.1 · 쓰기 1.25
- OpenAI API는 캐시 쓰기 토큰을 보고하지 않으므로 3버킷 배타 추정을 쓴다:
  `prompt_tokens >= 1024`면 `uncached = prompt - cached` 전량을 쓰기 버킷으로(보수적 과대,
  llm.py 주석 참조).
- 일 한도: 런칭 무료기간 `free_launch_token_limit=150,000`(app_config override 가능),
  기본 free 20k / trial·subscriber 100k, 소진 경고 임계 8k, 리뷰 노출 임계 15k. 리셋은 로컬
  04:00 경계(`activity_date`).
- 회사가 지불하는 **실원가(USD)는 quota와 별개**로 `ai_usage_ledger`에 적재한다(11.2절).

---

## 3. 프롬프트 조립과 캐시 계층

### 3.1 라이브 경로의 실제 배치

라이브 챗은 `chat._build_system` + 대화 배열로 조립한다. 순서 원칙은
**stable → append-only → current**다. 매 턴 바뀌는 값을 안정 프리픽스나 최근 원문 앞에 두면
그 뒤 전체가 캐시 미스가 된다.

```text
system (안정 프리픽스, 버전/hash 변경 시에만 교체)
  1. 캐피 코어 페르소나 + 안전 규칙 + 출력 계약   (ko=CAPI_PERSONA / ja=CAPI_PERSONA_JA /
                                                    그 외=ko 본문 + 원시 BCP 47 언어 지시)
  2. published interaction contract 렌더           (6장 — 항상 주입, 없으면 빈 문자열)
  3. v2 관계 상태 locale render                    (7장 — mode=v2만)
  4. [먼저 건넨 말] 선발화·미응답 리드              (있을 때만)
  5. focus block                                   (직전 카드 참조 좌표 — 있을 때만)

대화 배열 (append-only)
  6. 앵커 이후 최근 원문 (naming.render로 현재 이름)

휘발 system 블록 — 마지막 user 메시지 직전 삽입 (current)
  7. [지난 이야기] 최신 checkpoint 요약             (킬스위치 on + 존재 시)
  8. [기억] v2 회상 블록                            (5.5절)
  9. [지금 상태 - 서버 사실] 현재 턴 컨텍스트        (킬스위치 on 시: 시각 버킷·오늘 첫 대화·
                                                    함께한 일수·장착 아이템·테마·루틴 집계)

  10. 현재 사용자 메시지
  (도구 턴이면 이후 assistant tool_calls + tool results)
```

- 실측 근거: 휘발 값을 system에 두면 요약이 발행된 턴마다 캐시읽기 0·쓰기 4,500토큰이었다.
  최근 원문 뒤에 두면 앞의 append-only가 그대로 캐시된다.
- 캐시는 OpenAI **implicit(자동 prefix) 캐시**를 쓴다. explicit breakpoint 모드는 검증 전이라
  사용하지 않는다. Anthropic 경로(dormant)는 `cache_control` breakpoint(system+마지막 메시지,
  TTL 5m). `chat_prompt_cache_enabled` 킬스위치, Anthropic 전용 캐시 미작동 경보
  (`chat_cache_min_prefix_tokens=2048` 이상인데 read=write=0).
- 프로덕션 billable 구성 실측: 캐시 읽기 65% · 출력 32% · 입력 2%(회계 정정 전) → 정정 후
  출력이 지배적. 언어별 출력 토큰 차이가 턴 수를 좌우하므로 턴 지표에 `lang` 버킷을 남긴다.

### 3.2 순서 보존 assembler (shadow 전용)

`app/services/prompt_assembly.py`는 `PromptSegment(kind, role, content)`와
`CacheClass(STABLE→APPEND_ONLY→CURRENT→INPUT→TOOL)` 순서를 byte 단위로 강제하는 직렬화기다.
**라이브 응답에는 쓰지 않는다.** `shadow_prompt_trace` 잡(`worker/shadow_trace_jobs.py`)이
대화가 끝난 뒤 같은 재료로 v2 프롬프트를 조립해 직렬화 byte·추정 token·**캐시 가능 프리픽스
비율**만 계측한다(`prompt_trace.py`). `PENDING_BRIDGE` segment kind는 분류만 정의돼 있고
생산하는 코드는 없다(12장).

### 3.3 비용 부등식 — 도구 루프 예산의 근거

일 한도를 실제로 소진하는 헤비 유저(프리픽스 약 4,640tok, 현재 42턴) 기준으로, 도구 턴이
회계 정정 후에도 턴 수 감소 20% 이내여야 한다는 제약에서 유도했다.

```text
D = agent_decide_max_tokens (1홉 출력 상한 — 함수 호출 JSON)
T = agent_tool_result_budget_tokens (한 턴 도구 결과 합계)

도구 턴 billable = 2,157(고정비) + 7.25·D + 1.25·T
   (D는 1홉 출력 6배 + 2홉 입력 1.25배 = 7.25배, T는 2홉 입력 1.25배)
-20% 경계: 턴당 4,464 이하 → 7.25·D + 1.25·T ≤ 2,307

확정값 D=192, T=600 → 2,142 (여유 165)
검산: 도구 턴 4,299 → 150,000/4,299 ≈ 34.9턴 = 42턴 대비 -17% ✅
```

`agent/config.py`가 app_config override 조합을 이 부등식으로 재검증한다. 개별 상한
(D≤214·T≤732)은 상대가 기본값일 때의 최대치라, 둘 다 통과해도 조합이 위반할 수 있다 —
위반 시 코드 기본값(192/600)으로 되돌리고 경보한다. 이 근거 위에서 **비용 목적의 도구
사용률 상한은 두지 않는다**(canary는 롤아웃 속도 조절용).

---

## 4. 도구 루프와 grounding

### 4.1 런타임 (`app/services/agent/runtime.py`)

```text
turn_deadline = monotonic() + agent_turn_deadline_s
  1. 안전 게이트 — 현재 SAFETY_CLASSIFIER=None(승인된 위기 분류기 없음, 게이트는 자리만)
  2. step 1: generate_step(tools=읽기 도구+finish_response, max_tokens=decide_max_tokens)
     ├─ finish_response 선택 → 한 번의 호출로 턴 종료
     └─ 읽기 도구 호출 ↓
  3. 남은 시간 < final_reserve_s → 도구를 시작하지 않고 step 2 직행
  4. 도구 병렬 실행(툴별 단명 read-only 세션, per-tool timeout 800ms,
     프로세스 inflight semaphore 8) — 실패·타임아웃도 ToolResult(unavailable)로 형식 완결
  5. step 2: finish_response만 노출해 최종 답변·mode·ref를 typed sidecar로 수집
```

- 킬스위치 `agent_enabled`(기본 False) + `agent_canary_pct`
  (`sha256(user_id)` 기반 0.01% 단위, 프로세스·재시작 간 안정). 꺼져 있으면 단발 경로와 동일.
- **`agent_turn_deadline_s = 8.0`.** 5.0이 아니다 — dev `ai_usage_ledger` 실측(2026-08-06)에서
  1홉 tool_decide p50 1.54s/**p90 4.11s**, 2홉 tool_final p50 1.45s였고, 5.0이면 1홉 예산이
  2.5s라 p90을 못 덮어 호출 104건 중 26건(25%)이 timeout으로 죽었다. 8.0이면 1홉 5.5s로 p90
  위에 선다. `agent_final_reserve_s=2.5`.
- 라운드 상한 1, fan-out 상한 3. 모델이 초과 호출하면 앞의 3개만 실행하되 나머지 call_id에도
  `unavailable(tool_call_limit)`을 붙여 transcript 형식을 닫는다.
- 도구 결과는 transcript 삽입 전 **턴 합계 600 token 예산으로 절단**한다(호출 순서대로 채우고
  초과분은 truncated 표시). 도구별 글자 상한은 개별 안전장치일 뿐이다.
- 제어 의도(control intent)는 **스키마에서 제거됐다** — 등록된 제어 도구가 없고(4.2절)
  적용 경로도 없으므로 모델에 광고하지 않는다(없는 능력을 약속하게 된다,
  `test_transition_side_effects`로 고정). 런타임에 남은 처리 코드는 예기치 않은 제어 호출을
  shadow 계측 후 버리는 방어 경로다(1홉 도착 시 `control_intent_ignored`).
- 설정은 Phase 1에서 `effective_agent_config`가 app_config→Settings 우선순위로 1회 조회해
  frozen snapshot으로 들고 간다(프로세스 TTL 캐시 없음 — 두 EC2 캐시 불일치 없음).

### 4.2 도구 registry (`app/services/agent/tools/registry.py`)

등록된 도구는 **`recall_diaries`·`get_routines` 둘뿐**이다. 파일이 존재하는
`search_diaries`·`get_diary`는 registry에 올라가 있지 않다(wire 스키마에도 없음).
제어 도구 `_CONTROL_TOOLS`는 **비어 있다** — `forget_memory`가 유일한 제어 도구였는데
"잊어줘"를 대화로 처리하는 것은 의미가 없다는 제품 판단으로 제거했다(2026-08-06). 2차 호출에는
`finish_response`만 노출된다.

- 도구 name/description은 ASCII 고정 영어(언어별 분기 시 프리픽스 캐시가 언어마다 쪼개짐).
  registry 순서 = wire 스키마 순서(tuple 고정).
- `recall_diaries` — 답 완결형 일기 회상. 인자: `query`(≤200자), `need`
  (`count|summary|full|full_card|quote`), `from/to`, `focus_id`, `limit`(≤5). 검색은
  `diary_recall_documents`의 embedding(1536) 코사인 유사도 + `search_text ILIKE` 부분일치를
  결합하고, `kind IN (welcome, shared_day, capi_day)`·published·미삭제만 반환한다. 한 번의
  호출로 존재·개수·coverage·발췌·전문을 함께 돌려준다(검색→GET 순차 호출 구조 금지).
- `get_routines` — 달력 날짜(현지 00:00 경계) 기준 루틴 상세. `days_of_week` ISO 1=월…7=일,
  `frequency_per_week=len(days_of_week)`(현행 API와 동일 규칙, 새 enum 금지). 최대 20건,
  이름 100자·전체 2,000자, 현재 activity date ±31일. 이름은 유저 자유 입력이 섞이는 유일한
  필드라 `i18n.localized_name` 해석 후 반드시 살균.
- `finish_response`(내부 계약) — `text`(≤4,000자), `response_mode`
  (`summary|short_quote|full_card|reopen_reference`), `selected_refs`(≤3), `focus_ref`.
  ref ID는 런타임 allowlist 검증 전에는 신뢰하지 않는다.

### 4.3 reference card·focus·연속성

- 모델에는 발췌와 ID만 주고, **Phase 2가 DB 원문으로 카드를 만든다**(모델이 전문을 재작성하지
  않는다). 공개 API는 versioned `reply.references[]`로 노출하며, 클라이언트가
  `diary-reference-v1` capability를 보낸 요청에만 카드를 싣는다.
- `chat_response_references` — diary 카드만 영속화(user·reply_message_id·diary_id 복합 FK,
  본문 비복제, `state=available|unavailable`, redaction 좌표). 삭제·비공개 시 unavailable.
- `conversation_focus` — "그거/그 일기/두 번째 거"용 담화 좌표. 실제 제시 순서(ordinal)를
  고정 저장하고, **만료는 15분 또는 +6 커밋 턴**이다. 매 사용 시 소유권·published·suppression을
  재검증한다.
- 카드 전달·목록 발췌는 읽음 처리가 아니다 — `first_read_at`은 클라이언트의 명시적 열람
  이벤트(`/diaries/{id}/read`)만 기록한다.

---

## 5. 기억 파이프라인 (mem0 v2)

### 5.1 좌표·모드·부트스트랩

- source 좌표는 **`(user_id, turn_seq)` 하나**다. `messages.turn_seq/turn_position`
  (1=user, 2=moly, 0=greeting)이 턴을 정의하고, 과거 메시지는
  `20260806_backfill_turn_seq.sql`이 시간순을 보존하며 백필했다(기존 번호는 +N으로 밀어 올리고
  참조 테이블 동반 이동 — dev 적용 완료, prod는 컬럼 신설이라 전량 신규 부여).
- `memory_pipeline_states` — 사용자별 `mode(legacy|shadow|v2)`,
  `bootstrap_status(legacy|collecting|ready)`, 커서 3종
  (`source_through_turn_seq ≥ ingest_through_turn_seq ≥ consolidated_through_turn_seq`,
  DB CHECK로 역전 금지), stage lease(`stage_token`/`lease_until`/`revision` CAS),
  `privacy_epoch`, `repair_generation`.
- shadow 진입은 한 트랜잭션에서 historical upper turn_seq를 고정하고 collecting으로 바꾼다.
  bootstrap 완료 전에는 live turn을 먼저 색인하지 않는다(커서 연속성). shadow는 기록만 하고
  응답에는 쓰지 않으며, v2 mode만 회상·관계 render를 응답에 쓴다. **v2 mode에서 legacy
  fallback은 없다**(legacy 저장소 자체가 dev에서 제거됨, 12장).
- 커서 전진은 숫자 `+1`이 아니라 source table의 `MIN(turn_seq) > cursor`다.

### 5.2 쓰기 — ingest (`worker/mem0_jobs.py`, `mem0_pipeline.py`)

성공한 chat Phase 2가 source 커서 전진과 함께 `mem0_ingest` 잡을 `memory` 큐에 enqueue한다
(커서가 따라잡은 경우에만 — 대기 중 turn이 있으면 성공 finalize가 다음 `MIN(turn_seq)` 잡을
이어 건다. dedup key가 중복 enqueue를 막는다).

```text
source turn → extractor → eligibility → planned 후보(결정 UUID)
            → batch embedding(1회) → vector upsert → registry pending
            → 같은 finalize 트랜잭션에서 mem0_consolidate enqueue
```

- **extractor**: `gpt-4.1-mini-2025-04-14`(alias가 아닌 snapshot 고정,
  `mem0-extractor-v2`, 출력 상한 700 token). self-contained turn에서 후보 JSON을 뽑고,
  user 발화 evidence span이 있는 후보만 통과시킨다(assistant 발화는 대명사 해석용
  context_only일 뿐 독립 근거가 아니다). contract 지시·실명·현재 도메인 상태·테스트 상태·
  prompt-like 지시는 제외한다.
- **planned 후보 선저장**: provider 호출 전에 `mem0_ingest_candidates`에 결정
  `provider_memory_id`(UUID)와 candidate_text·근거 span(`mem0_ingest_candidate_sources`)을
  저장한다. provider 성공 직후 crash가 나도 재시도가 extractor를 다시 부르지 않고 같은 계획을
  읽어 **같은 id로 upsert에 수렴**한다(랜덤 중복 방지).
- **embedding**: `text-embedding-3-small`(1536차원), 통과 후보 전체를 batch 1회
  (`memory_embedding_batch_size=100`, 상한 2048). usage는 원장에 기록.
- **vector upsert**: `Mem0VectorIndexAdapter`(5.6절)로 `vecs.moly_memories_v2`에 bounded
  upsert. 성공 뒤에야 registry `pending`을 쓴다 — registry에 없는 provider 결과는 검색에서
  쓰지 않으므로, 순서가 뒤집히면 판정 안 된 기억이 노출된다.
- **단계 예산**: handler 총 40초를 extract 15 / embed 5 / upsert 12 / finalize 5 / wrapper 3으로
  나누고, 남은 시간이 다음 단계 예산보다 작으면 호출을 시작하지 않고 retry한다
  (`mem0_budget.StageBudget`). 외부 호출 동안 DB 세션·advisory lock 0 — 사용자 순서는
  pipeline state의 짧은 transaction CAS(stage token/revision)로만 지킨다.
- 사용자 내부는 직렬, 사용자 간 병렬. `mode=legacy` 사용자에게는 잡이 enqueue되지 않는다.

### 5.3 consolidation — 모순·중복 판정

`mem0_consolidate` 잡(총 45초: search 6 / classify 24 / validate 4 / finalize 5 / wrapper 6).

1. 한 turn의 신규 기억 전체를 서로 간에도 비교하고, 같은 사용자의 `active|ambiguous` 기존
   기억을 semantic search해 기존 후보 최대 12개를 만든 뒤 **classifier 1회**로 신규↔신규와
   신규↔기존을 batch 판정한다(`mem0-classifier-v2`, `settings.model_utility=gpt-5.6-luna`,
   출력 상한 900 token). 판정값은 `independent | duplicate | supersedes | ambiguous`와 비교
   대상 id뿐이다 — 자유문 판정과 존재하지 않는 id는 거부한다.
2. 코드 validator가 graph를 검증한다(존재하는 id만, cycle 없음, component 모순 없음). 같은
   old를 둘 이상이 supersede하면 `(max(source_occurred_at), source_turn_seq, candidate_hash)`
   정렬의 최신 canonical 하나만 승자다. 우열 불가·invalid graph는 component 전체를 보수적으로
   `ambiguous` publish — 두 번째 LLM 호출을 추가하지 않는다.
3. registry publish(짧은 transaction CAS):
   - independent → 새 행 `active`
   - duplicate → 새 행 `duplicate` + `duplicate_of_registry_id`, provider delete `pending`
   - supersedes → 새 행 `active`, 기존 행 `superseded` + `superseded_by_registry_id`,
     기존 provider delete `pending`
   - ambiguous → 관련 행을 같은 `conflict_group_id`로 묶음
4. provider 벡터 삭제는 `mem0_provider_delete` 잡(maintenance 큐, 한 번에 limit 50)이 뒤에서
   처리한다. 삭제가 늦거나 실패해도 **검색이 semantic 상태로 먼저 거르므로** 노출은 즉시
   막힌다(저장 비용 정리일 뿐).
5. 해당 turn의 provider id가 전부 terminal 상태가 된 뒤에만 consolidated 커서를 전진한다.

**reconsolidation**(`mem0_reconsolidate`, 하루 경계마다): 일반 consolidation은 신규 후보만
판정하므로 판정 규칙이 나아져도 이미 active로 굳은 중복이 남는다(dev 실측: 같은 뜻 두 건이
둘 다 프롬프트에 들어감). 이 잡이 살아 있는 기억끼리 재판정해 중복·대체를 닫는다 — 새로
만들지 않고 상태 전이만 한다.

### 5.4 registry — 현재 유효한 기억의 판정자

`mem0_memory_registry`는 provider memory id의 수명만 기록한다(본문·임베딩 비복제).

- `semantic_status`: `pending | active | duplicate | superseded | ambiguous | excluded |
  rejected_policy`. **검색에 통과하는 것은 `active | ambiguous`뿐이다.**
- `provider_delete_state`: `kept | pending | deleted | failed`.
- identity는 `(user_id, provider, collection_version, provider_memory_id)` unique.
  `event_started_at/ended_at/precision/resolved_timezone` — 서버 temporal resolver가 검증한
  경우에만 채우는 사건 시각(발화 시각 `source_occurred_at`과 구분).
- `mem0_memory_sources` — source hydration·감사의 DB 정본(message id·UTF-8 evidence span·
  content hash·authority·confidence·extractor version). provider metadata는 복구 보조 사본일
  뿐이다.

### 5.5 읽기 — 회상 (`app/services/mem0_recall.py`)

- **결정적 planner `needs_recall`**: 별도 LLM 호출 없이 발화 자체를 본다. 되짚는 표지
  (물음표·"기억/어제/그때/전에…" 정규식)가 있거나 6자 초과면 회상, 짧은 인사·호응은 provider
  호출 자체를 생략한다. 벡터 거리로는 판정할 수 없다 — dev 실측에서 'ㅇㅇ'(0.667)이
  '내 루틴 뭐있었지?'(0.623)와 비슷하게 "가까웠고", '안녕' 한마디에 기억 8건이 프롬프트에
  들어간 사고가 있었다.
- provider에서 user filter로 **40건 overfetch** → registry에서 `active|ambiguous`만 필터
  (user_id 재검증 포함 — 벡터 저장소가 오염돼도 남의 기억이 프롬프트에 실리지 않는다) →
  거리 오름차순 정렬.
- **상대 거리 컷**: `최소 거리 + margin(0.08)` 안쪽만 남긴다(절대 임계값은 원리적으로 안
  된다 — 내용 없는 입력이 임베딩 중심 근처에 놓여 모든 것과 적당히 가깝다). 절대 상한 0.90,
  최소 5건 보장(MIN_KEEP — 같은 주제 기억이 여럿일 때 정작 찾는 것이 margin 밖으로 잘리는
  실측 사고 방지), 최종 limit 8.
- margin 값은 dev 기억 12건으로 고른 **미검증 초기값**이다. golden set(회상 정답 200건)으로
  재측정해 정할 것.
- `render_block`: ambiguous는 발생 시각과 함께 "단정하지 말고 자연스럽게 물어봐" 지시문
  (ko/en/ja 언어별)으로 렌더한다. 헤더도 언어별(`[기억]/[memory]/[記憶]`).
- **이 모듈은 예외를 올리지 않는다.** 회상 실패는 빈 목록이고 대화는 계속된다. 챗 경계에서
  1.5초 wait_for로 한 번 더 자른다.

### 5.6 벡터 저장소와 mem0 façade

- 저장소는 같은 Supabase PostgreSQL의 `vecs.moly_memories_v2`
  (id varchar / vec vector(1536) / metadata jsonb). **migration이 만들고 런타임은 만들지
  않는다** — HNSW cosine 인덱스와 `metadata->>'user_id'` 인덱스 포함.
- `Mem0VectorIndexAdapter`(`mem0_adapter.py`)는 `mem0ai==2.0.11` exact pin의 **벡터 인덱스
  계층만** 감싼다. `Memory`/`AsyncMemory`는 인스턴스를 만들지 않는다(계약 테스트로 고정):
  1. `Memory.add()`는 infer 값과 무관하게 SQLite history를 쳐서 호스트마다 결과가 갈린다.
  2. 기본 클라이언트는 제어 불가능한 SQLAlchemy 5+10 풀과 런타임 DDL
     (`create schema/extension`)을 만든다.
- engine은 주입받는 **psycopg2 동기 엔진**(pool 3+0, timeout 2s, pre-ping — vecs가 동기
  SQLAlchemy+psycopg2 전제라 asyncpg·psycopg3 불가, 실측). 모든 연산 bounded(limit·timeout,
  스레드 실행), 결과는 사용 전 `user_id` 재검증.
- `SearchHit.distance`는 **거리다(낮을수록 가깝다)** — score로 이름 지으면 내림차순 정렬해
  가장 관련 없는 기억이 실리는 실측 사고가 있어 이름으로 못 박았다.

---

## 6. 사용자별 interaction contract

"앞으로 반말해" 같은 합의가 검색 성공 여부와 무관하게 항상 지켜지도록, 계약은 회상 경로를
타지 않고 **매 턴 안정 프리픽스에 주입**된다.

### 6.1 닫힌 스키마 (`app/services/interaction_contract.py`)

목적은 **사용자 문장을 프롬프트에 넣지 않는 것**이다. raw 문장을 stable prefix에 넣으면 임의
텍스트가 매 턴 명령 위치에서 전달된다.

- `kind`: `address | response_style | comfort | topic_boundary | expression_boundary |
  relationship_definition | durable_behavior | custom_preference`
- `action`: `use | avoid | prefer | ask_before | listen_before | do_not_assume |
  honor_preference` (kind별 allowlist 조합을 코드로 검증)
- `condition`: `always | when_distressed | when_asking_advice | when_topic_tag |
  custom_trigger`, `polarity`, `target_tag`
- 자유 문자열 자리는 `target_literal` 하나뿐: NFKC 정규화·단일행·최대 64 grapheme,
  제어문자·bidi·Markdown/XML delimiter·role/tool token 금지, 서버 template의 **인용된 데이터
  슬롯**에만 escape 렌더(명령 위치 금지).
- 캐피 정체성·안전 규칙 변경 요청은 후보 자체가 되지 않는다.

### 6.2 저장과 발행

- `user_interaction_contracts` — `(user_id, locale)`별 행. 정본은 locale-neutral
  `document_json`이고 `rendered_text`는 언어별 투영이다. `(user_id, locale)`당 published
  1개를 partial unique index로 강제. `render_hash`가 같으면 새 version을 만들지 않는다.
- `user_interaction_contract_items` — typed `value_json` + `authority(explicit_user |
  confirmed | repeated_observation)` + `source_message_id`(근거 사용자 발화) + 상태·유효기간.
- 주입 시(`contract_repo.published_text`) 저장된 `rendered_text`를 그대로 쓰지 않고 **정본
  document_json에서 다시 렌더한다** — 저장분이 옛 template이면 새 렌더 규칙(인용 슬롯)의
  방어를 받지 못한다. 조회 실패는 빈 계약으로 fail-open(계약 조회가 대화를 죽이지 않는다).

### 6.3 추출 — contract compiler

`contract_compile` 잡(content 큐)이 **하루 경계마다**(activity date가 닫힐 때) 돈다.
매 턴 돌리면 같은 합의를 반복 추출하고 비용만 든다.

- compiler(v1)는 추출기가 아니라 **필터**다. 이 경로의 위험은 놓치는 것이 아니라 사용자가
  안 한 약속을 만들어 내는 것이다 — 잘못 만든 항목은 stable prefix에 실려 매 턴 행동을 바꾼다.
- 명시적 요청만 후보로 만들고, 모델 출력은 전부 닫힌 스키마를 통과해야 하며, 근거 message id는
  실제 사용자 발화여야 한다(캐피가 한 말은 근거 불가). 정체성·안전 변경 요청(정규식 차단 목록
  포함)은 후보가 되지 않는다.
- 영향이 큰 항목(경계·관계 정의)은 draft로 남기고, 낮은 항목(호칭·말투·위로 방식)만 자동
  publish한다 — 매번 "이렇게 해줄까?"로 확인하면 페르소나의 질문 절약 규칙과 충돌하고,
  사용자가 확답 없이 화제를 넘기면 합의가 사라진다.
- 현재 사용자 메시지는 저장된 계약보다 높은 권위이므로 "지금부터 반말해"는 계약 publish 전에도
  현재 답변부터 적용된다.

---

## 7. 관계 상태

자유 서술 하나로 관리하지 않는다. 정본은 세 겹이다: **event**(append-only 사실) →
**state**(결정적 집계, 단조 증가) → **render**(locale별 문장 projection).

- `relationship_events` — `normal_turn_committed`(성공 finalize마다,
  dedup `(user, turn_seq)`)와 `active_day_started`(그날 첫 성공 turn, dedup
  `(user, activity_date)`) 두 종류(DB CHECK 고정). chat Phase 2가
  `memory_pipeline.record_turn_events`로 기록한다(shadow/v2 사용자).
- `user_relationship_states` — `active_days`, `successful_turns`,
  `qualifying_turns`(**하루 최대 10턴만 stage 계산에 누적**, raw 통계는 별도 보존),
  `relationship_stage`, `stage_rule_version='relationship-v1'`, CAS용 `version`과 stable
  prefix 교체 트리거인 `prompt_revision` 분리(매 턴 바뀌는 counter가 프롬프트 캐시를 깨지
  않게).
- stage 규칙(`relationship-v1`, 순수 함수 — 같은 입력은 항상 같은 출력):

| stage | 진입 조건 |
|---|---|
| `new` | 아래 미충족 |
| `acquainted` | `active_days >= 2` AND `qualifying_turns >= 6` |
| `familiar` | `active_days >= 7` AND `qualifying_turns >= 30` |
| `close` | `active_days >= 30` AND `qualifying_turns >= 120` |

- stage는 **단조 증가**한다(GREATEST upsert — event가 늦게 도착하거나 재처리돼도 캐피가
  어제보다 덜 친해지지 않는다). 자기개방 깊이·민감 주제 발화량·기억 개수·일기 열람률·결제는
  입력으로 쓰지 않고, 미접속으로 stage를 낮추지 않는다. stage는 말투의 친숙함에만 쓰고 가격·
  보상·기능 잠금·알림 압박·관계 이용 표현에는 절대 쓰지 않는다.
- 집계·렌더는 `relationship_project` 잡(maintenance 큐, 하루 경계)이 수행한다 — 챗 경로에서
  전체 event를 집계하면 지연이 이력 길이에 비례한다. 챗은
  `relationship_profile_renders`(user·prompt_revision·locale·renderer_version
  `relationship-render-v1`)의 렌더 결과만 읽는다. projector가 아직 안 돈 사용자는 관계 블록
  없이 대화한다.
- 관계 시작 시각의 정본은 `profiles`
  (`relationship_started_at/timezone/display_date`)다 — state에 복제해 두 번째 정본을 만들지
  않는다. 첫 성공 대화의 Phase 2에서 확정하고 같은 트랜잭션에서 welcome 일기를 만든다(2.4절).
  `profiles.relationship_revision`은 관계 표시 필드가 바뀔 때만 증가한다(일반 profile 수정이
  캐시 identity를 흔들지 않게).

---

## 8. 단기기억과 checkpoint

### 8.1 앵커 append-only 최근 원문

- 앵커 리셋 트리거: 세그먼트 40메시지 또는 30,000자. 리셋 후 보존: 최근 20메시지·12,000자.
  쿼리 안전 상한 120메시지. 트리거≫보존이라 리셋 사이 여러 턴이 캐시 히트한다(매턴 슬라이드
  방지). 원문 `messages`는 리셋으로 삭제되지 않는다.

### 8.2 대화 요약 checkpoint (W11 — 구현 완료, 킬스위치 기본 off)

`conversation_checkpoints(user_id, through_message_id, summary, version, source_hash)`.

- 리셋이 일어난 턴의 Phase 2에서, 버려질 구간을 요약하는 잡을 메시지와 **같은 트랜잭션**에
  enqueue한다(content 큐). 요약은 `keep_from - 1`까지 덮고 프롬프트에는 `keep_from` 이후가
  남아 겹침도 빈틈도 없다.
- `source_hash`는 이전 checkpoint `(id, source_hash)`와 원본 메시지의 정렬된
  `(id, sender, kind, content)`를 **길이-prefix 직렬화**해 SHA-256한 결정적 값이다(길이
  prefix가 없으면 `a|bc` vs `ab|c`가 같은 해시를 낼 수 있다). handler는 claim 후 원본을 다시
  읽어 hash가 다르면 결과를 버린다.
- 요약 입력·저장 모두 placeholder 상태를 유지하고 실명 stem 회귀 검사를 통과해야 한다.
- 다음 턴은 최신 checkpoint 하나 + 이후 메시지를 쓴다. 늦거나 실패하면 앵커를 전진시키지
  않고 기존 window로 계속 답한다(fail-open).
- 누적 왜곡 계측: 매 10번째 checkpoint는 체인 요약 대신 원본으로 재검증한다(원본이 400메시지를
  넘으면 재검증을 건너뛴다 — 부분 이력을 전체인 양 요약하지 않기 위함). 10·400은 측정 후 조정.
- **Summary는 Fact가 아니다.** checkpoint에서 장기 사실 추출 잡을 만들지 않는다 — 요약을
  사실로 되먹이면 근거가 요약으로 오염된다.
- 킬스위치 `context_checkpoint_enabled`(기본 False) — off면 잡도 조회도 없다.

### 8.3 checkpoint v2 — window 체인·daily digest (shadow 전용)

`checkpoint_v2.py`는 `window`(누적 체인, `previous_checkpoint_id`, segment/coverage 범위
구분)와 `daily_digest`(activity date 하나의 독립 요약, 체인 미연결·날짜 PK 조회)를
`ready|published|superseded` 상태로 정의한다. 현재는 `shadow_checkpoint` 잡이 하루 경계마다
**`ready` 상태로 생성만** 하고 live 프롬프트에는 쓰지 않는다(활성화 CAS 미구현, 12장). 요약
자체는 기존 `checkpoint.summarize()`를 재사용해 실명 검사·마스킹 보호를 공유한다.

---

## 9. 배치 잡 플랫폼

### 9.1 `async_jobs` 계약 (`app/services/jobs.py`)

```sql
-- 핵심 컬럼: queue, job_type, user_id, dedup_key, payload, state, priority,
-- available_at, expires_at, attempt/max_attempts, lease_owner/lease_token/lease_until,
-- result_code/result_detail, last_error_*, replay_of
-- UNIQUE (job_type, dedup_key)
-- state ∈ ready | running | succeeded | dead | cancelled
-- CHECK: running이면 lease 3필드 NOT NULL, 아니면 전부 NULL
```

설계 불변식(어기면 조용히 깨진다):

1. **attempt는 claim 시점에 증가**한다(claim 트랜잭션 안). 크래시로 finalize를 못 한 잡도
   재클레임마다 카운트돼 반드시 dead에 도달한다(poison job 무한루프 방지).
2. **claim은 `ready`만** 대상으로 한다(`FOR UPDATE SKIP LOCKED`, priority→available_at→
   created_at 순). 만료 lease 회수를 claim 쿼리에 섞지 않는다.
3. **finalize·heartbeat는 fencing UPDATE**: `id + state='running' + lease_owner +
   lease_token`. 0행이면 lease를 잃은 것이므로 도메인 반영도 하지 않는다. 도메인 쓰기·후속
   잡 enqueue는 `JobResult.apply_domain`으로 fencing UPDATE와 같은 짧은 트랜잭션에서만 한다.
4. **reaper는 (1) terminal running → (2) retryable running → (3) terminal ready를 각각 별도
   트랜잭션·commit**으로 돈다(10초 주기, statement당 50행, 큐별 독립 — retryable backlog가
   terminal 전이를 굶기지 않고 그 반대도 없다).
5. terminal(succeeded/dead/cancelled)에서 같은 행을 ready로 되살리지 않는다. **dead 자동 삭제
   없음.** 운영 replay는 `dedup_key='replay:{old_job_id}:{operation_id}'`인 **새 행** +
   `replay_of` FK(계보 감사, `20260804_job_replay_lineage.sql`)로만 만든다.
6. dead 전이는 commit 후 PII 없는 구조화 로그 + Slack best-effort 경보
   (`(queue,job_type,error_code)` dedup, 창 300초). 경보 실패가 job 상태를 롤백하지 않는다.
7. retryable 실패는 equal-jitter 지수 backoff: `raw = min(60, 2·2^(attempt-1))`,
   `delay = uniform(raw/2, raw)`. 유효한 `Retry-After`가 있으면 그보다 일찍 실행하지 않는다.
8. 외부 호출 중 row lock·세션 0. finalize 전용 DB acquire에는 별도 5초 상한
   (`job_finalize_timeout_s`) — handler timeout 뒤 풀 30초를 기다리다 lease를 잃는 경로 차단.
9. `expires_at` 경과는 `cancelled`(늦은 알림을 보내지 않기 위한 상태), 스키마 검증 실패·미지원
   payload는 즉시 `dead`, 대상 삭제는 `succeeded|cancelled` + 사유 코드.

### 9.2 큐와 실행값

큐는 6종이며 별도 프로세스가 아니라 `queue` 컬럼 + consumer 내부 고정 슬롯으로 분리한다
(content가 밀려도 critical/notification 슬롯을 빌려 쓰지 않는다). Redis·Kafka·Celery 없이
PostgreSQL 큐만 쓴다.

| queue | 용도 | slots | claim | timeout | lease | heartbeat | attempts |
|---|---|---:|---:|---:|---:|---:|---:|
| `critical` | 결제(RC) | 2 | 2 | 10s | 30s | 없음 | 3 |
| `interactive_async` | 대화 후속 | 2 | 2 | 30s | 45s | 15s | 3 |
| `content` | 일기·요약·계약 | 1 | 1 | 120s | 150s | 20s | 3 |
| `memory` | 기억 색인·판정 | 2 | 2 | 120s | 180s | 30s | 3 |
| `notification` | 저녁 푸시 | 1 | 1 | 10s | 20s | 없음 | 3 |
| `maintenance` | 정리·투영·벡터 삭제 | 1 | 1 | 60s | 90s | 20s | 3 |

- `memory`는 content와 분리된 전용 lane이다 — 같은 큐면 일기 300건이 도는 동안 기억이 통째로
  밀린다(concurrency 1이라 서로를 막는다, 감사 지적).
- 이 값은 전부 **env 전용**(app_config hot override 대상 아님 — 소비자 동시성·lease는 기동값
  이라 런타임 변경 시 이미 잡힌 lease와 어긋난다)이고, 처리량 근거가 아직 없는 보수적
  초기값이다(부하 측정 후 조정).
- heartbeat 불변식: `interval <= min(lease/3, 20s)`.

### 9.3 consumer와 등록 핸들러

`python -m worker.consumer` 상주 프로세스(`worker/consumer.py`). 등록 핸들러:

| job_type | 큐 | 역할 |
|---|---|---|
| `mem0_ingest` | memory | 5.2절 추출→임베딩→upsert→registry |
| `mem0_consolidate` | memory | 5.3절 batch 판정·registry publish |
| `mem0_provider_delete` | maintenance | non-active 벡터 정리(한 번에 50) |
| `mem0_reconsolidate` | memory | active끼리 재판정(하루 1회) |
| `conversation_checkpoint` | content | 8.2절 W11 요약 |
| `contract_compile` | content | 6.3절 계약 추출 |
| `relationship_project` | maintenance | 7장 state 집계·locale render |
| `shadow_prompt_trace` | content | 3.2절 프롬프트 계측(응답 미사용) |
| `shadow_checkpoint` | content | 8.3절 window/digest ready 생성 |
| privacy 계열 | maintenance | 11.3절 bounded 삭제·two-sweep 검증 |

하루 경계(직전 turn과 activity date가 달라진 시점)에
`shadow_checkpoint(daily_digest)`·`mem0_reconsolidate`·`relationship_project`·
`contract_compile`·window checkpoint를 activity date가 dedup key에 들어간 잡으로 건다 —
매 턴 걸면 하루에 수십 번 LLM을 부른다.

미지원 job_type은 즉시 `dead(unknown_job_type)`(배포 스큐·오타 관측). 배포 시 SIGTERM →
새 claim 중단 → 짧은 잡만 grace 안에 완료 → 끝나지 않은 잡은 lease 만료로 다른 host가 회수.

### 9.4 스케줄러 — 현행 tick과 이행 상태

- **현행 프로덕션 스케줄러는 15분 크론 틱**(`python -m worker`, `worker/tick.py`)이다:
  전체 profile을 순회해 로컬 04:00 일기 생성(`diary_gen_claims`로 (유저,대상일) 30분 lease
  상호배제), 05:00 저녁 푸시 개인화 문구 사전 생성(`push_personalization`, app_config 3-상태
  롤아웃·이중 검수 fail-closed), 09:00 아침 일기 푸시(**킬스위치 off** —
  `morning_push_enabled=False`, 코드 유지), 20:00 저녁 안부 푸시(FCM 직접 발송), RevenueCat
  inbox 드레인(틱당 200건, dependency 예약 슬롯 50).
- `user_schedules`(user×kind unique, kind 4종 `daily_digest | diary_generate |
  diary_morning_notification | evening_checkin`, timezone snapshot·`next_due_at`·revision)는
  **테이블·백필·대사까지만 구현됐다.** due 인덱스 dispatcher는
  `schedule_dispatcher_enabled=False`(기본 off)이며, schedule 4종 count=활성 profile 수·중복
  0·두 경로 결과 동일이 확인되기 전에는 full-profile scan을 제거하거나 읽기 경로를 전환하지
  않는다(잘못 켜면 그 사용자만 조용히 일기·알림을 못 받는다).
- 저녁 푸시는 notification 큐로의 이관 전이며 tick이 직접 발송한다(at-most-once: 발송 marker
  선점 후 실패는 재시도하지 않고 당일 손실 수용). scheduler는 `/etc/moly-worker-host` marker가
  있는 한 EC2에서만 실행한다.

---

## 10. 데이터 모델 요약

물리 계약의 정본은 `ERD.md`다. 여기서는 소유 관계만 정리한다.

| 그룹 | 테이블 | 역할 |
|---|---|---|
| 대화 원본 | `messages` | 원문 정본. `turn_seq/turn_position` 턴 좌표. 수정·삭제 없음 |
| 대화 상태 | `chat_contexts` | 앵커, `context_revision`, `last_committed_turn_seq`, `last_active_at` |
| 직렬화 | `chat_active_turns` | user당 1행 lease(turn_seq·idempotency_key·request_hash·token) |
| 멱등 | `idempotency_keys` | 응답 24h/tombstone 30d, `request_hash`, `terminal_status`, redaction |
| 참조·연속성 | `chat_response_references` / `conversation_focus` | diary 카드 영속·담화 focus(15분/+6턴) |
| 일기 회상 | `diary_recall_documents` / `diary_claim_sources` | 검색 projection(embedding+search_text) / 일기 span의 근거 message 연결 |
| 기억 v2 | `memory_pipeline_states` / `mem0_ingest_candidates(+_sources)` / `mem0_memory_registry` / `mem0_memory_sources` | 5장 |
| 벡터 | `vecs.moly_memories_v2` | mem0 벡터 컬렉션(1536, HNSW). migration 소유 |
| 계약 | `user_interaction_contracts` / `user_interaction_contract_items` | 6장 |
| 관계 | `relationship_events` / `user_relationship_states` / `relationship_profile_renders` | 7장 |
| 요약 | `conversation_checkpoints` | W11 체인 요약(라이브) · checkpoint v2 shadow 산출물 |
| 잡 | `async_jobs` | 9장. `replay_of` 계보 |
| 스케줄 | `user_schedules` | 9.4절(읽기 경로 미전환) |
| 비용 | `ai_price_catalog` / `ai_usage_ledger` | 11.2절 |
| 삭제 | `privacy_subject_barriers` / `privacy_ledger_events` | 11.3절 |
| 기타 | `user_daily_stats` / `greetings` / `diaries` / `diary_gen_claims` | quota 누적 / 선발화 / 일기(`kind=welcome|shared_day|capi_day`) / 일기 생성 상호배제 |

`provider_backoffs`는 모델·테이블만 있고 갱신·조회 경로가 아직 없다(12장).

---

## 11. 운영

### 11.1 타임아웃·예산 총괄

| 항목 | 값 | 근거·비고 |
|---|---:|---|
| chat end-to-end deadline | 8.0s | dev 실측 p90 기반(4.1절). app_config override 가능 |
| final reserve | 2.5s | 2홉 p50 1.45s |
| per-tool timeout | 800ms | |
| tool inflight(프로세스) | 8 | 측정 후 조정 |
| decide 출력 상한 | 192 tok | 부등식 3.3절 |
| tool result 턴 합계 | 600 tok | 부등식 3.3절 |
| v2 회상 경계 | 1.5s | 챗 wait_for |
| LLM per-request | min(60s, 남은 deadline) | |
| mem0 ingest | 40s = 15/5/12/5/3 | extract/embed/upsert/finalize/wrapper |
| mem0 consolidation | 45s = 6/24/4/5/6 | search/classify/validate/finalize/wrapper |
| context summary | 75s = 55/5/5/10 | model/verify/finalize/wrapper |
| job backoff | base 2s · cap 60s | equal-jitter |
| reaper | 10s 주기 · 50행 | 최단 lease(20s)보다 짧게 |
| finalize DB acquire | 5s | lease 상실 경로 차단 |

### 11.2 비용 회계 (`ai_price_catalog` / `ai_usage_ledger`)

- **사용자 quota와 회사 원가는 다른 값이다.** quota는 `chat._billable`의 weighted unit,
  원장은 provider 단가 기반 실원가(USD). 백그라운드 장애가 대화 quota를 갉아먹는 경로를
  만들지 않는다.
- 단가는 코드 상수가 아니라 **effective-dated catalog**(micro-USD per 1M tokens, 변경은 새
  version 행 추가로만). 원장 행은 `price_catalog_version`을 저장해 가격 변경 뒤에도 과거
  비용을 재현한다.
- 호출 전 `started` 기록 → 완료 시 usage/비용 fenced update. 응답을 잃은 호출은 0원으로
  숨기지 않고 `unknown_usage` + 단가 기반 상한 추정으로 보존한다. 상태:
  `started | completed | unknown_usage | failed`. lane `foreground | background`, purpose별
  (chat/tool_decide/tool_final/mem0 extract·embed·classify/checkpoint/diary 등) 귀속.
- 필요한 단가가 NULL인데 토큰이 0이 아니면 비용을 확정하지 않고 None(공짜 집계 사고 방지).
- cache-write는 provider가 주지 않는 추정값이며 표시를 남긴다 — "정확한 실비"라고 부르지
  않는다. 원장 기록 실패가 대화·잡을 깨뜨리지 않는다(best-effort).

### 11.3 계정 삭제와 프라이버시

인증 계정 삭제 자체는 moly-auth 소유다. moly-backend는 삭제 장벽과 파생 데이터 정리를 맡는다.

- `privacy_subject_barriers` — `state(active | deleting | deleted)` + **epoch**(삭제 사이클
  세대. 삭제 시작마다 +1 — 이전 epoch의 pending/running 잡은 authorize에서 걸린다).
  `privacy_barrier_mode`: `compat`(행 없으면 허용 — backfill 중) / `enforced`(행 없으면
  fail-closed). active 행 backfill·count 검증 두 sweep 통과 후에만 enforced로 올린다.
  순서 주의: 현행 코드가 "행 존재=차단"으로 읽던 시기의 사고를 막기 위해 (a) 컬럼 additive →
  (b) status-aware 코드 → (c) active 행 backfill → (d) enforced 전환 순서를 지켰다.
- `begin_subject_deletion`은 장벽을 `deleting`으로 세우면서 같은 문에서 즉시 비식별화한다:
  멱등 응답 payload NULL·`terminal_status='redacted'`, reference 카드 unavailable·metadata
  제거, `async_jobs` payload 제거·ready 잡 cancel.
- 실제 삭제는 coordinator 잡이 **bounded**(`delete_by_user(limit)` 후 continuation 잡),
  **two-sweep**(늦게 도착한 쓰기를 잡기 위해 연속 두 번 0이어야 완료), **멱등**으로 수행한다.
  완료 후 `mark_subject_deleted`.
- 챗 진입(`ensure_subject_active`)·worker publish·푸시 개인화 생성이 모두 장벽을 본다.

### 11.4 킬스위치와 관측

| 킬스위치 | 기본 | 효과 |
|---|---|---|
| `agent_enabled` + `agent_canary_pct` | False / 0 | 도구 루프. off면 단발 경로와 동일 |
| `context_checkpoint_enabled` | False | W11 요약(잡·조회 모두 없음) |
| `current_turn_context_enabled` (+`current_context_last_active_enabled`) | False | 현재 턴 컨텍스트 블록 |
| `chat_prompt_cache_enabled` | True | breakpoint 캐싱(Anthropic 경로) |
| `turn_usage_v2_enabled` | True | 턴 내 전 호출 합산 차감(off=주 호출만, 롤백 경로) |
| `schedule_dispatcher_enabled` | False | user_schedules due dispatcher(현재 tick 유지) |
| `morning_push_enabled` | False | 아침 일기 푸시 |
| `privacy_barrier_mode` | compat | 장벽 fail-closed 전환 |
| `push_personalization_rollout` (app_config) | off | 저녁 푸시 개인화(off/allowlist/all) |

관측:

- 턴 구조화 로그 1줄(유저 id·본문 미기록): `phase1_ms / context_ms / llm_ms / repair_ms /
  egress_ms / phase2_ms / total_ms / prompt·cache_read·cache_write tokens /
  cache_read_ratio / billable / lang 버킷 / used_tools / replay`. 언어별 분리 집계가 계약이다
  (메모리 `dialogue-eval-lang-split`).
- 잡: 큐별 ready/running/dead·oldest age(`/health` 노출), dead Slack 경보, `job_telemetry`.
- `worker_last_success` 하나로 전체를 정상 판정하지 않는다 — deadman은 "기대된 작업이 기한
  안에 수렴했다"를 검사한다.

---

## 12. 설계했으나 구현하지 않았거나 제거한 것

문서로만 존재하던 설계와 현재 코드의 차이. 새로 읽는 사람은 이 목록의 항목을 **현재 시스템에
없는 것**으로 읽어야 한다.

**제품 판단으로 제거:**

- **대화형 망각 전체** — `forget_memory` 제어 도구, `/memory/forget` API, forget marker·
  suppression·closure 체계. "잊어줘"를 대화로 처리하는 것은 의미가 없다는 제품 판단
  (2026-08-06). registry의 `_CONTROL_TOOLS`는 비어 있고, 계정 삭제만 완결적으로 지원한다.
  `finish_response`의 `control_intents(forget|pin)` 필드도 스키마에서 함께 제거됐다 —
  적용 경로 없는 능력을 모델에 광고하지 않는다(테스트로 고정).
- **legacy 정규화 기억 구조** — `memory_facts/evidence/insights(+sources)`,
  `memory_source_turns(+messages)/closures`, `memory_forget_markers`,
  `memory_recall_suppressions/suppression_operations/episodic_messages`,
  `relationship_profiles(+sources)` 13종과 이관 다리 `legacy_recall_tombstones`는 dev에서
  **삭제됐다**(`20260806_drop_legacy_memory.sql`, `20260806_drop_legacy_tombstones.sql`).
  legacy 읽기 경로도 없다. 구 문서의 W8~W10(정규화 fact/forget/cutover) 계약은 이
  구조와 함께 폐기됐고, 의미 장기기억은 mem0 v2(5장) 하나다.
- **한자·가나 복원 LLM 재호출**(`_repair_foreign_ko`) — 하드 데드라인·최대 2회 호출 계약과
  충돌해 결정적 제거(`strip_foreign_ko`)로 대체.

**설계만 있고 구현되지 않음(코드에 근거 없음):**

- `recall_timeline` 원문 조회 도구, `pending_bridge` 주입(segment 분류만 정의),
  checkpoint v2의 pending anchor·activate CAS(`chat_contexts`에 관련 컬럼 없음 — v2는 shadow
  `ready` 생성까지만).
- `user_interaction_contract_renders`/`user_interaction_contract_item_sources` 분리 테이블 —
  실제는 `(user_id, locale)`별 계약 행 + item의 `source_message_id` 단일 컬럼.
- `user_relationship_state_renders`의 profile_relationship_revision 복합 키 설계 — 실제는
  `relationship_profile_renders`(user·prompt_revision·locale·renderer_version).
- 9-queue 분리(`memory_ingest/memory_consolidation/interaction_profile/context_summary/
  diary/privacy` 등) — 실제는 6-queue(9.2절).
- `async_jobs`의 provider/model/lane/`eligible_at` 라우팅 컬럼과 `provider_backoffs` 공유
  backoff 배선, `ai_rate_windows` 분당 예약 — 미구현(`provider_backoffs`는 테이블만 존재).
- `user_schedules` due dispatcher 읽기 경로 전환(9.4절 — 기본 off), notification 큐를 통한
  푸시 발송(현재 tick 직접 발송).
- explicit prompt cache 모드·breakpoint 검증(현재 OpenAI implicit만),
  15,000 token 프롬프트 hard cap과 블록별 tokenizer 예산 강제(현재는 문자 상한과 도구 예산만),
  `memory-golden-v1` 200케이스 golden set·`dev-load-v1` 부하 manifest(계획 단계).
- 구 문서의 활성화 게이트·cutover 절차(cohort mode 전환, dual-write rollback soak 등)는
  설계 검토 이력이다. 실제 dev는 전 사용자 v2 전환·legacy DROP까지 완료했고, prod 적용
  절차는 별도로 다시 정의해야 한다(prod에는 v2 테이블·백필이 없다).

**설계값과 다르게 확정된 값:**

- `agent_turn_deadline_s` 5.0 → **8.0**(실측 근거 4.1절).
- mem0 회상: overfetch 25→**40**, 목표 K 5→**limit 8·MIN_KEEP 5**, score threshold 대신
  **상대 거리 margin 0.08 + 절대 상한 0.90**.
- focus 만료 24시간/20턴 → **15분/+6턴**.
- 일 한도 150k 유지 확정(2026-08-03), 캐시 읽기 가중 0.5→0.1·쓰기 0→1.25 반영 완료.
