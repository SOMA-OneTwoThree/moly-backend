# 캐피 대화 시스템 설계

> 상태(2026-08-04): 대화 중심 회상 구조의 코드·Dev DB·Dev 서버 구현과 독립 재감사를 완료했다.
> 단계적 cohort/mode 전환과 이전 저장소 관련
> 문단은 설계 검토 이력이며 런타임 계약이 아니다. 규범 설계는 이 문서 §0, 정확한 구현 범위와 완료
> 게이트는 `agentic-chat-IMPLEMENTATION.md` §0.7이 소유한다.
> 짝 문서: `agentic-chat-IMPLEMENTATION.md` — 이 설계를 파일·함수·DDL·테스트 단위로 옮긴 구현 명세.
> 이 문서는 **왜 이렇게 만드는가**를, 짝 문서는 **무엇을 어떻게 만드는가**를 담는다.
> 범위: 대화, 도구, 캐릭터 페르소나, 유저 관계 모델, 장·단기 기억, 일기, 루틴,
> 착용 아이템, 테마, 비동기 잡, 배치, 워커, 회계, 안전, 관측, 마이그레이션.
>
> **2026-08-04 대화 중심 회상 재설계:** 아래 `0장`은 구현된 규범 설계다. 코드·DDL의 정확한
> 대응과 완료 게이트는 `agentic-chat-IMPLEMENTATION.md` §0.7을 따른다. 뒤쪽의 v1 도구명이나
> 과거 전환안이 `0장`과 충돌하면 `0장`을 우선한다.

---

## 0. 대화 중심 회상 규범 설계

### 0.1 제품 원칙

캐피는 검색 명령을 기다리는 비서가 아니라 유저와 관계를 이어가는 대화 상대다. 유저는
“2026년 7월 29일 일기를 조회해 줘”라고 말할 필요가 없다. “우리 처음 만났을 때 어땠어?”,
“전에 내가 힘들다고 했던 거 기억나?”, “나에 대해 쓴 거 또 있어?”, “내가 씌워 준 거 마음에
들어?”처럼 말한다. 런타임은 답의 진실성이 저장 데이터에 달렸을 때만 필요한 근거를 조용히
가져오고, 캐피는 검색·DB·도구·메타데이터를 언급하지 않은 채 관계의 말로 답한다.

반대로 감정 공감, 의견, 장난처럼 저장 사실 없이도 정직하게 답할 수 있는 잡담은 조회하지 않는다.
자연스러움은 조회를 숨기는 것뿐 아니라 불필요한 조회를 하지 않는 것까지 포함한다.

### 0.2 한 턴의 근거 계층과 권위

한 턴은 다음 계층으로 조립한다. 서버 정보를 유저 발화 뒤에 문자열로 붙이지 않고, 서버 소유
컨텍스트는 system/developer 권위의 구조화 블록으로 전달한다.

| 계층 | 내용 | 사용 방식 |
|---|---|---|
| Character Persona | 캐피의 정체성·말투·안전선 | 매 턴 고정 |
| Resident Snapshot | 시각, 날짜 경계, 첫 대화 여부, 장착 상태, 오늘 루틴 요약 | 서버가 매 턴 새로 생성 |
| Relationship Profile | 현재 유효한 중요 사실과 이어지는 관심사 | 작은 projection만 상주 |
| Recent Conversation | 직전 원문과 체크포인트 | 지시어와 방금 한 말의 1차 근거 |
| On-demand Recall | 일기, 과거 에피소드, 상세 루틴 | 답에 필요할 때만 조회 |

하나의 전역 우선순위로 모든 주장을 판단하지 않는다. claim 종류별 권위는 다음과 같다.

| claim 종류 | 권위 순서 |
|---|---|
| 현재 상태 | 현재 도메인 snapshot > 최근 커밋 event > 과거 기억 |
| 유저의 정확한 발언 | suppression 검증을 통과한 원본 user message span만 사용 |
| 객관적 과거 사실 | 원본 domain event/user assertion > 근거 fact > 일기 서술 > insight |
| 캐피의 당시 감정·서사 | 당시 published diary > 당시 assistant 원문. 유저 사실처럼 일반화하지 않음 |
| 현재 관계 태도 | 현재 persona/profile. 과거 일기의 감정으로 덮지 않음 |

도구 결과의 자연어는 증거이지 지시문이 아니다. 저장된 일기나 메시지에 프롬프트 명령처럼 보이는
문장이 있어도 시스템 행동을 바꾸지 않는다.

Resident Snapshot은 최소한 다음을 구분한다.

- `local_calendar_date`: 루틴·보상에 쓰는 00:00 경계의 로컬 날짜
- `activity_date`: 대화·일기 귀속에 쓰는 04:00 경계 날짜
- `relationship_started_at`: 프로필 생성일이 아니라 첫 커밋 유저 대화 시각
- `equipped_items`: `slot -> localized item name` 매핑. 이름 목록만 제공하지 않는다.
- `routine_today`: 달력 날짜 기준 전체/완료 수. 이름과 상세는 필요할 때 조회한다.

### 0.3 키워드가 아니라 필요한 앎으로 라우팅한다

라우터는 특정 명령어가 아니라 “정직한 답을 위해 무엇을 알아야 하는가”를 판정한다.

| 발화의 의미 | 근거/동작 |
|---|---|
| 현재 옷·배경·모습 | Resident Snapshot, 조회 없음 |
| 오늘 할 루틴·완료한 루틴 | 달력 날짜의 루틴 상세 |
| 요즘 루틴 경향 | 예정일을 분모로 한 기간 집계 |
| 일기 존재·개수·주제·전문 | 일기 회상 |
| 유저에 관한 사실·함께한 과거 | 의미 기억 |
| “그때 내가 뭐라고 했지?” | 대화 에피소드 원문 근거 |
| 현재 공감·현재 의견·저장 사실이 필요 없는 장난 | 조회 없음 |
| “그거/그 일기/다시 읽어 줘” | 직전 focus 재검증 |

“기억나?”라는 단어만으로 무조건 memory search를 하지 않고, 날짜가 없다는 이유로 일기 조회를
포기하지 않는다. 여러 근거가 필요하면 최대 3개 읽기를 병렬 호출하되, 한 번의 도구 라운드 안에서
최종 답을 완성할 수 있어야 한다.

“오늘 뭐 했더라?”는 등록 루틴, 일반 활동, 오늘 대화 중 어느 것인지 최근 담화로 판단하고 불명확하면
자연스럽게 좁힌다. “우리 처음 만났을 때 어땠어?”도 저장소 이름이 아니라 유저가 원하는 답 형태
(캐피의 느낌, 실제 사건, 일기 열람)를 먼저 분류한다. 부정·가정·인용·비꼼이라는 문법 표지만으로
조회 여부를 정하지 않고 바깥 발화행위가 실제 과거 확인을 요구하는지 판단한다. “설마 기억 못 하는
건 아니지?”는 회상 요청일 수 있고, 단순 인용 예시는 아닐 수 있다. 과거 사실을 전제로 한 감정
질문도 recall 대상이며 현재 공감과 구분한다.

### 0.4 답 완결형 읽기 인터페이스

현행 `search_diaries -> get_diary` 순차 호출은 한 라운드 제한과 충돌한다. 목표 인터페이스는 검색
절차를 노출하지 않는 답 완결형 capability다.

#### 일기 회상 `recall_diaries`

입력은 자연어 주제/시간 힌트, 내부 필요 유형(`existence|count|list|recall|read`), 직전 focus다.
출력은 다음을 보장한다.

- `scope_total`, `deterministic_filter_total`, `returned_count`, `semantic_candidate_count`를 구분한다.
  날짜·종류·공개 상태·생성 시 확정한 `about` 태그처럼 결정적 필터만 exact라고 말한다. 자유 주제
  의미 검색의 후보 수는 threshold·index coverage가 붙은 추정 집합이며 “정확히 N편”이라고 말하지
  않는다. count와 목록은 같은 DB snapshot에서 계산한다.
- 각 결과에 user-scoped ID, 종류(`welcome|shared_day|capi_day`), 제목, 실제 사건 시각 또는 표시 날짜,
  짧은 발췌, 본문 완전성, 선택 ID를 둔다.
- `author=capi`, `primary_subject`, 다중 `about_tags[user|shared_day|capi_life]`, `generation_source`를
  분리한다. 하나의 일기가 유저와 함께한 하루를 동시에 다룰 수 있다. legacy 미분류 행은 exact tag
  집계에서 제외하고 coverage를 함께 반환한다. 현행 API의
  `personal`이라는 이름만 보고 “유저가 쓴 일기”라고 말하지 않는다. 대화 기반 일기도 저자는 캐피다.
- “나에 대해 쓴 일기”는 닉네임 문자열이 아니라 생성 시 확정한 `about=user` 태그로 푼다.
  `{유저이름}`은 현행 개인정보 계약대로 출력 경계에서 현재 닉네임으로 렌더하며, 이는 현재 호칭일
  뿐 당시 정확한 호칭 인용이라고 말하지 않는다.
- published 상태인 본인 일기만 반환하고 집계도 같은 권한·공개 필터를 쓴다.
- 메타데이터/시간과 lexical 검색을 우선하고, 필요할 때 같은 PostgreSQL pgvector 의미 검색을
  결합한다. 외부 벡터 DB를 새 진실 소스로 두지 않는다.

전문 요청은 3줄 캐릭터 응답 및 600-token 도구 결과 제한과 공존해야 한다. 채팅 응답에는 캐피의
짧은 말과 함께 서버가 DB 원문으로 만든 **구조화 diary reference/card**를 싣는다. 모델이 전문을
재작성하지 않는다. 카드에는 diary ID, 제목, 본문, 날짜 의미를 담고 클라이언트가 대화 안에서 펼쳐
보인다. 이 계약을 채택하지 않으면 3줄 제한과 payload 예산을 함께 완화해야 하므로 어중간하게
구현하지 않는다.

카드는 유저가 명시적으로 전문/원문 열람을 원할 때만 붙인다. 내용 질문에는 자연스러운 요약이나
검증된 짧은 인용을 사용해 매 대화를 조회 UI로 만들지 않는다. response mode는
`summary|short_quote|full_card|reopen_reference`를 구분하고, 카드 미지원 client는 일기 상세 화면으로
가는 접근 가능한 reference를 표시한다. 3줄은 일반 캐피 발화의 기본값이고 목록·복합 응답·안전
응답에는 응답 형태별 별도 상한을 적용한다.

존재·개수·목록·발췌와 서버의 card delivery는 `first_read_at`을 바꾸지 않는다. 응답에 포함됐다는
사실은 `delivered_at`이고 실제 `first_read_at`은 클라이언트가 카드를 펼치거나 상세를 연 명시적
멱등 event로 기록한다. 네트워크 단절·렌더 실패를 읽음으로 오인하지 않는다.

“몇 편 썼어?”는 기본적으로 welcome과 daily를 종류별로 나누어 답하고 total의 포함 범위를 밝힌다.
“또 있어?”는 현재 유저에게 보여 준 ordered result set의 항목을 제외한다. 자유 의미 주제라면 정확한
총계가 아닌 추가로 찾은 후보라고 표현한다. 사용자가 묻지 않은 날짜·ID·종류는 대화에 열거하지 않는다.

#### 기억 회상 `recall_memory`

기억은 서로 다른 질문을 위한 두 projection을 가진다.

- semantic fact: 취향·관계·계획처럼 재사용되는 정규화 사실. 유효성, supersession, 근거를 가진다.
- episodic turn index: “그때”, “내가 뭐라고 했지”를 위한 원본 대화 턴 검색 projection. 원본
  message가 진실 소스이고 index는 재생성할 수 있다.

결과에는 유형, 사건 시각, confidence, 근거 turn ID와 짧은 excerpt, 완전성을 둔다. exact wording에는
원문이 있을 때만 인용하고 fact 요약을 실제 발언처럼 말하지 않는다. 검색 0건은 “없었다”는 뜻이
아니므로 “지금은 선명하게 떠오르지 않아”처럼 회상 한계를 표현한다.

episodic index에도 forgotten/closure/generation 필터를 강제한다. fact를 잊게 한 뒤 원문 검색으로
다시 노출하는 우회로를 허용하지 않는다. 민감 기억은 질문과 직접 관련될 때만 쓰며, 상주 프로필이나
선발화에서 불쑥 꺼내지 않는다.

authoritative record는 `messages/diaries/domain events`이고, fact/evidence edge/insight/profile/
checkpoint/embedding/episodic index는 모두 재생성 가능한 projection이다. episodic projection은 최소한
`user_id, message_id, source_watermark, source_hash, sender, source_span/claim_segment, index_version,
embedding_version, suppression_state`를 가진다. exact wording은 index text를 인용하지 않고 원본 user
message span의 hash·소유권·suppression을 재검증한 뒤 읽는다.

fact의 독립 근거는 inbound user assertion span만 허용한다. assistant/greeting은 해석 문맥으로는 쓸
수 있지만 독립 evidence나 강화 근거가 될 수 없다. 캐피의 환각은 이후 기억으로 자기강화되지 않으며,
유저가 뒤에서 명시적으로 확인했을 때만 그 user span이 새 evidence가 된다.

각 fact provenance는 source role/span, extractor·prompt·normalizer version, observed time/event time,
confidence 근거를 가진다. 복수 assertion으로 만든 fact는 모든 supporting user span을 연결한다.
relationship profile은 검증 불가능한 완성 문자열이 아니라 item별 source ID가 있는 `document_json`으로
보관한다. 매 턴 active/suppression을 재검증하고 trusted renderer로 조립하며 실제 render hash와
`memory_generation`을 prompt cache key에 포함한다.

#### 루틴과 현재 모습

- 루틴 상세는 `local_calendar_date`를 기본값으로 쓴다. 00:00~03:59에 `activity_date`를 쓰면 어제
  루틴을 오늘 것으로 말하게 된다.
- 추세는 오늘 목록이 아니라 기간별 예정 횟수, 완료 횟수, 요일 스케줄을 집계한다.
- 현재 모습은 slot 정보를 사용해 모자와 목도리를 뒤바꾸지 않는다.
- “내가 씌워 준 것”처럼 행위자를 말하려면 equipment event의 actor/provenance가 있어야 한다. 현재
  snapshot만 있으면 “지금 쓰고 있는 것”까지만 말하고 유저가 장착했다고 확인하지 않는다.
- 장착·구매는 명시적 기존 API가 진실 소스이며 채팅이 추측으로 바꾸지 않는다.

### 0.5 첫 만남 일기는 일일 슬롯이 아닌 관계 프롤로그다

첫 만남 일기는 `profile.created_at - 1 day`로 만든 가짜 과거 일기가 아니다.

1. `relationship_started_at`, 당시 timezone, 고정 `display_date`를 첫 커밋 유저 턴의 Phase B에서
   `COALESCE`/CAS로 한 번만 확정한다. 이후 timezone 변경이 역사적 날짜를 바꾸지 않는다.
2. 같은 Phase B 트랜잭션에서 welcome 레코드와 사실 중립적인 deterministic 본문을 **동기 삽입**하고
   `published_at`을 확정해 즉시 공개 상태로 만든다. 첫 응답 커밋 뒤 published-only reader에서 반드시
   한 건이 보이는 것을 불변식으로 둔다.
   배포 중간 상태나 과거 결함으로 `relationship_started_at IS NOT NULL`인데 welcome이 없다면 다음
   안전한 chat Phase B가 같은 unique key로 멱등 생성한다. 목록 GET은 계속 side effect가 없으며,
   별도 backfill도 같은 생성 함수를 사용한다.
   비동기 잡 예약만으로 존재를 보장하지 않는다. 본문은 “오늘 처음 대화를 나눴다”처럼 커밋된
   사실만 사용하고, 유저가 실제로 하지 않은 말·감정을 넣지 않는다. 생성형 확장은 별도 제품 결정
   전에는 published 본문을 사후 변경하지 않는다.
3. `one welcome per user`와 `one daily diary per user/activity_date`를 별도 uniqueness로 보장한다.
4. `occurred_at`, `published_at`, `display_date`를 구분하고 충돌 회피 날짜를 역사로 말하지 않는다.
5. welcome을 preset/moly에 합치지 않는다. 존재·정확 개수·제목·전문을 일관되게 답해야 한다.
6. `kind=welcome|shared_day|capi_day`을 source와 별도 둔다. 생성 없음/실패 상태와 tombstone은 diary
   콘텐츠 kind에 넣지 않고 job/deletion 상태가 소유한다. `one welcome per user`와
   `one daily per user/activity_date` partial uniqueness, `(display_date,id)` pagination cursor,
   kind-aware date reader/daily 존재 판정을 함께 전환한다. 새 uniqueness만 먼저 켜지 않는다.
7. legacy welcome은 존재하던 `diary_date`를 안정된 display date로 보존하고
   `occurred_timezone_provenance=unknown`으로 표시한다. 현재 timezone을 과거 값처럼 backfill하지 않고
   정확한 현지 시각도 말하지 않는다. 알려진 고정 welcome template hash는 사실 중립 본문으로
   version migration하고, 알 수 없는 변형은 `legacy_unverified`로 두어 모델 회상에서 제외하며 raw
   record 열람만 허용한다.

### 0.6 대화 연속성: focus와 grounding

도구 결과를 다음 턴에 통째로 저장하지 않고 Phase B에서 응답과 함께 짧은 domain별 담화 focus를
저장한다. 일기 자체, 본문 속 감정/문장, 루틴, 아이템을 하나의 generic focus로 덮지 않는다. 결과가
여러 건이면 유저에게 실제 제시된 순서를 고정한 ordered result snapshot을 두어 “두 번째 거”가
재검색 rerank로 바뀌지 않게 한다.

- `domain`, `entity_type`, user-scoped `entity_id`, `facet`, 검증 가능한 `source_span_ref/claim_ref`, `ordinal`,
  `resolved_at`, `expires_at`, `memory_generation`, `context_revision`
- 원문 본문은 저장하지 않는다.
- “그거”, “그 일기”, “다시 읽어 줘”, “아니 그건 아니야”에서 사용한다.
- 매 턴 소유권, 공개/삭제/forgotten 상태와 generation을 다시 확인한다.
- 새 명시 대상이나 만료 시 교체하며, 모호하면 지어내지 않고 자연스럽게 좁힌다.

final model hop은 텍스트만 반환하지 않고 typed control sidecar의 `grounded_refs[]`, 각 claim의
`claim_ref`, `primary_focus_ref`, `response_mode`를 반환한다. 서버는 도구가 반환한 trusted candidate
ID와 대조하고 Phase B에서 소유권·공개·삭제 및 §0.7의 mode별 suppression/access policy를 다시 확인한
reference만 커밋한다.
모델용 축약 텍스트와 서버용 ID sidecar를 분리해 payload 절단으로 ID가 사라지지 않게 한다.

final output은 `claims[{claim_id,text,refs}]` 구조로 받고 결정적 assembler가 최종 문자열을 만든다.
grounded claim에는 후속 LLM egress repair를 금지한다. deterministic clean/language validation 뒤 claim
경계와 ref가 유지되지 않으면 해당 claim을 버리고 locale별 안전한 fallback으로 바꾼다. 저장되는 최종
문자열 기준으로 grounding 정합성을 마지막 검증한다.

동시 대화는 서버가 user별 `active_turn` lease와 monotonic `turn_seq`를 Phase A에서 CAS 예약한다.
다른 non-idempotent 요청은 기존 턴 종료까지 queue하거나 재시도 가능한 conflict로 반환한다. stale
context로 만든 응답 전체를 커밋하는 선택지는 허용하지 않는다. Phase B는 같은 lease/turn_seq를
검증하고 commit 뒤 해제하며 timeout은 lease reclaim한다. `context_revision/base_focus_version` CAS도
방어적으로 유지한다. client 단일 in-flight만 안전성 경계로 믿지 않는다.

운영 디버깅용 `turn_grounding`에는 선택 route, source entity ID, 성공/없음/timeout/truncated, 버전만
기록하고 본문은 남기지 않는다. 이로써 근거 없이 안다고 한 경우와 근거가 있는데 조회하지 않은
경우를 replay할 수 있다.

이전 답변에 “있는데?”, “아까는 안다며?”, “왜 또 달라?”라고 이의를 제기하면 `prior_answer_challenge`
경로로 분류한다. 직전 grounding의 상태와 원 근거를 재검사하고 적절한 capability를 다시 선택하며,
검증 전에는 유저가 암시한 제목·내용을 새로운 사실로 채택하지 않는다. “아니 그건 아니야”도
대상 선택 거부, 질문 해석 수정, 표현 선호, 실제 사실 정정을 먼저 구분한 뒤 마지막 경우만 정정
후보를 만든다.

재검사로 이전 답이 틀렸음이 확인되면 캐피는 오류를 짧게 인정하고 정확한 내용으로 바로잡는다.
개발 서버·검색 실패 같은 시스템 핑계를 대거나, 틀린 이유를 새로 지어내지 않는다.

### 0.7 기억 쓰기·수정·망각

대화 커밋 직후 원본 message는 저장되고 fact/episode projection은 비동기로 갱신한다. 방금 한 말은
projection 전에도 Recent Conversation으로 답한다. 기억 유형별 수명을 다르게 둔다.

- 안정 프로필 사실: 명시적 변경 전까지 유효
- 현재 상태형 사실: 새 사실로 supersede하거나 만료
- 함께한 에피소드: 상주하지 않고 필요할 때 검색
- 순간 감정: 낮은 중요도와 짧은 수명
- insight: fact보다 낮은 권위, 근거가 사라지면 invalidation

“아니, 그건 아니야”는 §0.6의 구분을 먼저 통과한다. 실제 사실 정정으로 판정되고 대상 근거가
확정된 경우에만 현재 focus에 연결해 정정 후보로 처리한다. 다음 의도는 구분한다.

- “그 사실 잊어 줘”: 지식 projection과 검색 노출을 닫는 forget
- “그 얘기는 먼저 꺼내지 마”: 보존하되 proactive surfacing을 금지하는 preference
- “그 대화/일기를 삭제해 줘”: 원본 도메인 레코드 삭제 정책

“그건 됐어”처럼 범위가 모호한 말만으로 영구 삭제하지 않는다. 쓰기 의도는 Phase B의 결정적
정책기가 권한·대상·멱등성을 확인한다. “물 마셨어”와 “물 마실게”도 구분하며, 대화형 루틴 쓰기를
활성화하기 전에는 기존 API만 상태를 바꾼다.

forget operation은 `cut_watermark`와 `future_learning=allow|block`을 가진다. 기본 “지금까지 잊어”는
cut 이하만 닫고 이후 새로 명시한 user assertion은 다시 기억할 수 있다. “앞으로도 기억하지 마”만
predicate의 미래 학습을 차단한다. 하나의 메시지에 여러 사실이 있으면 검증 가능한 claim segment/span
단위로 suppression한다. span을 확정할 수 없으면 privacy 우선으로 해당 메시지 전체를 회상에서 닫고
collateral forgetting을 감사한다.

망각 suppression은 fact, profile, checkpoint, episodic search뿐 아니라 일기의 주제 검색·발췌에도
적용한다. 단, 유저가 특정 diary ID/목록 항목을 명시적으로 열어 원본 기록을 보는 것은 “캐피의 기억”과
구분한다. 이 경로는 본문을 모델에 넣지 않고 raw card로 제공하며, 기억 망각은 원본 일기/대화 삭제가
아님을 제품 계약에 표시한다. 원본까지 없애려면 별도 record deletion을 사용한다.

새 일기 생성은 `diary_claim_sources(diary_id, diary_span, user_id, message_id, source_span,
source_hash)`로 user assertion provenance를 남긴다. suppression은 이 edge로 관련 diary span을
검색/발췌에서 가린다. provenance가 없는 legacy 일기는 같은 activity day에 suppressed source가 하나라도
있으면 privacy 우선으로 일기 전체를 모델 회상·주제 검색 후보에서 제외하고, 명시적 raw open만 허용한다.
suppression이 생기면 기존 full-body diary embedding을 즉시 generation-invalid로 만들어 semantic 후보에서
제외한다. provenance가 있는 일기는 suppressed span을 제거한 projection을 새 generation으로 re-embed하고,
동일한 redacted text로 lexical search projection도 다시 만든다. recall query는 원본 body의 GIN/trgm이나
구 vector를 직접 조회하지 않고 suppression generation이 일치한 projection만 사용한다. 재생성 완료
전에는 metadata-only 결과만 쓰며, legacy 무근거 일기는 redacted 재색인하지 않는다.

상태는 `recall_suppressed`와 `record_hidden/deleted`로 분리한다. 주제 검색·모델 excerpt에는 전자를
포함한 suppression gate를 적용한다. 명시적 raw record open은 후자가 아닌지와 ownership/published만
검증하고 recall suppression을 접근 거부로 쓰지 않는다. 따라서 Phase B는 recall mode에는 suppression,
raw-open mode에는 record access policy를 적용한다.

forget 직후 모델용 Recent Conversation도 affected span을 redacted view로 바꾼다. span이 불명확하면
해당 메시지 전체를 모델 transcript에서 제외한다. UI 원본 채팅과 모델 입력용 transcript view를
분리하고, 같은 트랜잭션에서 anchor/checkpoint/context generation을 무효화한다. 다음 턴은 suppression
적용 후 새 prefix로 조립하며 이전 generation의 app/provider prompt cache key를 재사용하지 않는다.
해당 user span/fact를 참조한 과거 assistant `claim_ref/grounded_ref`도 전이 suppression한다. grounding
도입 전 legacy assistant 발언은 연관성을 안전하게 증명할 수 없으면 해당 turn 전체를 모델 view에서
제외한다. UI history 원본은 별도 record deletion 전까지 유지한다.

정정·망각·새 추출의 순서는 처리 완료 시각이 아니라 source watermark/event order로 결정한다.
correction target, 새 user evidence, supersession reason을 한 트랜잭션으로 기록하고, 늦은 job은
generation뿐 아니라 target revision을 재검증해야 finalize할 수 있다.

### 0.8 실패 시에도 거짓말하지 않는다

| 상태 | 금지 | 허용 방식 |
|---|---|---|
| timeout/unavailable | “없어”, “쓴 적 없어” | 지금 바로 떠올리지 못했다는 한계 표현 |
| zero result | 존재하지 않았다고 단정 | 기억이 선명하지 않다고 표현 |
| truncated | 반환 페이지를 전체로 말함 | exact aggregate 범위만 전체 주장 |
| 근거 충돌 | 낮은 권위로 현재 상태 덮기 | 권위 순서 적용, 필요하면 불확실성 표현 |
| projection 지연 | 방금 한 말을 잊음 | 최근 커밋 원문 사용 |
| 삭제/forgotten focus | 이전 캐시로 재노출 | focus 무효화 |

최종 프롬프트에는 저장 사실이 필요하면 검색 명령 없이도 읽고, capability를 대화에서 언급하지
않으며, 일반 감정 대화에는 과조회하지 않고, 불완전 결과 이상의 주장을 하지 않는다고 명시한다.
복합 발화에서 한 capability만 실패하면 성공한 부분은 답하고 실패한 부분만 한계를 표현한다.
zero result, 일시 장애, 질문 모호성은 같은 건망증 문구를 반복하지 않고 원인에 맞는 자연스러운
복구 방식을 사용한다.

### 0.9 성능·비용·보안 계약

API 전체 동기 LLM 호출은 최대 2회, 도구 라운드 1회, 병렬 도구 최대 3개다. grounded 응답 뒤 별도
LLM repair는 호출하지 않는다. HTTP 수신 시 절대 deadline을 만들고 Phase B용 최소 500ms reserve가
남지 않으면 새 외부 호출을 시작하지 않는다. 이미 시작한 Phase B commit은 cancellation shield로
원자 완료한다. **5초는 hard cancellation이 아니라 API end-to-end p95 SLO**이며, stage별 timeout은
그 안에서 강제한다. deadline 부족 시 locale별 안전 fallback을 사용한다.

의도별 결과 예산은 count/existence는 작은 aggregate, list는 상위 3건+결정적 total, recall은 근거
있는 상위 3건, 전문은 구조화 카드로 둔다. query embedding은 DB session을 열기 전에 제한된 외부
단계에서 수행한다. metadata/lexical fast path와 의미 path의 선택 조건·각 timeout·미색인 fallback을
분리하고 tool remaining deadline을 embedding에도 적용한다. diary vector는 원본이 아니라
watermark/version을 가진 재생성 projection이다. 800ms/5초 수치는 실제 end-to-end dev replay p95로
검증한다.

#### versioned chat reference와 멱등·삭제

현행 text-only 응답에 바로 card를 쓰지 않는다. 먼저 additive `reply.references[]` schema와
`grounded_refs` 저장 구조를 모든 reader에 배포하고, 이후 writer를 켠다. reference는
`reply_message_id`에 연결해 POST뿐 아니라 GET history와 멱등 replay에도 남는다.

도구는 모델에 excerpt와 ID만 주고, Phase B가 mode별 access policy를 재검증한 뒤 DB 원문으로 card를
만든다. 재검증에 실패한 grounded claim은 결정적 assembler가 제거하고 fallback으로 바꾼다.

`chat_message_references`는 target ID, schema version, 당시 rendered non-sensitive metadata,
`redacted_at/reason`만 보존하고 body를 복제하지 않는다. target 삭제 트랜잭션은 reference를 먼저
redacted로 바꾸고 민감 metadata를 지운 뒤 target FK를 `ON DELETE SET NULL`로 전환한다. GET history는
사라지는 대신 versioned `unavailable` reference를 반환한다. projection/evidence edge는 원본 삭제 시
`ON DELETE CASCADE`, focus는 삭제, grounding target은 SET NULL+redaction한다.

client는 versioned API header/capability(`diary-reference-v1`)를 보낸다. capability가 없는 구 client에는
reference writer를 켜지 않고 짧은 대화 요약과 기존 일기 상세 deep link만 반환한다. app version 추측만
으로 card 지원을 가정하지 않는다.

idempotency response snapshot은 24시간 보존하며 그 안에서는 정상 상태의 최초 schema version과 렌더를
byte-identical replay한다. record/account deletion과 privacy redaction만 동일성보다 우선하고, 같은
트랜잭션에서 민감도와 무관하게 모든 suppressed claim/reference가 든 snapshot을 redacted version으로
바꾼다. idempotency row는 `reply_message_id/reference_id`의 직접 index를 가져 JSONB scan 없이 찾는다.
record/account 삭제 성공 응답 전 같은 트랜잭션에서 logical serving barrier, reference redaction,
idempotency payload redaction을 완료하며 물리 삭제만 24시간 안에 비동기로 한다.

24시간 뒤 body snapshot은 삭제하되 request hash, original message ID, terminal/redacted status의 비민감
dedupe tombstone은 30일 보존한다. 24시간 이후 같은 key는 새 턴으로 처리하지 않고 명시적 expired/
terminal 결과를 반환한다. 공개 API에 24시간 full-replay와 30일 duplicate-prevention window를 적는다.
active-turn lease도 `idempotency_key/request_hash`를 묶어 동일 재시도와 다른 요청을 구분한다. 메시지
history reference는 body를
diary에서 현재 권한으로 재구성하므로 닉네임 변경 시 현재
호칭으로 렌더될 수 있고, 삭제/비공개 시 unavailable이 된다.

배포/rollback은 다음 순서를 고정한다.

1. 구/신 idempotency response를 모두 읽고 `references=[]`를 허용하는 server/client 배포
2. reference/grounding table과 GET read path 배포, writer는 off
3. welcome legacy backfill(알 수 있는 occurred 값, timezone provenance, 안정 display date, kind,
   about tags, known-template neutralization)과 pagination reader 배포. v1 date cursor는 한 페이지의
   경계 날짜에 속한 모든 row를 함께 반환해 같은 날짜 welcome+daily를 건너뛰지 않는다. v2는
   `(display_date,id)` opaque cursor를 사용한다.
4. 구 lazy welcome writer 제거 및 daily predicate를 kind-aware로 전환
5. 누락 welcome 수렴 backfill을 먼저 실행하고, expanded diary reader를 rollback 최저 버전으로 고정한
   뒤 welcome writer는 client capability와
   무관하게 전 신규 첫 턴에 활성화한다. v1 diary API에는 내부 `kind=welcome`을 기존 `type=moly`로
   호환 매핑하고 v2에서 명시적 kind를 노출한다. card reference writer만 capability가 확인된 client에
   활성화한다.
6. 관찰 기간과 redacted replay/rollback 테스트 통과 뒤에만 old contract 제거

- 모든 조회·집계·focus 재검증은 서버 주입 user ID와 RLS/동등 predicate를 함께 쓴다.
- `messages`와 각 entity에 `(user_id,id)` unique를 두고 evidence, episode, typed focus/grounding reference는
  `(user_id,source_id)`와 `(user_id,target_id)` 복합 FK로 tenant 일치를 DB에서 강제한다. polymorphic
  reference는 타입별 reference table 또는 동일 수준의 결정적 validator를 사용한다. service role의
  RLS 우회를 고려해 모든 SQL tenant predicate와 DB FK를 함께 검증한다.
- 모델 인자에 user ID, SQL, unrestricted filter를 두지 않는다.
- user-derived 자유문을 system instruction과 같은 문자열에 붙이지 않는다. system profile에는 enum,
  boolean, bounded numeric처럼 자유문이 아닌 allowlist 값만 넣는다. predicate 값이 자유 문자열이면
  길이·타입을 검증한 뒤 별도 untrusted structured-data 영역의 quoted value로 전달한다. 원문/발췌도
  untrusted-data envelope/tool role에 두며 instruction으로 실행하지 않는다.
- grounding 로그에 본문, 민감 fact, diary body를 남기지 않는다.
- 응답은 현재 프로필 언어, 전문 카드는 원문을 보존한다.
- 닉네임 placeholder는 출력 경계에서 렌더하고 자기지칭 검색은 의미로 정규화한다.
- credential/secret/token은 projection·embedding을 금지한다. 정확 위치, 건강, 성생활, 종교, 재정,
  법률, 가정폭력 등 고위험 범주는 별도 sensitivity tag와 저장/검색 정책을 가지며 profile, greeting,
  push, diary generation, 선제 언급에서 제외한다. `do_not_surface` preference는 모든 출력 경로에
  동일하게 적용한다.
- “나에 대해 뭘 기억해?” 같은 broad request는 고위험 기억 공개 동의로 보지 않는다. 낮은 민감도의
  대표 기억만 답하고 민감 범위는 유저가 직접 좁혔을 때 별도 정책으로 다룬다.

#### 보존·삭제 정책

| 표면 | 기본 보존 | forget | record/account deletion |
|---|---|---|---|
| messages/diaries | 유저 삭제 또는 계정 존속까지 | 원본 유지, 모델 view 즉시 suppression | primary 24시간 내 삭제 |
| fact/episode/profile/checkpoint | 유효성/정책 수명까지. 순간 감정 30일 | 응답 확정 txn부터 비노출 | primary 24시간 내 삭제 |
| focus | 최대 24시간 또는 20 committed turns | 관련 ref 즉시 무효화 | 즉시 삭제 |
| grounding | 본문 없이 30일 | ref redaction | 24시간 내 ref redaction/삭제 |
| idempotency response | body 24시간, 비민감 dedupe tombstone 30일 | 민감도와 무관하게 suppressed claim/reference 즉시 redaction | 성공 응답 전 logical redaction, 물리 payload 24시간 내 삭제 |
| 본문 없는 운영 로그 | 14일 | 본문 자체를 기록하지 않음 | 기간 만료 |
| encrypted backup | 최대 30일 | 온라인 suppression ledger 유지 | 자연 만료, 복원 전 deletion ledger 재적용 |

provider에는 승인된 no-training/보존 계약과 삭제·cache 조건을 확인한 설정만 사용한다. provider가
개별 삭제를 보장하지 못하는 보존 기간은 사용자 정책에 그대로 명시하고 그보다 강한 삭제를 약속하지
않는다. 삭제 barrier가 시작되면 실행 중 외부 호출을 가능한 범위에서 취소하고, 취소 불가여도 결과
finalize/publish를 금지한다. backup 복원은 deletion/suppression ledger를 재적용한 뒤에만 serving한다.
이 ledger는 최장 backup 30일 + 복원 검증 여유 15일보다 긴 최소 45일간 append-only로 보존하고,
복원 대상 backup과 독립된 최신 durable 저장소에 별도 복제한다. 복원 시 ledger high-watermark가 복원
직전 기대값 이상인지 검증하며 불일치하면 serving을 fail-closed한다. account deletion tombstone은 모든
복구본 만료와 ledger 재적용 검증이 끝났음이 증명되기 전에는 제거하지 않는다.

### 0.10 검증 행렬과 활성화 문턱

실제 실패 대화(첫 만남 일기가 있는데 모른다고 답하고, 유저 정정을 근거 없이 받아들여 내용을
지어낸 사례)를 첫 golden case로 고정한다. 다음을 paraphrase까지 포함해 replay한다.

| 영역 | 필수 시나리오 |
|---|---|
| 첫 만남 | 목록 미열람 신규 유저, 첫 턴 직후, 동시 첫 요청, 첫날 daily 공존, 생성 실패 없음, 존재/제목/개수/전문/“그거”, timezone 변경 |
| 일반 일기 | 주제·대략 시점·“나에 대해”·공개일, exact count, 0건, truncated, 전문 카드 |
| 기억 | 안정 사실, 함께한 순간, 정확 발언, 방금 한 말, 정정과 늦은 job, 0건, forget cut 뒤 재진술, 한 메시지 일부 망각, assistant 환각 미확인 |
| 루틴 | 오늘 이름/완료, 기간 추세, 00:00~03:59 경계, 완료/미래 표현 |
| 착용 | 직접 질문, “내가 씌운 거”, 복수 slot, 장착 직후 snapshot 순서 |
| 연속성 | “그거/그 일기/두 번째 거”, 결과 순서 고정, 본문 facet, 새 대상, focus 만료·삭제·망각, 두 기기 동시 요청 |
| 실패 | timeout, unavailable, partial/truncated, 근거 충돌, 비동기 지연 |
| 언어/이름 | ko/ja/en, 닉네임 변경, placeholder welcome 검색 |
| 안전/보안 | evidence/episode/focus/grounding 타 유저 FK, 저장 원문·fact·diary prompt injection, 민감 기억 선제 노출, 탈퇴-worker 경합, 삭제 후 멱등 replay |
| 자연스러움 | 날짜·검색 명령 없는 paraphrase, 부정·가정·인용·비꼼·오타·언어 혼용, 잡담 미호출, 복합 발화 부분 실패, 이전 오답 challenge |
| 카드/읽음 | summary와 전문 구분, 미지원 client, 전달 후 미오픈·렌더 실패, 명시 open event, GET history/rollback |

DB/코드 불변식으로 강제하는 타 유저 노출, suppression 우회, 삭제 후 cache 재노출은 허용 0건이다.
확률적 모델 품질은 유한 replay의 “0건”을 전체 입력의 증명처럼 말하지 않고 corpus 크기, paraphrase,
seed/repeat 수, 신뢰구간과 문턱을 기록한다. 현재 상태 오답, 실패를 부재로 단정, 일기·정확 발언 조작은
이 품질 문턱의 critical failure로 별도 집계한다. route recall/precision, grounded answer 비율, 메타 언어
없는 자연스러움, follow-up 해결률, p95 latency/비용도 함께 측정한다. shadow가 online 추가 호출인지
offline replay인지 명시하고 provider 전송·비용 범위를 승인한다.

shadow replay(route만 기록) → dev golden/E2E → 제한 canary 순서로 올린다. 첫 만남 lifecycle,
answer-complete 일기+결정적 count, versioned response reference, server-owned context+slot-aware appearance,
typed grounding/focus CAS, suppression의 모든 회상 경로 차단, tenant DB 무결성, 삭제/idempotency 계약,
보안 불변식과 모델 품질 문턱 및 p95 예산을 모두 통과하기 전에는 활성화하지 않는다.

### 0.11 독립 검증 상태

PostgreSQL 원본과 재생성 가능한 pgvector projection 중심 구조는 타당하며 mem0나 별도 벡터 DB를
진실 소스로 유지할 필요는 없다. 그러나 현행 fact 검색만으로는 자연스러운 대화가 완성되지 않는다.
**답 완결형 일기 회상, 원문 근거 episodic recall, follow-up focus, 시간 경계 분리, 첫 만남 프롤로그,
망각의 전 경로 차단**을 한 작업 범위에서 구현·검증해야 한다. 일부만 켜면 캐피가 더 자연스럽게
틀릴 수 있다.

2026-08-04 사용자/컴패니언 UX, 코드 런타임, DB·개인정보·tenant, 공개 API·Dev 배포의 독립 적대 검토를
다시 수행했다. 목표 방향은 승인됐지만 현행 코드에는 다음 활성화 blocker가 확인됐다: 별도 recall
suppression 부재, stale 동시 turn, 응답 입구 밖 deadline과 추가 repair 호출, grounding/focus/reference
부재, GET lazy welcome·단일 일기 날짜 슬롯, user-only evidence의 DB 강제 부재, 만료 없는 멱등/job
payload, diary worker fencing과 Dev project-ref 검증 부재. 따라서 과거의 “PASS/새 blocker 없음” 표시는
철회한다. 수정된 목표 계약은 이 장과 구현 명세 §0.7이며, 실제 코드·Dev DB·golden E2E가 활성화 문턱을
전부 통과한 뒤에만 구현 PASS를 기록한다.

---

## 1. 결론

새 구조는 **대화 런타임**, **컨텍스트·메모리**, **내구 잡 플랫폼**의 세 축으로 만든다.

1. 대화 런타임은 한 턴 안에서 필요한 읽기 도구만 제한적으로 호출한다. DB 쓰기와 무거운
   추출·요약은 최종 응답 확정 트랜잭션과 비동기 잡으로 분리한다.
2. 캐피의 고정 성격인 **캐릭터 페르소나**와 유저마다 달라지는 **관계 프로필**을 서로 다른
   모델로 관리한다. 둘을 모두 “페르소나”라고 부르지 않는다.
3. 장기기억은 원문, 관찰 사실, 파생 통찰, 관계 프로필을 분리한다. LLM의 추측을 사실과 섞지
   않고 모든 파생 데이터에 근거와 버전을 둔다.
4. 15분마다 모든 일을 한 순서로 실행하는 전역 틱은 폐기한다. 스케줄러는 멱등 잡만 만들고,
   결제·일기·저녁 푸시·메모리·정리는 논리적으로 분리된 queue에서 lease 기반으로 소비한다.
   두 EC2 모두 consumer를 실행하고, scheduler timer는 현재 `/etc/moly-worker-host`가 있는 한 EC2에
   유지한다. scheduler 중복이나 재실행도 DB unique key로 안전하게 만든다.
5. 모든 외부 호출은 DB 트랜잭션 밖에서 실행한다. 모든 상태 변경은 짧은 트랜잭션으로 확정한다.
   기억·요약처럼 중복·누락을 허용하는 후속 작업은 원본 변경 트랜잭션에서 `async_jobs`에 직접
   enqueue하고, outbox는 결제 inbox와 탈퇴 삭제처럼 전달 이력 자체를 감사해야 하는 경로에만 쓴다.

이 구조에서 “에이전틱”은 무제한 자율 루프가 아니다. **모델이 필요한 컨텍스트를 고르되 서버가
권한, 도구, 횟수, 시간, 비용, 쓰기 시점을 통제하는 bounded orchestration**이다.

---

## 2. 설계 결정과 승계 계약

### 2.1 유지할 좋은 결정

- 작고 안정적인 정보는 상주시키고 큰 정보는 필요할 때 조회한다.
- 대화 중 긴 DB 트랜잭션이나 유저 락을 잡지 않는다.
- 도구의 `user_id`는 모델 인자가 아니라 서버가 주입한다.
- 기억 쓰기·요약·반추는 응답 뒤로 보낸다.
- 작업은 멱등 키, 재시도, dead 상태, 관측 가능성을 가진다.
- 일기는 정확한 한 시각이 아니라 lookback으로 누락에 수렴한다.
- 기능은 shadow → canary → 확대 순으로 롤아웃한다.

### 2.2 교체해야 할 결정

| 검토 과정에서 나왔던 안 | 문제 | 이 설계의 결정 |
|---|---|---|
| 전역 15분 틱 + 전역 lease | 한 레인의 지연·강제종료가 결제, 일기, 푸시, 기억 전체에 전파된다. 처리량도 가장 느린 레인에 묶인다. | 스케줄러와 큐별 워커를 분리한다. 전역 lease 없이 잡 단위 claim을 사용한다. |
| `os._exit()`로 13분에 종료 | finally, telemetry flush, 정상 종료를 건너뛰며 같은 프로세스의 다른 일을 함께 죽인다. | 잡 lease 만료와 supervisor의 graceful shutdown을 사용한다. 취소 불가능한 라이브러리는 별도 프로세스로 격리한다. |
| 결제 inbox를 일기와 같은 틱에서 처리 | 결제가 AI 작업 부하와 장애의 영향을 받는다. | 결제 전용 소비자를 둔다. 결제는 최고 우선순위가 아니라 별도 실패 도메인이다. |
| `worker_runs` 단일행 | 단일행이 전 시스템의 직렬 병목이자 장애 지점이다. | `async_jobs`의 행별 lease와 `FOR UPDATE SKIP LOCKED`를 사용한다. |
| `tools.py` 한 파일에 모든 도구 | 도구가 늘수록 카탈로그, 스키마, 실행, 도메인 접근이 다시 한 파일에 뭉친다. | Tool 인터페이스 + 도구별 모듈 + registry로 나눈다. |
| “핫패스는 읽기만” | 실제로는 mem0/LLM 같은 외부 I/O를 하며 최종 확정 시 DB도 쓴다. 검증 불가능한 표현이다. | “LLM·도구 구간의 durable write 0, 열린 DB 트랜잭션 0”을 불변식으로 정의한다. |
| 관계 코어에 사실과 반추를 함께 저장 | 관찰된 사실과 모델이 추론한 성향의 신뢰도가 섞인다. 자기증폭 방지 규칙도 저장 모델이 아니라 프롬프트 규칙에 의존한다. | evidence, fact, insight, projection을 별도 타입과 provenance로 관리한다. |
| `user_facts`를 bi-temporal이라고 정의 | `valid_from/to`만으로는 시스템 기록 시간까지 표현하는 bi-temporal이 아니다. 구현 복잡도에 비해 필요한 질의가 불명확하다. | 우선 event time + immutable revision으로 구현한다. 과거 시점 재현이 제품 요구로 확인될 때 system time을 추가한다. |
| 실패한 도구를 버리고 “도구 없이 답변” | 첫 모델 응답이 tool call뿐이면 사용할 정상 답변이 없다. 실패 뒤 호출도 예산이 없을 수 있다. | 최종 답변용 최소 예산을 처음부터 예약한다. 각 tool call에는 성공/실패 result를 반드시 붙인다. |
| `remember`를 쓰기 도구처럼 노출 | 자동 추출과 역할이 중복되고 “대화 중 쓰기 없음”과 의미가 충돌한다. | 모델은 `memory_intent`만 제안한다. durable write는 턴 확정과 함께 job으로 직접 enqueue한다. UX 활성화는 제품 결정 뒤다. |
| 일기 없음도 `diaries(source='none')`로 표현 | 사용자 콘텐츠와 처리 상태가 한 테이블에 섞인다. | 콘텐츠는 `diaries`, 생성 상태와 generation별 결과는 `async_jobs`의 diary result로 분리한다. |
| 푸시를 시간대별 프로필 스캔으로 결정 | 현재 제품 알림은 저녁 안부뿐이며 정확한 발송 창을 놓친 뒤 보내면 어색하다. | 아침 일기 푸시는 제거하고 저녁 20:00 안부만 만료시각을 가진 job으로 처리한다. |

### 2.3 반드시 별도 검토할 안전 정책

“기관·상담전화 안내는 하지 않는다”, “헷갈리면 위기가 아니라고 본다” 같은 문구는 아키텍처
결정으로 고정할 수 없다. 이는 캐릭터 톤보다 상위인 안전·제품 정책이며, 오탐과 미탐의 비용을
포함한 별도 승인과 회귀 평가가 필요하다. 이 설계는 다음 구조만 확정한다.

- 현재 발화 기반 안전 분류는 검색 기억보다 먼저 수행한다.
- 과거 기억이나 도구 결과는 현재 위기 판정을 새로 만들 수 없고 참고 신호로만 사용한다.
- 안전 정책이 발동해도 캐피의 말투는 가능한 범위에서 유지하되, 안전 정책이 페르소나보다 우선한다.
- 정책 버전, 분류 결과, 선택한 응답 경로를 민감 본문 없이 감사 가능하게 기록한다.

### 2.4 반드시 승계할 현행 계약

이것은 전면 교체안이 아니다. 다음은 지금 코드가 이미 확보한 것이고, 새 모듈 경계 안으로 그대로 옮긴다.

- 현행 egress 체인과 닉네임 placeholder 저장/렌더 계약은 §6.6의 순서와 저장 표면까지 그대로 유지한다.
- 기억·요약 작업은 Phase B에서 `async_jobs`로 직접 enqueue한다. outbox→dispatcher를 한 단계 더 두지 않는다.
- Relationship Profile은 `locale`을 키와 렌더 입력에 포함해 ko/ja/en 렌더가 섞이지 않게 한다.
- 알림은 저녁 20:00 안부만 유지한다. 일기의 오전 09:00 공개 시각은 알림 기능과 별개다.
- 현행 SOMA-374의 read-only Phase 1 → 커밋 → 외부 I/O → 재락 Phase 2 상태머신을 유지한다.

---

## 3. 용어와 책임

| 용어 | 뜻 | 소유 주체 |
|---|---|---|
| Character Persona | 모든 유저에게 동일한 캐피의 정체성, 세계관, 말투, 금지선 | 버전 관리된 프롬프트 자산 |
| User Profile | 닉네임, 언어, 시간대 등 계정의 명시적 정보 | account/profile 도메인 |
| Relationship Profile | 캐피가 이 유저를 대하는 거리감, 최근 관심사, 중요 사실의 작은 projection | memory 도메인 |
| Turn Context | 이번 응답에만 필요한 시간대, 첫 대화 여부, 장착 상태, 도구 결과 | agent context builder |
| Evidence | 실제 메시지나 명시적 유저 데이터. 파생 기억의 근거 | 원본 도메인 테이블 |
| Fact | evidence에서 추출한 현재/과거 사실 | memory 도메인 |
| Insight | 여러 fact로부터 추론한 경향. 사실보다 낮은 권위 | memory 도메인 |
| Summary | 대화 구간의 손실 압축본 | conversation projection |
| Tool | 대화 런타임이 필요할 때 호출하는 bounded capability | agent 도메인 |
| Job | 응답과 분리해 재시도 가능한 내구 작업 | job platform |

핵심 규칙은 다음과 같다.

- Character Persona는 유저 데이터로 수정하지 않는다.
- Relationship Profile은 캐릭터 정체성을 수정하지 않는다.
- Fact와 Insight는 같은 필드나 같은 우선순위로 프롬프트에 넣지 않는다.
- 원본 메시지는 기억 projection 실패 여부와 무관하게 진실 소스로 남는다.

---

## 4. 전체 아키텍처

```text
                       ┌─────────────────────────┐
HTTP POST /chat ──────▶│ ConversationApplication │
                       └────────────┬────────────┘
                                    │
             ┌──────────────────────┼───────────────────────┐
             │ Phase A              │ Agent phase           │ Phase B
             ▼                      ▼                       ▼
      TurnRepository         AgentRuntime             TurnRepository
      ContextSnapshot        ├─ SafetyRouter          message + call usage
      Idempotency            ├─ PromptAssembler       idempotency result
      quota read             ├─ ModelGateway          async_jobs direct enqueue
      (short txn)            └─ ToolExecutor          (short txn)
                                    │
               ┌────────────────────┼─────────────────────┐
               ▼                    ▼                     ▼
          Memory Query          Diary Query         Routine/Appearance
          read session          read session        read session

                    scheduler / direct job enqueue
                                  │
       ┌───────────────┬──────────┼──────────┬────────────────┐
       ▼               ▼          ▼          ▼                ▼
   payment worker  diary worker memory worker push worker maintenance
       queue           queue      queue       queue          queue
```

배포는 당분간 같은 저장소와 Docker image를 사용한다. 두 EC2 각각에 API와 별도의 consumer를 하나씩
상주시킨다. scheduler는 현재 인프라의 `/etc/moly-worker-host` 마커가 있는 한 호스트에서만 실행하되,
lookback과 job unique key로 짧은 중단·중복 실행에 안전하게 만든다. queue는 consumer 내부의 priority·
concurrency slot으로 논리 분리하고, 트래픽이 실제로 늘어난 queue만 나중에 프로세스를 분리한다.
이것은 마이크로서비스 전환이 아니라 **실패 도메인을 분리한 모듈러 모놀리스**다.

---

## 5. 코드 구조

```text
app/
  conversation/
    application.py        # 한 턴 유스케이스. phase A/agent/phase B 조정
    context.py            # ContextSnapshot 생성
    prompt.py             # 프롬프트 계층 조립
    repository.py         # 메시지·앵커·멱등·usage 저장
    models.py             # TurnRequest, TurnDraft, TurnResult, TurnUsage

  agent/
    runtime.py            # bounded tool loop와 deadline
    safety.py             # 현재 턴 안전 경로 선택
    gateway.py            # provider 독립 StepResult 계약
    tool.py               # Tool protocol, ToolContext, ToolResult
    registry.py           # 명시적 등록과 allowlist
    tools/
      memory_search.py
      diary_search.py
      diary_get.py
      routines_get.py

  memory/
    commands.py           # extract/reconcile/forget 처리
    queries.py            # recall 및 relationship projection 조회
    projection.py         # Relationship Profile 렌더
    extraction.py         # LLM 출력 schema 검증
    reconciliation.py     # 중복·갱신·모순 처리
    repository.py

  jobs/
    model.py              # Job, JobState, RetryPolicy
    repository.py         # claim/heartbeat/finalize/retry
    registry.py           # job_type → handler

  outbox/                 # 결제·탈퇴 삭제처럼 감사 가능한 전달이 필요한 경로만
    repository.py
    dispatcher.py

worker/
  scheduler.py            # due work를 멱등 enqueue. 외부 API 호출 금지
  runner.py               # 지정 queue 소비
  handlers/
    payment.py
    diary.py
    memory.py
    push.py
    maintenance.py
```

의존 방향은 `application → domain port → infrastructure adapter`다. 도메인 서비스가 FastAPI request,
전역 session, provider SDK 객체를 직접 알지 못하게 한다. 기존 `app/services/*.py`는 단계적으로 새
application/port를 호출하는 façade로 축소한다.

---

## 6. 대화 한 턴의 정확한 계약

### 6.1 불변식

1. LLM 또는 도구를 기다리는 동안 열린 DB 트랜잭션과 유저 락은 0개다.
2. agent phase에서 durable write는 0개다.
3. 유저 메시지, 캐피 응답, 호출별 usage, 일일 quota, 멱등 응답, 기억 후속 job은 Phase B 한
   트랜잭션으로 확정한다.
4. 같은 `(user_id, idempotency_key)`는 최종 결과를 한 번만 만든다.
5. 모든 도구 호출은 서버가 만든 `ToolContext.user_id` 범위 안에서만 조회한다.
6. 최종 답변 생성에 필요한 시간·출력 토큰 예산은 도구 호출 전에 예약한다.

### 6.2 Phase A: snapshot

짧은 트랜잭션에서 다음을 읽고 plain immutable DTO로 복사한다.

- 멱등 완료 결과
- quota/entitlement와 activity date
- Character Persona version, language
- Relationship Profile의 published version
- 최근 메시지 또는 summary checkpoint 이후 메시지
- 현재 장착 아이템과 테마
- 오늘 루틴의 작은 요약(이름 전체가 아니라 예정/완료 개수 등 상주 가치가 있을 때만)
- 현재 로컬 시간 bucket, 마지막 활동 bucket, 오늘 첫 대화 여부

Phase A는 DB를 변경하지 않는다. 기존처럼 quota TOCTOU를 줄이기 위한 짧은 유저 단위 직렬화는
허용하되 commit 직후 해제한다. DTO를 만든 뒤 ORM 객체에는 접근하지 않는다.

### 6.3 Agent phase

Provider adapter의 공통 계약은 wire-format dict가 아니라 typed transcript다.

```text
TranscriptItem = UserText | AssistantText | AssistantToolCalls | ToolResult
ModelStepResult {
  text?: str
  tool_calls: [{call_id, tool_name, validated_arguments}]
  control_intents: [{kind, target_fact_ids?, value?}]
  finish_reason
  usage
}
```

`control_intents`는 provider가 text와 JSON schema를 함께 안정적으로 반환할 수 있을 때만 final step에서
받는다. 해당 조합을 지원하지 않는 provider에서는 명시적 기억 명령을 별도 command routing step으로
처리하고, 일반 자동 기억 추출은 항상 비동기 `ConversationTurnCommitted` 경로를 사용한다. 자유문에서
정규식으로 삭제 대상을 추측하지 않는다.

```text
deadline 시작
  → 현재 발화 안전 분류
  → prompt 조립
  → model step(tools=allowed tools)
  → tool_calls가 있고 tool budget이 남으면 read tools 병렬 실행
  → 모든 call_id에 ToolResult 연결
  → final model step(tool_choice=none, reserved budget 사용)
  → 출력 정책 적용
  → TurnDraft(text, usage, tool_trace, memory_intents)
```

- 기본 tool round는 1회다. 코드 계약은 반복 가능하되 config 상한을 강제한다.
- `asyncio.gather`에 무제한 fan-out하지 않는다. 턴당 call 수와 프로세스 전체 inflight semaphore를 둔다.
- 각 도구는 독립 read-only 세션을 짧게 열고 결과 DTO 생성 후 즉시 닫는다.
- timeout/cancel/error도 정상 `ToolResult(status='unavailable')`로 transcript에 연결한다.
- 전부 실패해도 예약한 final step으로 “지금 확인할 수 없는 상태”를 반영해 답한다.
- 모델의 `memory_intents`는 제안일 뿐 이 단계에서 저장하지 않는다.

### 6.4 Phase B: finalize

유저 단위 락을 다시 얻고 멱등 결과를 재확인한다. 승자만 다음을 한 트랜잭션으로 저장한다.

- 유저 메시지와 캐피 최종 응답
- 모든 hop과 한자·가나 복원 시도를 포함한 `TurnUsage` 및 호출별 `llm_call_usage` 행
- 일일 quota 원자 증가
- context checkpoint 변경
- idempotency response
- `ConversationTurnCommitted` 성격의 extraction `async_jobs` 행 직접 enqueue
- 제품 정책으로 활성화된 경우 모델이 제안한 명시적 memory intent `async_jobs` 행 직접 enqueue
- §19의 제품 정책에 따라 서버 정책기가 확정한 forget이면 해당 fact 상태와 forget marker

forget marker는 privacy deny marker이므로 무거운 LLM 작업 없이 이 트랜잭션에서 바로 쓴다. 관계
프로필 재생성 및 외부 벡터 삭제는 `async_jobs`에 직접 넣는다. 동일 idempotency 요청이 agent phase를
중복 실행할 수는 있지만 저장·차감·잡 생성은 한 번뿐이다.
이를 완전히 없애려는 분산 in-flight 예약은 복잡도 대비 이득이 작으므로 초기에는 만들지 않는다.

### 6.5 응답 확정 뒤 연결이 끊기는 경우

HTTP 응답 전송 실패는 DB 확정과 분리된다. 클라이언트는 같은 idempotency key로 재호출해 저장된
응답을 받는다. 따라서 “응답 크래시 창을 수용한다”가 아니라 **확정 후 전송 실패를 멱등 replay로
복구한다**고 계약한다.

### 6.6 현행 대화 불변식 승계 계약

새 `ConversationApplication`은 다음을 일반적인 “output policy”로 치환하지 않고 코드 계약과 회귀
테스트로 그대로 승계한다.

1. **닉네임 비식별 저장과 현재값 렌더**: LLM에는 현재 닉네임을 주되 저장 직전에
   `naming.to_placeholder(text, current_nickname)`를 적용한다. 조회·프롬프트 재투입 때는
   `naming.render(stored_text, current_nickname)`를 적용해 현재 닉네임과 현재 받침에 맞는 조사를
   다시 계산한다. 기존 `messages`, `greetings`, `diaries.content`뿐 아니라 새
   `memory_facts.canonical_text`, `memory_insights.text`, `relationship_profiles.document_json`의 문자열 값,
   `relationship_profiles.rendered_text`, `conversation_checkpoints.summary`에도 실명 스템을 저장하지
   않는다. extractor/projector 입력은 현재 이름을 알 수 있지만 repository write port가 마지막으로
   placeholder 변환을 강제하며, 백필도 같은 변환기를 사용한다. SOMA-321/322/365 백필 후 남은 평문을
   다시 만들 수 있는 별도 writer를 금지한다.
2. **선발화의 검증·단일 커밋·응답 에코**: Phase 1에서 `greeting_id`를 UUID로 검증하고, 해당 유저
   소유이며 `committed_message_id IS NULL`인 내용만 snapshot에 넣는다. Phase B 유저락 안에서 fresh
   read한 뒤 조건이 여전히 참일 때만 greeting message를 유저 메시지보다 먼저 한 번 insert하고
   `committed_message_id`를 연결한다. 실제 커밋한 경우에만 현재 닉네임으로 렌더한 greeting DTO를 같은
   멱등 응답에 echo한다. 유효하지 않거나 경합에서 진 greeting은 저장·echo하지 않는다.
3. **egress 순서 고정**: 한국어 응답은 `메타 프리앰블 제거 → 한자·가나 복원 호출 → 부호 정제 및
   되묻기 물음표 복원 → naming.to_placeholder` 순서다. 순서를 바꾸거나 단계들을 하나의 자유로운 LLM
   “출력 정책”으로 합치지 않는다. 클라이언트 반환은 저장된 placeholder를 마지막에 `render`한 값이다.
4. **i18n 고정**: 저장된 BCP 47은 `i18n.resolve`로 base language를 해석해 ko/ja/en 콘텐츠 버킷을
   고른다(`None→ko`, 미지원→en). 페르소나는 ko=`CAPI_PERSONA`, ja=`CAPI_PERSONA_JA`, 그 밖은 raw
   BCP 47 언어 지시 분기를 유지한다. 알림과 오류 등 서버 고정문구도 `i18n.pick`의
   resolved→en→ko 규칙을 사용하며, 임의의 `language == 'ko'` 비교나 한국어 하드코딩을 새 경로에
   만들지 않는다.
5. **멱등 응답 고정**: `(user_id, idempotency_key)`에 저장하기 전과 replay 시 모두
   `PostMessageResponse` schema를 검증한다. 비호환 저장 행은 삭제·재실행하지 않고 fail-closed 500으로
   보존한다. JSONB에는 Phase B 당시 닉네임으로 이미 렌더된 greeting/reply, 당시 token·review 값을
   저장하고 replay에서는 다시 렌더하거나 현재 상태로 재계산하지 않는다. 따라서 개명 후 일반 이력은
   새 이름으로 보이지만 과거 멱등 HTTP 응답은 최초 반환값과 byte-equivalent 의미를 유지한다.

---

## 7. 프롬프트와 캐시

프롬프트는 변동성이 낮은 순서로 조립한다.

```text
1. Character Persona + safety precedence + output contract  (전역, 버전 고정)
2. Tool schemas                                            (전역, 버전 고정)
3. Relationship Profile                                    (유저별, publish 전까지 고정)
4. Summary checkpoint + append-only recent messages        (유저별, append-only)
5. Current Turn Context + current user message              (매 턴 변동)
6. Tool results                                            (해당 턴만)
```

- 시간, 장착 아이템, 테마는 작아도 자주 바뀌므로 안정 prefix 앞에 두지 않는다.
- Relationship Profile은 자연어 한 덩어리가 아니라 섹션이 고정된 renderer로 만든다.
- tool result와 기억은 **untrusted data**다. 대괄호 제거 같은 sanitization은 표현 정리일 뿐 보안
  경계가 아니다. 명확한 data delimiter, 길이 제한, system instruction 우선순위, adversarial eval로 막는다.
- `prompt_version`, `persona_version`, `relationship_version`, `toolset_version`을 trace에 남긴다.
- 캐시 적중을 위해 정확성을 희생하지 않는다. 캐시율은 설계 입력이 아니라 관측 후 튜닝 대상이다.

Relationship Profile 예시는 다음처럼 짧고 권위가 구분돼야 한다.

```text
<relationship_profile version="18">
  <stance>편안하지만 섣불리 단정하지 않는다.</stance>
  <known_facts>현재 유효하고 중요도가 높은 사실 5개 이하</known_facts>
  <recent_threads>최근 이어지는 관심사 3개 이하</recent_threads>
  <inferred_tendencies confidence="low">파생 통찰 2개 이하</inferred_tendencies>
</relationship_profile>
```

---

## 8. 도구 설계

### 8.1 도구 인터페이스

```python
class Tool(Protocol):
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    timeout_ms: int

    async def execute(self, ctx: ToolContext, args: BaseModel) -> ToolResult: ...
```

`ToolContext`는 `user_id`, `language`, `activity_date`, `deadline`만 서버가 만든다. 모델 인자에
`user_id`, SQL, arbitrary filter를 두지 않는다. 출력은 Pydantic schema, 행 수, 글자 수, 날짜 범위를
모두 제한한다.

### 8.2 v1 도구

| 도구 | 목적 | 인자 | 제한 |
|---|---|---|---|
| `search_memory` | 구체적인 과거 사실 회상 | query, optional time hint | active fact/insight 구분, top K |
| `search_diaries` | 내용 또는 기간으로 일기 찾기 | query?, from?, to? | published diary만, 발췌 길이 제한 |
| `get_diary` | 특정 activity date 일기 | date | 해당 유저 1건 |
| `get_routines` | 예정·완료 루틴 확인 | date? | soft-deleted 제외, 유저 locale |

시간, 현재 착용 아이템, 테마는 도구가 아니라 `CurrentTurnContext`로 항상 제공한다. 데이터가 작고
정확한 현재 상태이며 모델이 “조회할지 판단”하게 할 이유가 없기 때문이다.

### 8.3 쓰기 의도

초기 버전에는 외부 상태를 즉시 바꾸는 쓰기 도구를 제공하지 않는다.

- 자동 추출은 제품 UX와 무관한 best-effort projection으로 계속할 수 있다.
- `pin`/`forget` 명령을 어떤 표현에서 자동 확정할지, 모호할 때 확인할지, 범위를 어디까지로 할지는
  §19의 제품 결정이다. 결정 전에는 intent를 durable write로 활성화하지 않고 shadow 분류만 한다.
- 제품 결정 후에도 모델은 `memory_intent`와 후보 fact id만 제안한다. 서버의 결정적 정책기가 확정한
  명령만 Phase B에서 상태 변경 또는 `async_jobs` 직접 enqueue로 반영한다.
- 루틴 완료/아이템 장착 같은 실제 행동은 기존 명시적 API가 진실 소스다.

향후 대화에서 쓰기 도구를 추가할 때는 read tool과 별도 정책을 사용한다. 확인 필요 여부,
idempotency key, authorization, audit event, compensation 가능성을 도구별로 정의해야 한다.

---

## 9. 메모리 모델

### 9.1 네 계층

| 계층 | 내용 | 생성 | 대화 사용 |
|---|---|---|---|
| Evidence | 원본 message/diary/routine/profile event | 원본 트랜잭션 | 직접 대량 주입 안 함 |
| Fact | evidence에서 추출한 구체 사실 | memory worker | 검색 또는 중요 fact 상주 |
| Insight | 여러 fact에서 유도한 경향 | reflection job | 낮은 권위로 소수 상주/검색 |
| Relationship Profile | 현재 대화용 작은 projection | projector | 매 턴 상주 |

이 표의 Evidence는 장기 목표 taxonomy다. **v1 activation gate에서는 `conversation_turn`만 장기기억
extraction source로 허용한다.** diary/routine/profile event는 Evidence로 분류할 수 있지만 v1 extractor,
`memory_source_turns`, `memory_source_closures`에는 넣지 않는다. 각 source에 user별 monotonic watermark와
closure의 enqueue/replay/finalize 거부 계약을 추가하고 검증하기 전에는 이 gate를 열지 않는다.

원본과 projection을 분리하므로 projection을 언제든 다시 만들 수 있다. mem0는 원본 저장소나
도메인 모델이 아니라 필요하면 extraction/검색 adapter로만 쓴다. SQLite history에 의존하는 현재
mem0 write path는 새 구조의 최종 상태에서 제거한다.

### 9.2 권장 테이블

`memory_facts`

- `id`, `user_id`, `kind`, `canonical_text`, `subject`, `predicate`, `object_json`
- `event_time`, `valid_from`, `valid_to`, `status(active|superseded|forgotten)`
- `importance`, `confidence`, `content_hash`, `superseded_by`
- `embedding`, `created_at`, `updated_at`
- `id`는 PK이고, user-scoped FK의 대상이 되도록 `(user_id, id)` UNIQUE도 둔다.
- `canonical_text`를 포함한 모든 자연어 writer는 §6.6의 placeholder 저장 계약을 적용한다.

`memory_evidence`

- `fact_id`, `source_type`, `source_id`, `source_excerpt_hash`, `observed_at`
- 같은 source/fact 조합 unique

`memory_insights`

- `id`, `user_id`, `text`, `confidence`, `status(active|invalidated|superseded)`, `valid_from`, `valid_to`
- `derivation_version`, `created_at`
- `id`는 PK이고 `(user_id, id)` UNIQUE를 둔다.

`memory_insight_sources`

- `user_id`, `insight_id`, `fact_id`, PRIMARY KEY `(user_id, insight_id, fact_id)`
- `(user_id, insight_id)` → `memory_insights(user_id, id)`와 `(user_id, fact_id)` →
  `memory_facts(user_id, id)`를 각각 `ON DELETE RESTRICT` 복합 FK로 강제한다. 애플리케이션이
  `user_id`를 잘못 주입해도 타 유저 fact를 insight의 근거로 연결할 수 없다. insight를 또 다른
  insight의 근거로 사용할 수 없게 두 번째 FK 대상 자체도 fact로 제한한다.

`relationship_profiles`

- `id`, `user_id`, `version`, `locale`, `memory_generation`, `relationship_profile_input_revision`, `document_json`,
  `rendered_text`, `render_hash`
- `status(draft|published|invalidated|superseded)`, `created_at`, `published_at`
- `id`는 PK이고 `(user_id, id)` UNIQUE를 둔다.
- `(user_id, locale)`별 published 1개는 `CREATE UNIQUE INDEX relationship_profiles_one_published_idx
  ON relationship_profiles(user_id, locale) WHERE status='published'`로 강제한다.
- `document_json`의 각 항목은 `source_refs=[{type: fact|insight, id: uuid}]`를 가져야 한다. 단일 자유문
  본문이나 타입 없는 source id만 저장하지 않는다.
- `(user_id, locale, version)`으로 버전을 구분하며 자연어 필드는 모두 placeholder 상태다. publish 후
  context builder가 현재 닉네임으로 렌더한다.

`relationship_profile_sources`

- `id`, `user_id`, `relationship_profile_id`, `item_key`, `fact_id?`, `insight_id?`
- `num_nonnulls(fact_id, insight_id)=1` CHECK를 두고, `(user_id, relationship_profile_id)` →
  `relationship_profiles(user_id, id)`는 `ON DELETE CASCADE`, `(user_id, fact_id)` →
  `memory_facts(user_id, id)`와 `(user_id, insight_id)` → `memory_insights(user_id, id)`는 각각
  `ON DELETE RESTRICT` 복합 FK로 강제한다. `item_key`는 `document_json` 항목의 불변 키이며 같은
  profile/item/source edge는 nullable FK를 함께 넣은 복합 UNIQUE로 처리하지 않는다. 대신
  `CREATE UNIQUE INDEX relationship_profile_sources_fact_uq ON relationship_profile_sources
  (user_id, relationship_profile_id, item_key, fact_id) WHERE fact_id IS NOT NULL`과
  `CREATE UNIQUE INDEX relationship_profile_sources_insight_uq ON relationship_profile_sources
  (user_id, relationship_profile_id, item_key, insight_id) WHERE insight_id IS NOT NULL`인 두 partial
  unique index로 fact edge와 insight edge의 중복을 각각 막는다.
- profile draft 작성자는 JSON의 모든 `source_refs`를 이 테이블에도 같은 트랜잭션으로 쓴다. publish는
  `chat_contexts`의 해당 user 행을 `FOR UPDATE`로 잠근 뒤 (a) JSON refs와 edge 행이 type/id/item_key까지
  정확히 양방향 일치하고, (b) 모든 edge가 같은 user의 active fact/insight를 가리키고, (c) insight의
  모든 fact source도 같은 user이며 active이고 forget marker에 걸리지 않고, (d) profile의
  `memory_generation`이 현재 값과 같고, (e) `relationship_profile_input_revision`이 `chat_contexts`의 현재 값과 같은
  경우에만 `draft→published`를 허용한다. (a)~(c)를 어기면 publish 없이 `state='dead',
  result_code='invalid_provenance'`로 끝내며 자동 재시도하지 않는다. (d)는 §9.5의 `stale_generation`,
  (e)는 `stale_profile_input`으로 결과를 폐기한다. DB 복합 FK와 이 publish 검증을 모두
  통과해야 하므로 JSON만 조작해 타 유저 source를 profile에 유입할 수 없다.
- publish 전이는 위 검증과 같은 트랜잭션에서 수행한다. `chat_contexts` user 행을 잠근 상태로 해당
  `(user_id, locale)`의 현재 `published` 행도 `FOR UPDATE`로 잠그고, 기존 행이 있으면 먼저
  `published→superseded`로 바꾼 다음 draft를 `published`로 바꾸고 `published_at=clock_timestamp()`를
  기록한다. 두 UPDATE 사이에는 commit하지 않으며 어느 statement라도 실패하면 모두 rollback한다.
  따라서 partial unique index를 위반하지 않고 다른 트랜잭션에는 구·신 profile의 중간 상태가 보이지 않는다.

`memory_forget_markers`

- `id`, `user_id`, `scope(fact|predicate|all)`, `fact_id?`, `normalized_hash?`,
  `normalization_version?`, `predicate?`, `memory_generation`, `created_at`, `expires_at?`
- `scope='fact'`는 `fact_id`, 해당 fact를 privacy deny 정규화한 `normalized_hash`, 그
  `normalization_version`이 모두 필수이고,
  `(user_id, fact_id)`는 같은 유저의 `memory_facts(user_id, id)`를 참조한다. 이를 위해
  `memory_facts(user_id,id)` unique를 둔다. 이 복합 FK는 `ON DELETE NO ACTION DEFERRABLE INITIALLY
  DEFERRED`다. 이때 `predicate`는 NULL이다. `scope='predicate'`는
  `predicate`만 NOT NULL이고 fact target 세 필드는 NULL, `scope='all'`은 네 target 필드가 모두
  NULL이어야 한다는 CHECK를 둔다.
- fact 행은 아래 retention 종료 절차 전까지 hard delete하지 않아 marker의 FK와 감사 근거를 보존한다. fact marker는
  id뿐 아니라 hash에도 매칭해 같은 내용을 새 id로 재추출해 되살리는 것을 막는다. normalizer 변경 시
  extractor는 DB에 남은 marker version들을 모두 비교하며, marker를 새 version으로 backfill·검증하기 전
  구 version 비교 코드를 제거하지 않는다.
- 사용자 forget marker의 `expires_at`은 NULL이며 시간 경과로 만료시키지 않는다. retention 종료는 fact
  scope면 그 fact, predicate/all scope면 그 범위의 모든 fact/evidence를 대상으로 한다. 먼저 해당
  extraction 입력 구간에 아래 `memory_source_closures`가 영속 기록됐고 그 범위가 enqueue/replay/finalize
  될 수 없으며 외부 vector도 삭제됐음을 검증한다. 그 뒤 한 DB 트랜잭션에서 영향받은 profile을 `invalidated`하고 profile source
  edge와 insight source edge, insight, evidence, fact를 순서대로 hard-delete한 다음 **마지막 statement로
  marker를 삭제**한다. marker→fact FK는 deferred `NO ACTION`이므로 fact 삭제 뒤 marker 삭제까지 같은
  트랜잭션에서만 잠시 허용되고, 중간 실패는 전부 rollback되어 marker와 fact가 함께 복원된다. marker를
  먼저 지우거나 fact만 지운 채 commit할 수 없다. `expires_at`을 채우는 별도 만료 단계는 없다.
- 삭제 요청 원문 전체를 저장하지 않는다.

`memory_source_turns`와 `memory_source_closures`

- v1에서 이 두 테이블의 source 범위는 위 activation gate에 따라 `conversation_turn`뿐이다.
- 장기기억 extraction 대상인 커밋 turn마다 `memory_source_turns(user_id, message_id,
  source_watermark, committed_at)`를 두고, `(user_id, message_id)`를 PK, `(user_id,
  source_watermark)`를 UNIQUE로 둔다. `chat_contexts.memory_source_watermark bigint NOT NULL DEFAULT 0`을
  유저별 high watermark로 두며 Phase B의 `chat_contexts` user lock 안에서 1 증가시킨 값을 새 turn에
  배정한다. cutover 전 기존 turn은 `(created_at, id)`의 고정 순서로 backfill하고
  `chat_contexts.memory_source_watermark`가 user별 최대값과 같은지 검증한 뒤 enqueue/replay guard를 켠다.
- `memory_source_closures(id, user_id, source_kind, from_watermark, through_watermark,
  forget_operation_id, created_at)`를 두며 현재 `source_kind='conversation_turn'`,
  `from_watermark <= through_watermark` CHECK, `(user_id, forget_operation_id, source_kind,
  from_watermark, through_watermark)` UNIQUE를 강제한다. forget이 대상으로 삼은 fact의 evidence turn
  범위와 predicate/all의 cut watermark까지를 이 테이블에 기록한다. 이 행은 privacy marker retention과
  무관한 영속 replay deny 기록이며 계정 삭제 전에는 삭제하거나 축소하지 않는다.
- 모든 memory job payload는 `source_kind`, `source_from_watermark`, `source_through_watermark`를 가진다.
  일반 enqueue와 §11.2 운영 replay는 `chat_contexts` user 행을 잠근 짧은 트랜잭션에서 closure 범위와
  겹치면 job을 만들지 않고 `source_range_closed` 감사 결과를 남긴다. forget은 같은 user lock 아래 먼저
  closure를 쓴 뒤 generation을 올린다. 이미 queued/running인 job도 finalize 직전 closure overlap을
  검사해 겹치면 결과를 폐기하고 `state='succeeded', result_code='source_range_closed'`로 끝낸다.
  따라서 enqueue 검사와 closure 생성이 경합하지 않으며 marker가 나중에 삭제돼도 옛 turn 범위의
  신규 operation replay로 fact를 되살릴 수 없다.
- marker retention 종료의 사전 조건은 해당 closure 행의 존재, 대상 source 범위 backfill 완료,
  enqueue/replay/finalize overlap 거부 테스트 통과다. closure는 marker·fact hard delete 트랜잭션에
  포함해 삭제하지 않는다.

### 9.3 추출과 reconcile

```text
ConversationTurnCommitted
  → extract job
  → schema-validated candidate facts
  → deterministic normalize/deduplicate
  → 기존 active fact와 비교
  → ADD | REINFORCE | SUPERSEDE | KEEP_BOTH | IGNORE
  → projection refresh job coalesce
```

- LLM은 candidate와 관계 판정만 제안한다. 상태 변경은 도메인 코드가 한다.
- scalar slot은 명확한 predicate에만 사용한다. 자유로운 모든 사실을 하나의 slot 체계에 억지로
  넣지 않는다.
- `content_hash`는 정규화 버전을 포함한다. 정규화 규칙 변경 시 과거 hash와 충돌하지 않게 한다.
- importance와 confidence는 별개다. 감정적으로 중요하지만 불확실한 사실을 표현할 수 있어야 한다.
- “사용자가 말했다”와 “캐피가 추론했다”를 절대 같은 권위로 합치지 않는다.
- `chat_contexts.relationship_profile_input_revision bigint NOT NULL DEFAULT 0`을 user별 단조 revision으로
  둔다. extract/reconcile이 fact 또는 evidence를 반영할 때, reflection이 insight의 내용·source·상태를
  바꿀 때, maintenance가 profile 후보 fact/insight를 교정·병합·무효화할 때, forget이 fact/insight/profile을
  무효화할 때 각각 그 상태 변경 트랜잭션에서 1 증가시킨다. 한 트랜잭션의 여러 변경은 한 번만 올리며,
  입력을 바꾸지 못한 시도·재시도·embedding 재색인은 올리지 않는다. 증가 후 값을 그 트랜잭션에서
  enqueue하는 relationship refresh payload와 §11.2 dedup key에 사용한다.

### 9.4 검색

후보 생성과 재랭킹을 분리한다.

1. fact는 `user_id=:user_id AND status='active'`이면서 현재 marker의 id/hash/predicate/all 어느 것에도
   매칭하지 않는 행만, insight는 `user_id=:user_id AND status='active'`이면서 모든 source fact가 같은
   조건을 만족하는 행만 hard filter
2. semantic top N + lexical/date 후보 union
3. `relevance`, `importance`, `recency`, `confidence`로 deterministic rerank
4. Relationship Profile에 이미 들어간 fact 제외
5. fact와 insight를 라벨링해 top K 반환

점수 가중치는 코드 상수가 아니라 평가 데이터로 결정한다. 검색 품질 평가는 recall@K만 보지 않고
다음 네 가지를 함께 본다.

- 필요한 기억을 찾았는가
- 틀리거나 폐기된 기억을 노출하지 않았는가
- 관련 없는 민감 기억을 불필요하게 꺼내지 않았는가
- 답변이 기억을 과시하지 않고 자연스럽게 사용했는가

### 9.5 forget과 탈퇴

- forget 기능은 해당 유저의 legacy 문자열 snapshot을 폐기하고 정규화 source로 전환한 뒤에만
  활성화한다. `chat_contexts`에 `memory_mode(legacy|normalized)`와 `memory_generation`을 additive로
  추가한다. 유저별 cutover는 **API-A/API-B와 consumer-A/consumer-B가 모두 mode-aware release로
  교체됐음이 배포 inventory와 각 프로세스 heartbeat에서 확인된 뒤에만** 연다. 한 프로세스라도 구버전이거나
  버전 확인이 안 되면 전 cohort의 cutover gate를 닫는다. 유저별 cutover 트랜잭션은 backfill watermark와 published profile 준비를 확인하고
  `memory_mode='normalized'`, `memory_generation=memory_generation+1`, `memory_text=NULL`,
  `memory_refreshed_at=NULL`을 함께 확정한다.
- mode-aware `_save_memory`는 `INSERT` 기본값을 legacy로 두고 conflict `UPDATE`에
  `WHERE chat_contexts.memory_mode='legacy'`를 걸어 normalized 행을 갱신하지 않는다. expand DDL에는
  `BEFORE INSERT OR UPDATE` trigger도 `db/migrations/`에 추가하고 dev→prod 순으로 적용한다. INSERT에서
  `NEW.memory_mode='normalized'`이면 snapshot 필드를 NULL로 만들고, UPDATE에서
  `OLD.memory_mode='normalized'`이면 `NEW.memory_mode='normalized'`로 되돌려 mode downgrade도 막은 뒤
  `NEW.memory_text`와 `NEW.memory_refreshed_at`을 강제로 NULL로 만든다. 따라서 구버전 호스트가 남은 호환 배포 구간이나
  운영 실수로 cutover가 먼저 실행돼도 현행 무조건 upsert가 legacy snapshot을 재생성할 수 없다.
  trigger는 구버전 전부 제거 확인 뒤에도 contract migration까지 유지한다. 다만 구버전 reader는
  normalized 의미를 모르므로 이 trigger를 cutover 허가의 대체재로 사용하지 않는다.
- `memory_mode='normalized'`인 context builder는 어떤 장애·빈 성공·profile 미생성 상태에서도
  `chat_contexts.memory_text` 또는 mem0 문자열로 fallback하지 않는다. 정상 빈 결과는 빈 기억으로,
  저장소 장애는 기억 없는 응답으로 fail-open하며 경보를 남긴다. 즉, 현행 `_reload_memory`의
  “빈 성공이면 과거 문자열 재사용” 규칙은 legacy mode에서만 허용되고 cutover 순간 종료된다.
- forget 요청이 확정되면 publish와 같은 `chat_contexts` user 행을 `FOR UPDATE`로 잠근 한 트랜잭션에서
  대상 evidence의 source watermark 범위를 확정해 `memory_source_closures`를 먼저 쓰고,
  `memory_generation`을 올리고 `relationship_profile_input_revision`도 1 증가시킨 다음 marker를 쓰고
  matching fact를 `forgotten`으로 전환한다. 이어 반드시
  `memory_insight_sources.user_id=:user_id` 범위에서 그 fact를 참조하는 active insight를 `invalidated`로
  바꾸고 `valid_to=now()`로 닫으며, 해당 user의 `relationship_profile_sources`가 이 fact/insight를
  참조하는 published profile도 `invalidated`로 바꾼다. profile refresh와 외부 벡터 삭제 job은 같은
  트랜잭션에서 직접 enqueue한다. 이는 결제급 saga가 아니라 동일 Postgres 안의 결정적 파생 무효화다.
- 검색은 §9.4의 fact/insight 조건을 항상 적용한다. published profile renderer도 정규화된
  `relationship_profile_sources`와 JSON `source_refs`를 대조하고 각 source를 현재 fact/insight 상태 및
  marker와 다시 대조해 하나라도 없거나 무효하면 해당 항목을 렌더하지 않는다. 새 profile publish는
  §9.2의 동일-user 복합 FK와 publish 검증을 모두 통과한 경우만 허용한다. 따라서 profile refresh 지연 중에도
  forgotten fact나 그것에서 파생된 insight가 대화에 다시 들어오지 않는다. id가 없는 legacy
  `memory_text`를 필터링하는 용도로 marker를 쓰지 않는다.
- 이미 queued/running인 모든 memory handler는 finalize 직전 forget marker를 다시 검사한다.
- 모든 memory job payload는 enqueue 당시 `memory_generation`을 가진다. finalize는 먼저 §9.2의 source
  range와 closure를 검사해 겹치면 `state='succeeded', result_code='source_range_closed'`로 끝낸다. 겹치지
  않을 때만 현재 generation을 비교하며, cutover 전처럼 generation만 낡은 job은 결과를 버리고
  `state='succeeded', result_code='stale_generation'`으로 끝낸다. 두 검사를 모두 통과할 때만 fact/profile을
  publish한다.
- 벡터 인덱스 삭제 실패는 별도 delete job으로 재시도하며 검색은 Postgres 상태를 먼저 필터하므로 즉시
  비노출을 보장한다.
- 탈퇴 트랜잭션은 FK 소유 데이터 삭제와 `UserDeletionRequested` outbox를 함께 기록한다. FK 밖 외부
  저장소는 deletion worker가 처리하고 완료/실패를 감사한다.

---

## 10. 단기기억과 대화 요약

현재 append-only anchor 방식은 유지할 가치가 있다. 다만 context reset 때 단순히 앞 메시지를 버리는
대신 비동기 summary checkpoint를 사용한다.

- `conversation_checkpoints(user_id, through_message_id, summary, version, source_hash, created_at)`
- checkpoint는 해당 구간 메시지가 모두 커밋된 뒤 비동기로 생성한다.
- 다음 턴은 가장 최신 checkpoint + 이후 메시지를 사용한다.
- summary가 늦거나 실패하면 기존 메시지 window로 계속 답할 수 있어야 한다.
- 같은 `through_message_id`와 `source_hash`는 한 번만 생성한다.
- 새 summary가 이전 summary를 입력으로 받을 수는 있지만 일정 횟수마다 원본 구간 기반 재검증을 해
  누적 왜곡을 측정한다.

Summary는 Fact가 아니다. summary에서 장기 사실을 재추출하지 않고 원본 Evidence에서만 추출한다.

---

## 11. 내구 잡 플랫폼

### 11.1 왜 단일 워커 틱이 아닌가

결제는 낮은 지연과 강한 정합성이 필요하고, 일기·메모리는 느린 LLM에 의존하며, 푸시는 만료 시각이
중요하다. 하나의 틱과 전역 lease로 묶으면 서로 다른 SLO와 실패 정책을 표현할 수 없다.

### 11.2 job DDL과 outbox 범위

기억·요약·일기·푸시는 원본/스케줄러 트랜잭션에서 `async_jobs`에 직접 insert한다. 중복·누락을
멱등 재시도로 수렴시키면 충분하므로 outbox→dispatcher 2단계를 강제하지 않는다. `outbox_events`는
RevenueCat raw inbox의 처리 전달과 `UserDeletionRequested`처럼 전달 이력을 장기간 감사해야 하는
경로에만 사용한다.

잡 스키마는 다음 DDL 하나를 기준으로 한다. 상태명은 전 구간에서 `ready`로 통일한다.

```sql
CREATE TABLE async_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  queue text NOT NULL,
  job_type text NOT NULL,
  user_id uuid NULL REFERENCES profiles(id) ON DELETE CASCADE,
  dedup_key text NOT NULL,
  payload jsonb NOT NULL,
  state text NOT NULL DEFAULT 'ready'
    CHECK (state IN ('ready','running','succeeded','dead','cancelled')),
  priority integer NOT NULL DEFAULT 100,
  available_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NULL,
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts integer NOT NULL CHECK (max_attempts > 0),
  lease_owner text NULL,
  lease_token uuid NULL,
  lease_until timestamptz NULL,
  last_error_code text NULL,
  last_error_at timestamptz NULL,
  result_code text NULL,
  result_detail jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz NULL,
  UNIQUE (job_type, dedup_key),
  CHECK (
    (state = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
    OR (state <> 'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL)
  )
);
CREATE INDEX async_jobs_claim_idx
  ON async_jobs (queue, priority, available_at, created_at)
  WHERE state = 'ready';
CREATE INDEX async_jobs_reclaim_idx
  ON async_jobs (lease_until)
  WHERE state = 'running';
```

완료·dead 행은 보존한다. 따라서 `dedup_key`는 “작업 이름”이 아니라 **논리 실행 세대**를 포함한다.

- turn extraction: `turn:{message_id}:extractor:{version}`
- relationship refresh: `user:{user_id}:watermark:{source_watermark}:input-revision:{relationship_profile_input_revision}:renderer:{version}:locale:{locale}`
- diary: canonical formatter `diary_dedup_key(user_id, activity_date, generation_version)` =
  `user:{user_id}:date:{activity_date}:generation:{generation_version}`
- 저녁 push: `user:{user_id}:date:{activity_date}:kind:evening`
- 일/주기 maintenance: `period:{YYYY-MM-DD}:shard:{n}:version:{version}`
- 운영 replay: 기존 행을 되살리지 않고 `replay:{old_job_id}:{operation_id}`로 새 행을 만들고 감사 로그를
  남긴다. memory job은 새 operation이어도 §9.2의 영속 source closure와 겹치면 생성 자체를 거부한다.

같은 generation의 scheduler 재실행은 `ON CONFLICT DO NOTHING`으로 합쳐진다. relationship refresh는
같은 source watermark에서도 `relationship_profile_input_revision`이 바뀌면 새 generation으로 실행되고, 그 밖의
잡은 위 key의 날짜·버전·locale 등 논리 입력이 바뀌면 다시 실행된다. 단순 wall-clock UUID로 매 틱 새
generation을 만들지 않는다.

### 11.3 상태 전이

```text
ready --claim--> running --success--> succeeded
  ▲                 │
  │                 ├--retryable failure--> ready(available_at=backoff)
  │                 ├--attempt exhausted--> dead
  └--lease expired--┘

ready/running --expired policy--> cancelled
```

- claim은 아래 SQL을 짧은 한 트랜잭션에서 실행하며 `ready` 행만 가져간다. lease 만료 회수와
  `expires_at`/attempt 소진 정리는 claim 앞의 전 큐 UPDATE로 하지 않고, 큐별 reaper가 별도 cadence로
  bounded batch 처리한다. attempt는 claim 성공 시 증가하므로 crash-loop도 소진된다.
- 외부 호출 중 DB row lock과 session을 보유하지 않는다.
- finalize와 heartbeat는 모두 `job_id + lease_owner + lease_token + state='running'` fencing을 쓴다.
- heartbeat가 필요한 장기 잡만 lease를 연장한다. 짧은 잡은 충분한 lease와 호출 timeout을 사용한다.
- dead job은 자동 삭제하지 않는다. replay는 새 운영 action과 audit trail을 남긴다.

```sql
BEGIN;

WITH candidate AS (
  SELECT id
  FROM async_jobs
  WHERE queue=:queue
    AND state='ready'
    AND available_at <= now()
    AND attempt < max_attempts
    AND (expires_at IS NULL OR expires_at > now())
  ORDER BY priority ASC, available_at ASC, created_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT :batch_size
)
UPDATE async_jobs j
SET state='running', attempt=j.attempt+1,
    lease_owner=:worker_id, lease_token=gen_random_uuid(),
    lease_until=now()+(:lease_seconds * interval '1 second')
FROM candidate c
WHERE j.id=c.id
RETURNING j.*;

COMMIT;
```

큐별 reaper는 consumer claim과 독립된 짧은 주기로 실행한다. 한 실행은 정확히 한 `:queue`만 대상으로
하고 `FOR UPDATE SKIP LOCKED LIMIT :reap_batch_size`로 묶는다. 매 주기마다 아래 **terminal 전이
statement를 먼저 한 batch 실행·commit**하고, 이어 재시도 가능 lease 회수 statement도 별도 한 batch
실행·commit한다. 따라서 지속적인 retryable backlog가 terminal 행을 batch 밖으로 밀어내지 않고,
지속적인 terminal backlog도 retryable 회수를 완전히 굶기지 않는다. 각 statement는 한 batch 뒤 다음
주기에 양보한다. queue별 cadence와 batch 크기는 backlog 실측으로 정하고 전 큐 무제한 UPDATE는 금지한다.

```sql
-- 1. expired lease 중 terminal 전이 전용 batch
BEGIN;

WITH candidate AS (
  SELECT id
  FROM async_jobs
  WHERE queue=:queue
    AND state='running'
    AND lease_until < now()
    AND ((expires_at IS NOT NULL AND expires_at <= now()) OR attempt >= max_attempts)
  ORDER BY CASE WHEN expires_at IS NOT NULL AND expires_at <= now() THEN 0 ELSE 1 END,
           lease_until, id
  FOR UPDATE SKIP LOCKED
  LIMIT :reap_batch_size
)
UPDATE async_jobs j
SET state=CASE
      WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'cancelled'
      ELSE 'dead'
    END,
    finished_at=now(),
    last_error_code=CASE
      WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'expired'
      ELSE 'attempts_exhausted' END,
    last_error_at=now(), lease_owner=NULL, lease_token=NULL, lease_until=NULL
FROM candidate c
WHERE j.id=c.id;

COMMIT;

-- 2. expired lease 중 재시도 가능한 행 전용 batch
BEGIN;

WITH candidate AS (
  SELECT id
  FROM async_jobs
  WHERE queue=:queue
    AND state='running'
    AND lease_until < now()
    AND (expires_at IS NULL OR expires_at > now())
    AND attempt < max_attempts
  ORDER BY lease_until, id
  FOR UPDATE SKIP LOCKED
  LIMIT :reap_batch_size
)
UPDATE async_jobs j
SET state='ready', available_at=now(), finished_at=NULL,
    last_error_code='lease_expired', last_error_at=now(),
    lease_owner=NULL, lease_token=NULL, lease_until=NULL
FROM candidate c
WHERE j.id=c.id;

COMMIT;
```

`ready` 상태에서 이미 만료됐거나 attempt를 소진한 행도 같은 queue/LIMIT 패턴의 별도 statement로
`cancelled`/`dead` 처리한다. 서로 다른 queue reaper는 행과 실행 slot을 공유하지 않아 content 대량
backlog가 critical/notification 정리를 잠그지 않는다.

```sql
WITH candidate AS (
  SELECT id
  FROM async_jobs
  WHERE queue=:queue
    AND state='ready'
    AND ((expires_at IS NOT NULL AND expires_at <= now()) OR attempt >= max_attempts)
  ORDER BY available_at, id
  FOR UPDATE SKIP LOCKED
  LIMIT :reap_batch_size
)
UPDATE async_jobs j
SET state=CASE
      WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'cancelled'
      ELSE 'dead' END,
    finished_at=now(),
    last_error_code=CASE
      WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'expired'
      ELSE 'attempts_exhausted' END,
    last_error_at=now()
FROM candidate c
WHERE j.id=c.id;
```

성공 finalize는 다음과 같다. 0행이면 lease를 잃은 결과이므로 도메인 반영도 하지 않는다. 도메인
결과와 후속 job insert가 있다면 이 UPDATE와 같은 짧은 트랜잭션에 둔다.

```sql
UPDATE async_jobs
SET state='succeeded', finished_at=now(), result_code=:result_code, result_detail=:result_detail,
    lease_owner=NULL, lease_token=NULL, lease_until=NULL
WHERE id=:id AND state='running' AND lease_owner=:worker_id AND lease_token=:lease_token;
```

문서의 `succeeded(no_published_diary)` 같은 표기는 별도 state가 아니라
`state='succeeded', result_code='no_published_diary'`를 뜻한다. 구조화된 비민감 진단만
`result_detail`에 기록하고 payload나 사용자 본문을 복제하지 않는다.

retryable 실패 finalize에서 **현재 claim으로 증가한 `attempt >= max_attempts`이면 즉시 dead**, 아니면
`ready`로 되돌린다. 즉 max_attempts=3이면 세 번째 claim의 실패 시점에 dead가 되며, 세 번째 claim
중 crash하면 다음 reaper가 dead로 만든다.

```sql
UPDATE async_jobs
SET state=CASE WHEN attempt >= max_attempts THEN 'dead' ELSE 'ready' END,
    available_at=CASE WHEN attempt >= max_attempts THEN available_at ELSE :retry_at END,
    finished_at=CASE WHEN attempt >= max_attempts THEN now() ELSE NULL END,
    last_error_code=:error_code, last_error_at=now(),
    lease_owner=NULL, lease_token=NULL, lease_until=NULL
WHERE id=:id AND state='running' AND lease_owner=:worker_id AND lease_token=:lease_token;
```

### 11.4 재시도 분류

| 오류 | 처리 |
|---|---|
| timeout, 429, 일시 네트워크/DB 장애 | exponential backoff + jitter |
| schema validation, unsupported payload | 즉시 dead |
| 사용자 탈퇴/대상 삭제 | succeeded 또는 cancelled로 명시 |
| push token invalid | token 비활성화 후 succeeded |
| deadline/expires_at 경과 | 정책에 따라 cancelled |

무조건 모든 예외를 재시도하지 않는다. handler는 typed error code를 반환한다.

### 11.5 큐와 프로세스

- `critical`: payment webhook events
- `interactive_async`: memory intent, forget, post-turn extraction
- `content`: diary generation, summary, reflection
- `notification`: evening check-in
- `maintenance`: orphan cleanup, reindex, reconciliation

각 큐는 concurrency, timeout, max attempts 기준이 다르다. 그러나 초기부터 프로세스를 다섯 개로
나누지는 않는다. 현재 규모에서는 두 EC2에서 다음처럼 실행한다.

```text
EC2-A(marker): API + consumer + systemd timer → scheduler oneshot
EC2-B:         API + consumer
```

consumer 내부에서 긴 content job이 critical/notification을 막지 않도록 queue별 concurrency slot과
critical 예약 slot을 둔다. 두 consumer 중 하나가 내려가도 다른 하나가 전체 queue를 처리한다. 그것으로
부족하다는 지표가 확인될 때만 critical/content 프로세스를 분리한다. Redis, Kafka, Celery 같은 새
인프라는 초기 버전에 도입하지 않고 Postgres job queue를 사용한다.

### 11.6 두 EC2와 롤링 배포

ALB deregistration과 connection draining은 HTTP API 요청에만 적용된다. worker는 ALB를 거치지 않으므로
배포 스크립트가 worker container를 어떻게 stop/start하는지 별도로 계약해야 한다.

두 EC2는 다음 구성으로 둔다.

```text
EC2-A(marker): API-A + consumer-A + scheduler timer
EC2-B:         API-B + consumer-B
                    │
                    └── 같은 Postgres async_jobs
```

- scheduler는 현재 `moly-infra/deploy.sh`의 `/etc/moly-worker-host` guard를 그대로 사용한다. scheduler가
  재실행되거나 운영 실수로 잠시 두 개가 떠도 `(job_type, dedup_key)` unique와
  `INSERT ... ON CONFLICT DO NOTHING`으로 한 행만 생긴다.
- marker host가 배포되는 짧은 동안 enqueue가 멈출 수는 있지만 scheduler는 수 초짜리이고, 다음
  cadence의 lookback이 누락을 보충한다. 작업 실행 자체는 다른 host consumer가 계속한다.
- consumer A/B는 `FOR UPDATE SKIP LOCKED`로 서로 다른 job을 claim한다. 같은 job은 한 consumer만
  `lease_token`을 갖는다.
- 배포 시 host를 ALB에서 뺀 뒤 consumer에 SIGTERM을 보낸다. consumer는 새 claim을 즉시 중단하고,
  짧은 job만 grace period 안에서 마친다.
- grace period 안에 끝나지 않은 LLM job은 heartbeat를 중단하고 종료한다. lease가 만료되면 다른 host가
  회수한다. 종료 직전에 무조건 `ready`로 돌리면 아직 끝나지 않은 외부 호출과 겹칠 수 있으므로,
  cancellation이 확정되지 않은 job은 lease 만료로 회수한다.
- job finalize는 `job_id + lease_owner + lease_token` fencing 조건을 사용한다. 배포 전에 시작한 old consumer가 늦게
  돌아와도 lease를 잃었다면 결과를 확정할 수 없다.
- 새 consumer와 API가 healthy해진 뒤 host를 ALB에 재등록하고 다음 EC2를 배포한다. 따라서 항상 최소
  한 consumer가 살아 있다. scheduler의 짧은 공백은 다음 cadence의 lookback으로 보완한다.

롤링 중에는 구버전 consumer와 신버전 consumer가 동시에 실행된다. 따라서 배포는 반드시 다음을
지킨다.

1. DB 변경은 additive expand migration을 먼저 적용한다.
2. job payload에 `schema_version`을 넣고 신버전 handler는 최소 N-1 payload를 읽는다.
3. 구버전이 모르는 새 job type은 두 host 코드가 모두 교체된 뒤 scheduler에서 활성화한다.
4. consumer container는 `init: true`, `stop_grace_period`와 Docker stop timeout을 명시한다.
5. API health와 별도로 consumer heartbeat, scheduler freshness, queue oldest age를 배포 gate로 검사한다.

배포 중 LLM 호출이 중단되면 provider 비용이 이미 발생했을 수 있고 재시도로 한 번 더 과금될 수 있다.
이를 정확히 한 번으로 만드는 것은 불가능하므로 다음 결과 쓰기를 멱등하게 만든다.

- diary content: `(user_id, diary_date)` unique의 first-writer-wins, generation별 실행 결과는 `async_jobs`
- memory: source message/fact dedup key로 upsert
- payment: RevenueCat event id unique
- 저녁 push: 명시적인 at-most-once marker와 발송 만료시각

---

## 12. 도메인별 비동기 흐름

### 12.1 결제

```text
RevenueCat webhook
  → signature/auth validation
  → raw inbox + outbox commit
  → critical worker claim
  → event별 짧은 transaction으로 정합 반영
  → success/retry/dead
```

결제 처리량과 상태는 다른 워커 health와 분리한다. content worker가 죽어도 결제는 계속 처리된다.

### 12.2 일기

스케줄러는 **`profiles.next_diary_due_at` projection**만 사용해 due user/date의
`GenerateDiary(user, activity_date)`를 enqueue한다. due timezone SQL 대안은 사용하지 않는다.
dedup key는 전 구간에서 §11.2의 canonical formatter
`diary_dedup_key(user_id, activity_date, generation_version)`만 사용한다.

```text
schedule/reconciler → GenerateDiary
  → snapshot transcript and policy in short transaction
  → LLM outside transaction
  → diary first-writer-wins insert + generation result + 필요한 후속 async_jobs in short transaction
  → 대화 memory extraction과 중복 추출하지 않음
```

- 일기 생성이 장기기억 저장을 직접 호출하지 않는다.
- `profiles.next_diary_due_at timestamptz`를 nullable expand migration으로 추가하고 전 행 backfill·검증
  뒤 `NOT NULL`로 바꾼다. `CREATE INDEX profiles_diary_due_idx ON profiles(next_diary_due_at, id)`를 둔다. 가입/백필 시 유저
  timezone의 다음 로컬 04:00을 UTC로 계산한다. scheduler는 `next_diary_due_at <= now()`를 keyset/lock
  batch로 읽는다. 잠근 행의 저장된 due instant를 `D`, 그때 profile에 저장된 timezone을 `Z`라 하면
  enqueue할 대상은 반드시 `activity_date_for(D, Z) - 1 day`다. scheduler 실행 시각 `now()`로 대상 날짜를
  다시 계산하지 않는다. 잡 payload에는 `due_at=D`, `timezone=Z`, `activity_date`를 함께 넣어 감사 가능하게 한다.
- 장기 중단으로 `D <= now()`인 due가 여러 개면 `D`에서 시작해 `Z`의 다음 로컬 04:00 occurrence를
  차례로 계산하고, 각 occurrence마다 위 규칙으로 서로 다른 날짜 job을 enqueue한 뒤 마지막으로 처리한
  occurrence 다음 값까지 `next_diary_due_at`을 advance한다. 한 profile당 한 번에 처리할 occurrence 수와
  전체 batch는 bounded config로 제한하고, 제한에 걸리면 아직 due인 값을 그대로 남겨 다음 scheduler가
  이어간다. 잡 insert와 projection advance는 같은 짧은 트랜잭션이다.
- timezone 변경 API가 profile update와 같은 트랜잭션에서 `next_diary_due_at`을 새 timezone 기준
  `now()` 이후 첫 로컬 04:00으로 다시 계산한다. DST의 nonexistent/ambiguous 시각은 `safe_zone`의
  표준 정책으로 한 번만 UTC에 매핑한다. 변경 경계에서 빠질 수 있는 과거 activity date는 아래
  lookback reconciler가 보충하고, canonical diary dedup key에 대한
  `async_jobs(job_type, dedup_key)` unique가 중복 생성을 막는다.
- 가입 activity date 이전은 enqueue하지 않는다.
- lookback reconciler가 설정된 lookback 범위의 누락 generation result를 보충한다. 범위 길이는 보존·운영
  요구로 정하며 현재 근거 없이 고정하지 않는다.
- `diaries`에는 실제 콘텐츠만 저장한다. “콘텐츠 없음/게이트 미달”은 job result code로 기록한다.
- `diaries`의 현행 UNIQUE `(user_id, diary_date)`는 날짜별 canonical 콘텐츠 한 건 계약으로 유지한다.
  generation version 변경은 새 `async_jobs` 실행을 허용할 뿐 기존 일기 콘텐츠를 update하거나 덮어쓸
  권한이 아니다. handler는 먼저 기존 일기를 확인해 있으면 LLM을 호출하지 않고
  `state='succeeded', result_code='existing_diary_retained'`와 해당 `diary_id`만 결과에 기록한다. 경합 시
  finalize 트랜잭션에서 `INSERT ... ON CONFLICT (user_id, diary_date) DO NOTHING`을 사용하며, insert에 진
  generation도 같은 result code로 끝낸다. 이전 generation이 `no_content`로 끝나 diary 행이 없을 때만
  새 generation이 콘텐츠를 insert할 수 있다. 기존 `source='none'` tombstone은 handler가 암묵적으로
  교체하지 않고 별도 명시적 data migration 대상으로 둔다.
- 생성, repair, self-check는 하나의 무제한 user timeout에 넣지 않고 단계별 deadline을 가진다.
- 선택 단계는 남은 시간이 부족하면 생략할 수 있지만 저장한 품질 상태를 trace에 남긴다.

기존 `582명/37건/1001.8초`는 `worker/tick.py`가 profile 전부를 순회하며 skip, 푸시, 기타 유지보수까지
마친 뒤 기록한 **틱 전체 elapsed**다. 이를 37로 나눈 값은 일기 생성 단가나 처리량이 아니다. 실제 빈 틱은
458명 13.4초, 585명 19.3초로 유저당 각각 약 29ms, 33ms, 즉 약 30ms의 profile scan 비용을 보였다.
따라서 582명 scan은 대략 17초 규모임은 분리할 수 있지만, 나머지 시간도 생성 외 작업을 포함하므로
37건의 생성 latency로 역산하지 않는다.

생성 단가는 **측정 필요**다. 새 handler에서 LLM 생성·repair·self-check·DB finalize를 단계별로 계측하고,
대표 production arrival trace와 latency 전체 분포(최소 p99와 timeout·retry 포함), provider rate limit,
DB pool 대기시간을 재현하는 부하 시험을 한다. `L95`와 `D99`를 곱한 산식은 latency tail 밖의 요청을
포함하지 못하므로 용량 보장의 근거로 쓰지 않는다. content concurrency는 후보값을 올려 가며 반복 시험한다.
합격 기준은 09:00까지 due였던 전체 job을 분모로 `succeeded / due` 하한, `dead / due` 상한,
`cancelled / due` 상한을 각각 따로 두며 세 기준을 모두 만족해야 한다. `dead`나 `cancelled`를 success에
합치거나 terminal 비율을 용량 합격 기준으로 쓰지 않는다. `existing_diary_retained`와 정책상 정상인
`no_content`는 `succeeded`에 포함하되 result code 분포를 함께 보고한다. 각 비율의 승인값, 시험 표본수,
반복 횟수, 안전 여유와 실제 concurrency는 production arrival trace와 제품 SLO로 정해야 하므로 현재
**측정 필요**다. 승인값을 시험 전에 고정한 뒤 provider/DB 제한과 세 비율 gate를 함께 만족하는 최소
concurrency를 택한다. 두 EC2의 안전한 slot 안에서 만족하지 못하면 숫자를 임의로 확정하지 않고 생성
단계 축소나 별도 runner를 검토한다.

### 12.3 푸시

- 제품 푸시는 **저녁 20:00 안부** 하나만 둔다. 아침 일기 푸시용 config, scheduler kind, handler,
  user setting, 멱등 marker와 테스트는 호환 배포 뒤 제거한다. 일기의 오전 09:00 공개 시각과
  `morning` 선발화는 푸시 기능이 아니므로 유지한다.
- handler 순서는 **`evening_chat` 설정 확인 → entitlement 토큰 잔액 가드 → 멱등 send marker 선점 →
  FCM device token·credential 조회 → 만료 재검사와 provider deadline 설정 → 발송**으로 고정한다.
  `tokens_remaining` 조회는 marker보다 앞이고, FCM device token 조회는 marker보다 뒤다. 설정 행이 없으면
  현행처럼 true다. opt-out이면 marker를 선점하지 않고
  `state='succeeded', result_code='user_opted_out'`으로 끝낸다. scheduler의 사전 필터는 부하 절감일 뿐이다.
  handler는 marker 선점 statement의 같은 DB snapshot에서 설정과
  `async_jobs.expires_at > clock_timestamp()`를 다시 확인한다. 설정 재확인과 선점 사이에 commit을 두지
  않으며, 설정이 false이거나 이미 만료됐으면 선점 row가 0건이어야 한다.
- 현행 `gating.resolve`로 `entitlement.tokens_remaining`을 확인한다. `NULL`은 무제한 tier라 통과하고
  `<= 0`이면 marker를 선점하지 않고 `state='succeeded', result_code='tokens_exhausted'`로 끝낸다.
- scheduler가 `available_at`과 `expires_at`을 가진 저녁 due job을 만들며 정확한 로컬 발송 창을 놓친
  job은 늦게 보내지 않고 `cancelled`로 끝낸다.
- claim과 marker 선점만으로 발송 시각을 보장하지 않는다. device token·credential 조회가 끝난 뒤 FCM
  호출 **직전** 짧은 조회에서 `state='running'`, 현재 `lease_owner/lease_token`,
  `lease_until > clock_timestamp()`, `clock_timestamp() < expires_at`을 모두 다시 확인한다. 이 조회에서
  읽은 `provider_start_deadline = min(expires_at, lease_until)`을 고정하고 프로세스의 monotonic
  deadline도 이 절대시각에서 파생한다. 조회 뒤 heartbeat가 lease를 연장해도 이번 provider deadline은
  늘리지 않는다. fencing 또는 이 deadline까지 남은 시간이 없으면 FCM을 호출하지 않고 lease를 아직
  소유한 경우에만 fencing 조건으로
  `state='cancelled', result_code='expired_before_send'`로 끝낸다. 시간이 남으면 FCM gateway에 absolute
  `provider_start_deadline`을 필수 전달하고 플랫폼별 TTL/expiration과 호출 timeout을 그 남은 시간 이하로
  설정한다. gateway는 실제 provider HTTP 요청을 시작하는 마지막 경계에서 monotonic remaining을 다시
  검사하고 0 이하면 요청을 만들지 않는다. gateway가 이 start-deadline 계약을 보장하지 못하거나 provider
  요청 시작 전에 deadline이 지나면 fail-closed로 발송하지 않는다. 따라서 조회 직후 lease가 만료되어
  회수 가능한 상태가 된 기존 worker도 FCM 요청을 시작할 수 없고, marker 선점 뒤 조회 지연으로 창을
  놓친 경우도 당일 손실로 끝난다. 이 경로는 provider delivery의 exactly-once를 주장하지 않는다.
- provider end-to-end idempotency가 없는 현재 경로는 발송 전 DB marker를 원자 선점하는 현행
  at-most-once를 명시적으로 유지해 중복보다 당일 손실을 택한다. marker 선점 후 FCM 실패는 재시도하지
  않고 `state='succeeded', result_code='send_failed_after_claim'`로 관측한다. 제한 재시도로 바꿀지는 §19 제품 결정이며,
  결정 전 exactly-once/saga를 도입하지 않는다.
- FCM credential/token은 프로세스 캐시하고 refresh는 bounded thread에서 실행한다.

### 12.4 메모리

- turn마다 extraction job 행을 Phase B에서 직접 만들어 원본 커밋 뒤 무음 영구유실을 관측·복구할
  내구 신호를 남긴다. 이것은 turn마다 extraction LLM을 실행한다는 뜻이 아니다. worker는 같은 유저의
  가까운 ready job을 bounded coalesce해 한 번 실행할 수 있고, 합쳐진 각 job을 같은 결과 code로 finalize한다.
- extraction job에는 §9.2의 단일 turn source watermark 범위를 넣고, coalesce job은 포함한 turn들의
  최소/최대 watermark와 개별 message id 목록을 보존한다. 중간에 닫힌 범위가 하나라도 있으면 이를
  건너뛰어 부분 publish하지 않고 전체 결과를 `source_range_closed`로 폐기한 뒤 열린 source만 새
  generation job으로 다시 coalesce한다.
- coalesce 후에도 각 source message id를 evidence로 보존한다.
- extract → reconcile → relationship projection은 후속 job을 같은 finalize 트랜잭션에 enqueue한다.
- relationship projection payload는 §9.3에서 증가시킨 `relationship_profile_input_revision`을 가지며, draft에도 같은
  값을 기록한다. finalize/publish 시 현재 revision과 다르면 `stale_profile_input`으로 결과를 폐기한다.
- 한 단계가 실패하면 다음 단계가 먼저 실행되지 않도록 dependency를 dedup key/상태로 명확히 한다.

### 12.5 maintenance

매일 특정 시각에 직접 외부 작업을 하지 않는다. scheduler가 날짜별 멱등 job을 만들고 최근 완료
cursor를 기준으로 누락 날짜를 보충한다. maintenance 실패는 content/critical queue를 막지 않는다.

---

## 13. timeout, deadline, 동시성

모든 timeout은 하나의 monotonic deadline에서 파생한다.

### 대화

- 요청 전체 deadline
- 첫 model step 상한
- tool round 상한 및 tool별 상한
- 최종 model step 예약 시간
- egress validation 상한

`remaining <= reserved_final_budget`이면 새 도구 호출을 시작하지 않는다. LLM SDK timeout만 믿지 않고
HTTP client timeout도 connect/read/write/pool로 구분한다.

### 잡

- job execution timeout < lease duration
- heartbeat interval < lease duration / 2
- handler 외부 호출 timeout의 합은 job timeout 이하
- DB pool concurrency는 queue concurrency의 합보다 작지 않게 실측
- 취소되지 않는 sync SDK는 `to_thread`만으로 hard timeout이 되지 않는다. 반드시 교체하거나 별도
  subprocess에서 실행해 프로세스 단위로 종료한다.

전역 semaphore 하나로 모든 기능을 직렬화하지 않는다. provider별, queue별, tool별 bulkhead를 둔다.

---

## 14. 회계와 quota

`TurnUsage`는 호출별 원본 usage를 가진다.

- provider, model, purpose(chat/tool-final/repair 등)
- uncached input, cached input, cache write, output, reasoning token(지원 시)
- price catalog version, billable amount

메시지 행의 합산 컬럼은 조회 편의를 위한 projection이고, 재감사의 진실 소스는 별도
`llm_call_usage` 행이다. 모델 가격은 코드 상수 대신 effective-dated price catalog로 관리한다.

대화 Phase B는 `messages`, 모든 `llm_call_usage(turn_id, call_id, purpose, provider, model, token
buckets, price_catalog_version, billable_amount)`, 합산 quota, 멱등 응답을 **같은 트랜잭션**에 쓴다.
`call_id`는 턴 안에서 unique이고 retry attempt를 구분한다. 첫 판단·tool-final뿐 아니라 한국어
한자·가나 복원 호출의 성공한 모든 시도도 `purpose='foreign_repair'`로 `TurnUsage`에 더한다. 현재
`_repair_foreign_ko`가 usage를 로그만 남기고 차감하지 않는 동작은 Phase 1 이식 때 제거한다. 외부 호출이
성공했지만 Phase B가 지면 승자 응답에 사용되지 않은 비용이 생길 수 있으며, 이를 saga로 정산하지 않고
`duplicate_agent_execution_cost` telemetry로 관측한다. 비동기 LLM/embedding usage는 해당 job 결과와
같은 finalize 트랜잭션에 기록한다.

`B0` 계측을 위해 경로 전환보다 먼저 durable `legacy_ai_usage_ledger`를 배포한다. 최소 키는
`user_id`, `activity_date`, `operation_id`, `call_id`, `kind(llm|embedding)`,
`purpose(chat_legacy|foreign_repair|mem0_extract|mem0_embed|diary_generate|diary_repair|diary_self_check|diary_translate|other_async_legacy)`,
provider/model, token 또는 provider billable unit bucket, price catalog version, billable amount, success와
`status(started|completed|unknown_usage)`, created_at이며 `(operation_id, call_id, kind)`를 unique로 둔다.
provider 요청 전에 `started` 행을 짧은 트랜잭션으로 쓰고 응답 뒤 usage와 함께 `completed`로 바꾼다.
응답을 잃었거나 usage가 없으면 `unknown_usage` 또는 `started`가 남아 누락으로 계수된다. 현행 일기 뒤
`memory.add_conversation()`이 usage를 반환하지 않는다는 이유로 호출을 누락하지 않는다. legacy mem0의
내부 LLM/embedding provider adapter, legacy chat과 `_repair_foreign_ko`, 일기의 generate/repair/
self-check/translate 모델 adapter, 그 밖의 B0 async provider adapter에서 **모든 요청 시도**를 계측하고
같은 논리 `operation_id`로 귀속한다. retry attempt마다 별도 `call_id`를 쓴다. provider가 usage를 응답하지
않는 embedding은 모델별 공식 tokenizer 또는
provider가 제공하는 billable unit로 입력량을 결정하고 provider billing export와 대사한다. 정확한
billable unit을 재현·대사할 수 없는 provider 경로가 하나라도 있으면 `B0`가 유효하지 않으므로 cost
rollout gate를 열지 않는다. 이 bridge ledger는 §14.1의 비용 view에서 `llm_call_usage` 및 새 비동기
usage와 동일한 price catalog로 합산하고, legacy 경로 종료 뒤에도 baseline 감사 기간 동안 보존한다.

초기 quota 정책은 현행처럼 턴 시작 전 `remaining > 0`, 턴 종료 후 실사용 차감을 유지할 수 있다.
다만 agent runtime은 턴별 hard token/비용 상한을 가져 폭주를 제한한다. 도구 턴이 일반 턴보다 얼마나
비싼지 shadow 환경에서 측정한 뒤 quota 상품 정책을 별도로 결정한다.

### 14.1 현실적인 LLM 비용 예상

비용은 token 수가 아니라 effective-dated price catalog로 환산한 billable 통화액으로 비교한다. rollout의
한 표본 단위는 **production의 test/internal 계정을 제외한 사전 등록 cohort의 user activity-date**다.
cohort 등록 조건은 rollout 전에 고정하고 canary/control을 entitlement별로 무작위 배정한다. 평가 기간의
모든 등록 user-day를 포함하므로 비용 0인 날도 제외하지 않는다. 두 cohort는 정확히 같은 calendar interval을
쓰며, 기간 길이는 필요한 표본수와 요일 효과를 반영한 사전 power analysis로 정한다. 임의의 고정 일수를
근거 없이 쓰거나 canary가 유리한 날만 고르지 않는다.

기존 경로의 user-day 총비용 baseline `B0(u,d)`와 새 경로 `B1(u,d)`는 다음처럼 **같은 비용 표면**으로 정의한다.

```text
B0 = C_chat_legacy + C_foreign_repair_legacy
   + C_mem0_extract_llm + C_mem0_embedding
   + C_diary_legacy + C_other_async_legacy

B1 = C_chat_agent + C_foreign_repair_agent
   + C_extract + C_embedding + C_reflection + C_summary
   + C_diary_new + C_other_async_new
```

여기서 일기 항은 다음과 같고 각 항은 위 ledger의 같은 이름 purpose에서 합산한다.

```text
C_diary_legacy = C_diary_generate + C_diary_repair
               + C_diary_self_check + C_diary_translate
```

`C_chat_legacy`,
`C_foreign_repair_legacy`, mem0 두 항과 `C_other_async_legacy`도 각각 대응 purpose에서 합산한다. 새 legacy
provider 경로를 추가할 때는 B0 식의 비용 항과 purpose allowlist를 같은 migration/release에서 먼저
추가해야 하며 미등록 purpose의 provider 호출은 rollout gate를 닫는다.

현재 비동기 mem0 추출 LLM·embedding과 기존 일기를 `B0` 분모에서 빼지 않는다. 공통 비용은 증분
`B1-B0`에서 상쇄될 수 있지만 총비용 대비 증감률의 분모에는 남는다. attribution 불가능한 shared batch
비용은 source user 수에 대한 고정 규칙으로 양 cohort에 동일 배분하고 규칙/version을 usage ledger에 남긴다.

일반 턴은 모델 호출 1회, 도구 턴은 판단과 최종 응답 호출이 필요하므로 후자가 더 비싸다는 방향만
확실하다. 도구 호출률 `p`, legacy 동기 턴 평균 비용 `C_chat_legacy`, 새 일반/도구 턴 평균 비용을
`C_normal`, `C_tool`이라 하면 새 동기 대화 비용은 `(1-p)C_normal + pC_tool`이다. 현재 문서에 이 세
실측값이 없으므로 동기 배수도 **측정 필요**이며, 근거 없는 배수 범위를 총비용 예측에 사용하지 않는다.

총비용 증감률은 같은 관찰 기간의 cohort 분포로만 계산한다.

```text
Rmean = (sum B1 / canary user-days) / (sum B0 / control user-days) - 1
R50 = percentile50({B1(u,d)}) / percentile50({B0(u,d)}) - 1
R95 = percentile95({B1(u,d)}) / percentile95({B0(u,d)}) - 1
```

rollout cost gate의 primary는 유저당 하루 평균 총비용인 **`Rmean <= 35%`**이고 tail guardrail은
**`R95 <= 35%`**다. 둘 중 하나라도 넘으면 확대를 멈춘다. `R50`은 분포 진단값이지 별도 기준이 아니다.
비용 ledger 완전성(`legacy_ai_usage_ledger`, `llm_call_usage`, 새 비동기 LLM/embedding usage에서 B0/B1
식에 포함되는 모든 provider 요청 시도 대비 `started` 행 누락 0, 완료 응답 대비 usage 확정 누락 0)을
cohort별 user-day 수와 함께 gate한다. `started`/`unknown_usage`는 행 누락은 아니지만 비용을 확정할 수
없는 누락으로 별도 집계하며 하나라도 남으면 cost gate를 열지 않는다.
control의 mean 또는 p95 분모가 0이면 비율 gate는 성립하지 않으므로 확대하지 않고 cohort/기간을 다시
정한다; 임의의 epsilon으로 통과시키지 않는다.
동기 대화 배수를 `B0` 전체에 바로 더하거나, 측정 전 비동기 비율을 가정해 총비용 배수로 재계산하지
않는다. 현재 자료로 `B1/B0` 수치는 **측정 필요**다. canary에서 경로별 실측 후
`B1-B0`을 chat/tool/extraction/embedding/reflection/summary/diary 항으로 분해해 원인을 판단한다.

§12.4처럼 enqueue는 매 턴의 내구 신호로 남기지만, 기억 추출 LLM 실행은 턴마다 하지 않는다. worker는
다음 중 하나가 충족될 때 source job들을 coalesce해 실행한다.

- 일정 메시지 수가 쌓였을 때
- 대화가 일정 시간 끊겼을 때
- “기억해줘”처럼 명시적 가치가 있을 때
- 일일 reconciliation 때 아직 처리하지 않은 turn을 batch로

관계 프로필이 기존 “최근 기억 20개 전체 주입”을 대체하면 입력 토큰이 줄어 agentic 호출 증가분을
일부 상쇄할 수 있다. 따라서 비용 목표는 단순히 “새 호출 수”가 아니라 **유저당 하루 총 billable**로
검증한다.

### 14.2 현실적인 응답시간 예상

| 경로 | 추가 지연 목표 |
|---|---:|
| 일반 대화 | 기존 대비 100~300ms 이내 |
| Postgres 읽기 도구 사용 | 보통 2~6초 추가 |
| 도구 일부 실패 | 남은 deadline 안에서 최종 답변, 무제한 재시도 없음 |

도구 턴이 느린 주된 원인은 DB가 아니라 두 번째 LLM 호출이다. 따라서 일기·루틴 DB 조회를 50ms
줄이는 것보다 불필요한 도구 호출을 줄이는 편이 훨씬 중요하다. 제품 목표는 일반 턴의 85~90%를
단일 LLM 호출로 유지하는 것이다. 정확한 p95는 shadow 트래픽으로 측정하며, 전체 deadline을 넘길
것 같으면 도구를 시작하지 않고 기본 context로 답한다.

### 14.3 인프라 비용

초기 버전에는 Redis, Kafka, 별도 벡터 DB, queue별 EC2를 추가하지 않는다.

- 현재 Postgres에 `outbox_events`, `async_jobs`를 추가
- 현재 두 EC2 각각에서 API + consumer 1개, marker host에서만 짧은 scheduler timer 실행
- pgvector도 현재 Postgres를 유지
- mem0의 로컬 SQLite write path는 제거

따라서 초기 고정 인프라 비용 증가는 거의 없고, 실제 증가는 LLM·embedding 사용량과 DB 저장량이다.
queue 적체나 CPU 사용량이 실측 임계치를 넘을 때만 runner 인스턴스를 추가한다.

### 14.4 개발 기간 예상

백엔드 개발자 1명 기준의 현실적인 범위다. 운영 이슈와 리뷰 시간을 포함하며 동시에 다른 기능을
진행하면 더 길어진다.

| 범위 | 예상 기간 | 결과 |
|---|---:|---|
| 워커 P0 안정화 | 2~3주 | Postgres job queue, 결제 분리, timeout/retry/관측 |
| 대화 경계 + 현재 상태 | 1.5~2.5주 | 기존 2단계 상태머신에 gateway/tool loop/usage·보호장치 이식 |
| read tool MVP | 1~2주 | 일기·루틴·기억 검색, bounded tool loop |
| 새 메모리 모델 | 4~6주 | fact/evidence/insight/profile, snapshot cutover, forget, backfill/eval |
| DDL 작성·적용 | — | `db/migrations/` 작성 → dev 적용·검증 → prod 적용(머지 전) |
| 롤링·canary·부하·장애 검증 | 2~3주 | N/N-1, EC2 2대 순차 배포, 5→25→100% 관찰 창 |

겹칠 수 있는 작업을 감안해도 **쓸 수 있는 MVP는 약 6~9주**, 메모리 전환과 외부 DDL·롤링 검증까지
포함한 전체 완료는 **약 11~16주**가 현실적이다. 외부 DDL 선반영과 롤링 관찰 시간은 구현 완료에
흡수해 0일로 계산하지 않는다.

---

## 15. 안전과 데이터 보호

- 안전 라우팅은 현재 유저 발화와 현재 turn metadata로 먼저 수행한다.
- 검색 결과는 prompt injection을 포함할 수 있는 비신뢰 데이터다.
- 모든 tool query는 service-role RLS가 아니라 명시적 user predicate와 integration test가 보안 경계다.
- 로그에 메시지, 기억, 일기 본문을 기본 기록하지 않는다. trace는 id, 길이, hash, 분류값만 기록한다.
- Relationship Profile과 memory는 민감 projection으로 분류해 클라이언트 직접 접근을 차단한다.
- 데이터 보존 기간, forget 범위, 탈퇴 삭제 완료 SLO를 문서화한다.
- 모델 provider에 전송되는 데이터 종류와 목적을 inventory로 유지한다.

---

## 16. 관측과 SLO

### 대화 지표

- end-to-end p50/p95/p99
- first/final model latency, tool latency/status
- tool call rate, tool usefulness, no-tool fallback rate
- prompt/cache token bucket, turn cost
- idempotency replay/duplicate agent execution
- safety route와 output policy failure

### 잡 지표

- queue별 ready/running/dead 수
- oldest ready age, schedule lag, runtime histogram
- attempts, lease expiry/reclaim, timeout, cancelled/expired
- handler error code, throughput
- payment/deletion outbox undispatched oldest age

### 도메인 정합 지표

- turn committed인데 extraction job이 없는 건수
- relationship profile source version lag
- forgotten fact가 검색된 건수(목표 0)
- payment inbox unresolved age

`worker_last_success` 하나로 전체 시스템을 정상 판정하지 않는다. 큐별 freshness와 domain invariant를
각각 health로 노출한다. deadman은 “프로세스가 돌았다”가 아니라 “기대된 작업이 기한 안에 수렴했다”를
검사한다.

---

## 17. 테스트와 평가 게이트

### 결정적 테스트

- phase A/B 사이에 열린 transaction이 없는지
- duplicate idempotency request에서 message/usage/direct job enqueue가 1회인지
- 멱등 replay가 schema 불일치 시 행을 보존하고 fail-closed하며 최초 렌더값을 그대로 반환하는지
- nickname이 message/fact/insight/profile/checkpoint 어느 저장 표면에도 평문으로 남지 않고 개명 후
  일반 조회에서 조사까지 다시 계산되는지
- greeting_id 소유권·미커밋 검증, 경합 시 단일 커밋, 실제 커밋분만 response echo하는지
- ko egress가 메타 제거→한자·가나 복원→부호·물음표 정제→placeholder 순서를 지키는지
- BCP 47 ko/ja/en 페르소나와 서버 고정문구 fallback 회귀 테스트
- 모든 tool query의 cross-user negative test
- tool partial failure에도 call_id가 완결되는지
- deadline 부족 시 final budget을 보존하는지
- job crash 후 lease reclaim과 fencing이 동작하는지
- 세 번째 claim 실패 및 세 번째 claim 중 crash 후 reaper에서 dead가 되는지
- 같은 queue에 retryable expired-lease backlog가 계속 유입돼도 reaper의 terminal 전이 batch가 먼저
  실행되고, terminal backlog 중에도 별도 retryable batch가 매 주기 실행되는지
- queue별 reaper가 batch limit을 넘지 않고 content 대량 backlog 중에도 critical/notification 행을
  독립 회수하는지
- 반복 job의 새 generation 실행, 같은 generation dedup, replay/audit 전이가 정확한지
- diary의 새 generation과 경합 실행이 기존 `(user_id, diary_date)` 콘텐츠를 바꾸지 않고
  `existing_diary_retained` result만 남기며, 이전 generation이 no-content일 때만 새 콘텐츠를 넣는지
- normalized cutover 뒤 빈 결과·저장소 장애에서도 legacy memory_text를 재사용하지 않는지
- 구버전 `_save_memory` upsert를 재현해도 normalized 행의 legacy snapshot이 trigger로 계속 NULL이고,
  두 API/consumer release gate 전에는 cutover가 거부되는지
- push가 claim 뒤 설정·일기·entitlement·device token·credential 조회 중 만료되는 각 경계에서 marker를
  선점하지 않거나 `expired_before_send`로 끝나고 FCM을 호출하지 않는지, provider TTL/expiration도 job
  deadline을 넘지 않는지, 발송 직전 조회 뒤 `expires_at`보다 `lease_until`이 먼저 도달하면 gateway가
  `min(expires_at, lease_until)` 이후 provider 요청 시작을 거부하는지
- 아침 푸시 job·handler·설정 경로가 존재하지 않고 scheduler가 아침 notification job을 만들지 않는지
- 저녁 opt-out과 `tokens_remaining<=0`에서 marker를 선점하지 않는지, 설정 확인→entitlement token
  가드→marker 선점→FCM device token·credential 조회→deadline fencing→발송 순서를 지키는지
- 다른 user의 fact를 `memory_insight_sources`나 `relationship_profile_sources`에 insert하면 복합 FK로
  실패하고, JSON/edge 불일치·타 유저 source·forgotten source·stale generation profile publish가 모두
  거부되는지
- 같은 source watermark에서 forget·reflection·maintenance·extract 반영으로 profile 입력 revision이
  바뀔 때마다 서로 다른 canonical relationship refresh dedup key가 생성되고, 이전 revision 결과의
  publish가 `stale_profile_input`으로 거부되는지
- 같은 profile/item/fact edge와 profile/item/insight edge의 중복 insert가 각각 partial unique index로
  실패하는지
- 새 profile publish 경합에서 기존 `(user_id, locale)` published 행이 같은 트랜잭션 안에서 먼저
  `superseded`가 되고 새 draft 하나만 `published`가 되는지, 중간 statement 장애 시 둘 다 rollback되는지
- forget과 running extraction 경합에서 fact가 부활하지 않고, 파생 insight·기존 published profile
  항목·검색 결과가 즉시 비노출되는지
- forget marker retention 종료의 각 statement 사이에 장애를 주입해도 transaction rollback으로 marker와
  fact가 함께 남고, 성공 시 evidence/fact 삭제 뒤 marker가 마지막에 삭제되는지
- retention 성공으로 marker가 삭제된 뒤에도 닫힌 source watermark와 겹치는 일반 enqueue·운영 replay가
  `source_range_closed`로 거부되고, 이미 running인 job의 finalize도 fact/profile을 publish하지 않는지

### 모델 평가

- 캐릭터 정체성/말투 유지
- 신규 유저와 장기 유저의 관계감 차이
- 관련 기억 사용과 허위 기억률
- 모순된 최신 사실 우선
- 일기/루틴/장착 상태 질문의 도구 선택 정확도
- 도구를 부를 필요 없는 대화에서 과호출하지 않는지
- 한국어/일본어/영어 언어 순수성
- prompt injection과 cross-context leakage
- 안전 replay

Golden set은 기대 답변 한 문장만 비교하지 않는다. route, tool calls, cited fact ids, safety policy,
persona rubric, latency/cost를 함께 기록한다.

### 부하·장애 주입

- LLM 429/timeout, pgvector 지연, FCM 지연
- worker mid-call kill, DB failover, lease expiry
- 특정 큐 backlog가 다른 큐에 영향을 주지 않는지
- 수천 user timezone due job enqueue 성능
- 장기 중단의 각 due instant에서 activity date를 도출하는지, timezone 변경과
  `next_diary_due_at` 재계산 및 lookback 중복 방지
- payment/deletion outbox dispatcher 중복 전달

---

## 18. 마이그레이션 순서

기존 구조를 한 번에 교체하지 않는다. 새 경계를 먼저 만들고 트래픽을 옮긴다.

### Phase 0 — 운영 안정화

1. 경로 변경 전에 §14의 `legacy_ai_usage_ledger`와 legacy chat·foreign repair·mem0 LLM/embedding·
   일기 generate/repair/self-check/translate·other async adapter 계측을 선배포한다. provider billing
   export 대사와 B0 대상 요청 시도 대비 started 행 누락 0, 완료 응답 대비 usage 확정 누락 0을 확인하고,
   rollout과 같은 cohort 정의로 유효한 `B0` 관찰 구간을 확보하기 전에는 cost gate나 legacy 경로 제거를
   시작하지 않는다.
2. 결제 inbox 소비자를 현재 전역 틱에서 분리한다.
3. `async_jobs`와 job runner를 만들고 일기/푸시를 job 단위로 옮긴다.
4. 일기 후 인라인 mem0 호출을 없애고 `async_jobs`에 직접 enqueue한다.
5. `next_diary_due_at` DDL·인덱스를 dev→prod 순으로 적용하고 backfill한 뒤 scheduler를 전환한다.
6. 저녁 푸시의 만료 창과 발송 직전 deadline fencing을 배포하고 아침 푸시 enqueue·handler 경로는
   비활성 호환 배포 후 제거한다.
7. 큐별 health, oldest age, dead job 관측을 먼저 배포한다.

완료 조건: content worker timeout이 결제 처리에 영향을 주지 않고, 워커 kill 후 잡이 자동 회수된다.

### Phase 1 — 대화 application 경계

현행 `post_message`는 이미 SOMA-374의 read-only Phase 1 → commit → 외부 mem0/LLM/egress → 유저락
재획득 Phase 2 상태머신이다. 새로 분리하는 작업으로 산정하지 않고 다음 **실제 변경분**만 이식한다.

1. Phase 1 snapshot에 현재 시각 bucket, 첫 대화 여부, 착용·테마·작은 루틴 상태, published
   relationship version/locale을 추가하되 commit 후 ORM 접근 금지를 유지한다.
2. 기존 외부 I/O 구간에 `ModelGateway`, typed transcript, bounded read-only tool loop와 final budget을
   이식한다. 기존 memory reload와 §6.6 egress chain은 순서를 보존한다.
3. Phase 2 저장을 repository로 추출하고 모든 hop·foreign repair의 `llm_call_usage`, 합산 quota,
   extraction/memory job 직접 enqueue를 메시지·멱등 응답과 같은 트랜잭션에 넣는다.
4. greeting 검증/재조회/단일 커밋/echo, placeholder write/render, BCP 47 분기, 멱등 schema fail-closed와
   당시 렌더값 고정을 façade 안에 남기지 않고 application/repository 계약 테스트로 옮긴다.

완료 조건: §6.6 회귀 테스트와 기존 chat 계약 테스트 통과, LLM·tool 대기 중 DB connection 0,
foreign repair까지 호출별 usage 합산, direct job enqueue 원자성을 부하/장애 테스트로 확인한다.

### Phase 2 — 메모리 정규화

1. fact/evidence/insight/profile과 `memory_insight_sources`, `relationship_profile_sources`,
   `memory_source_turns`, `memory_source_closures`를 additive migration하고 §9.2의 `(user_id,id)`
   UNIQUE·복합 FK·deferred marker FK·CHECK·partial unique index를 `db/migrations/`에
   적용한다. cross-user negative insert와 profile publish validator가 통과하기 전에는 shadow profile도
   publish하지 않는다.
2. `db/migrations/`가 `chat_contexts.memory_mode`, `memory_generation`, `memory_source_watermark`,
   `relationship_profile_input_revision`과 §9.5의
   normalized legacy-write 차단 trigger를 선반영한다. mode-aware API/consumer는 legacy cohort만
   snapshot을 쓰도록 배포하고 기존 mem0 입력을 shadow extraction/reconcile에 투영한다. 모든 자연어
   backfill writer에 placeholder 변환을 적용한다.
3. locale별 Relationship Profile을 shadow 생성하고 기존 기억 snapshot과 품질 비교한다.
4. 두 EC2의 모든 API/consumer heartbeat가 mode-aware release이고 구버전 프로세스가 0임을 확인하는
   cutover gate를 통과한다. N/N-1 롤링 중에는 어떤 cohort도 cutover하지 않으며 trigger로 accidental
   normalized write를 차단한다.
5. gate 뒤 prepared user만 §9.5의 단일 cutover 트랜잭션으로 normalized mode에 전환하면서 legacy
   `memory_text`를 즉시 NULL로 만든다. 이 시점부터 장애·빈 결과 fallback도 normalized 경로 안에서만 한다.
6. 기존 source turn watermark backfill과 user별 최대값을 검증하고, 일반 enqueue·운영 replay·finalize의
   closure guard를 모두 배포한다. normalized cutover, stale-generation, marker 삭제 후 closed-range replay
   거부 테스트가 완료된 cohort에서만 제품 결정에 따라 forget을 활성화한다. legacy cohort에는 forget
   성공을 응답하지 않는다.

완료 조건: §9.2의 두 provenance edge에서 cross-user insert/publish 0, forget 부활·재노출 0,
retention 장애 주입에서 fact 단독 삭제와 marker 선삭제 0, marker 삭제 뒤 closed source enqueue/replay/
finalize 허용 0, 기존 대비 기억 평가 비열화 없음. 이 조건과 normalized cutover가 함께 충족될 때
forget 부분판정을 닫는다.

### Phase 3 — read tools

1. `search_memory` shadow 실행
2. diary/routine tool 추가
3. 5% → 25% → 100% canary

완료 조건: p95, 턴 비용, tool usefulness, 안전 평가 기준 충족.

### Phase 4 — summary와 쓰기 의도

1. conversation checkpoint 활성화
2. §19 결정이 끝난 범위에서 pin/forget memory intent 활성화
3. 이미 Phase 2 cutover에서 사용 중단·행별 폐기한 legacy 컬럼과 SQLite history 경로를 contract
   migration으로 제거한다. 기능적 forget보다 뒤에 snapshot 폐기를 미루지 않는다.

각 단계는 독립 kill switch를 가진다. old-path fallback은 아직 cutover하지 않은 cohort에만 허용하며,
normalized memory cohort에는 §9.5에 따라 legacy fallback을 금지한다. DB migration은 expand → migrate →
contract 순서로 하며 old reader가 존재하는 동안 destructive schema 변경을 하지 않는다.

---

## 19. 구현 전에 확정해야 할 제품 결정

다음은 아키텍처가 임의로 정하면 안 되는 항목이다.

1. 어떤 유저 표현을 장기기억 대상으로 볼지와 보존 기간
2. “기억해줘/잊어줘”의 확인 UX와 범위
3. 캐피가 일기 내용을 먼저 언급할 수 있는지
4. 루틴을 조회만 할지 대화에서 완료 처리까지 허용할지
5. 안전 경로의 구체 정책과 외부 도움 안내 원칙
6. 기억·일기 데이터의 모델 provider 전송 동의와 탈퇴 삭제 SLO
7. agentic turn의 비용을 기존 일일 quota에 어떻게 반영할지
8. 푸시 provider가 end-to-end idempotency를 제공하지 않을 때 현행 at-most-once(중복 방지·당일 손실
   수용)를 유지할지, 제한 재시도(중복 가능)를 택할지와 종류별 허용 손실률

이 결정 전에도 Phase 0~2의 기반 구조와 shadow 평가는 진행할 수 있다.

---

## 20. Definition of Done

새 구조는 파일을 나눈 것만으로 완료가 아니다. 다음이 모두 성립해야 한다.

- 대화 LLM/도구 구간의 열린 DB 트랜잭션 0
- 메시지·호출별 usage·quota·멱등 응답·기억 job 직접 enqueue 원자 확정
- 결제·일기·메모리·푸시의 독립 실패 도메인
- 모든 잡의 멱등, lease reclaim, bounded retry, dead visibility
- 캐릭터 페르소나와 관계 프로필의 명확한 분리
- 사실과 파생 통찰의 provenance 분리 및 insight/profile source의 same-user DB 강제·publish 검증
- 타 유저 데이터 노출 0, forget 후 재노출 0
- normalized cutover/forget 뒤 legacy 문자열 snapshot fallback 0
- message·fact·insight·relationship profile·summary 저장 표면의 현재 닉네임 평문 0
- greeting 단일 커밋·echo, egress 순서, BCP 47 분기, 멱등 fail-closed/당시 렌더값 회귀 100% 통과
- 누락 일기와 maintenance가 lookback 안에서 자동 수렴
- 아침 푸시 enqueue·발송 0과 관련 실행 경로 제거
- 저녁 푸시의 at-most-once 또는 제한 재시도 정책에 대한 중복·손실 장애 테스트와 운영 SLO 승인
- tool partial failure와 provider timeout에서도 정상 최종 응답 또는 명시적 오류
- 품질, 지연, 비용, 안전, 큐 적체를 배포 전후 비교 가능

이 조건을 만족할 때 캐피는 단순히 도구를 호출하는 챗봇이 아니라, 캐릭터 일관성과 유저 관계를
지키면서도 운영 장애에 견디는 에이전틱 컴패니언이 된다.
