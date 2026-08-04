# 캐피 대화 새 구조 — 구현 명세서

> **구현 상태(2026-08-04, 재개방)**: W1~W11의 기존 코드와 저장 구조는 회귀 기준으로 구현됐지만,
> 대화 중심 회상 전체 범위에 대한 독립 코드·DB·API 적대 검토에서 활성화 차단점이 발견됐다.
> 따라서 아래 W1~W11의 `완료` 표시는 기존 기반선만 뜻하며, 이 문서 §0.7의 통합 완료 게이트를
> 통과하기 전에는 새 기억 구조를 완료 또는 운영 가능으로 표시하지 않는다. 현재 운영 기술서는
> `ARCHITECTURE.md`, 데이터 계약은 `ERD.md`, HTTP 계약은 `openapi/openapi.yaml`이 소유한다.
> 이 문서의 cohort·`memory_mode`·이전 문자열 저장소 전환 절차는 구현 중 검토 이력이며 최종 구조가 아니다.
> 최종 기억 구조는 전 사용자 공통 PostgreSQL 정규화 사실/근거/통찰 + 같은 DB의 pgvector 검색 인덱스다.
> 검색 방식은 기억=pgvector, 일기=pg_trgm으로 확정했고, 망각은 확인된 API/최종 agent intent에서 즉시 적용한다.
>
> **새 구현 계약(2026-08-04):** 자연스러운 대화 회상의 규범 설계는
> `agentic-chat-ARCHITECTURE.md` §0, 정확한 구현 범위·순서·완료 조건은 이 문서 §0.7이 소유한다.
> 설계 변경은 별도 저장소나 외부 벡터 DB로 미루지 않는다. PostgreSQL 원본과 같은 DB의 pgvector
> projection을 단일 구조로 사용하고, Dev에서 end-to-end 계약까지 닫는다.
>
> **이 문서의 원래 목적**: 세션이나 문맥이 사라져도 이 문서 하나로 구현에 착수할 수 있게 한다.
> 설계 의도와 근거는 짝 문서 `agentic-chat-ARCHITECTURE.md`에 있지만, 구현자는 그 문서를 열 필요가 없다.
> 필요한 계약·DDL·초기값·전이·게이트는 이 문서가 전부 소유하며 구현 판단은 이 문서만으로 한다.
> **날짜·기간은 쓰지 않는다.** 작업 단위와 의존 순서만 정의한다.
> 작성 기준: 2026-08-03 main(`21b1f56`), OpenAI SDK 2.44.0.

---

## 0. 시작 전 반드시 읽을 것

### 0.1 하드 제약 (제품 요구, 타협 불가)

| 제약 | 값 | 검증 방법 |
|---|---|---|
| 응답 시간 | **최대 5초** (도구 사용 턴 포함) | W2 계측의 end-to-end p95 |
| 대화 턴 수 | 현재 대비 **감소 20% 이내** | 턴당 billable 평균 비교 (W1 계측 기준) |

이 두 값이 작업 순서를 지배한다. **W2(계측) 이전에는 두 제약의 만족 여부를 판정할 수 없다.**
현재 프로덕션의 chat p50/p95는 측정된 적이 없다. 따라서 도구 런타임(W4·W5) 활성화 여부는
W2 결과를 보고 결정한다. 측정 없이 도구 루프를 켜지 않는다.

### 0.2 절대 깨뜨리면 안 되는 현행 불변식

새 코드가 이 다섯 가지를 하나라도 잃으면 회귀다. 각 작업 단위의 "보존" 항목에서 다시 명시한다.

1. **닉네임 비식별 저장** — LLM 입력에는 실명, 저장 직전 `naming.to_placeholder`, 출력 시
   `naming.render`. 새로 만드는 어떤 저장 표면에도 실명 스템을 넣지 않는다.
   (근거: SOMA-321/322/365, 프로덕션 백필 2회 실행됨)
2. **선발화 단일 커밋** — `greeting_id` 검증 → Phase 2에서 유저 메시지보다 먼저 1회 insert →
   실제 커밋했을 때만 응답에 echo.
3. **egress 순서 고정** — 메타 프리앰블 제거 → 한자·가나 복원 → 부호·물음표 정제 → placeholder 변환.
4. **i18n** — 서버 고정문구는 `i18n.resolve` 버킷(None→ko, 미지원→en), LLM은 원시 BCP 47 태그를
   보존한 `system_prompt`의 ko/ja/그 외 분기를 쓴다. 새 경로에 `language == "ko"` 하드코딩 금지.
5. **멱등 응답** — 저장·replay 양쪽에서 `PostMessageResponse` 스키마 검증, 비호환 행은 fail-closed 500.
   JSONB에는 Phase 2 당시 렌더값을 저장하고 replay에서 재렌더하지 않는다.

### 0.3 SOMA-374 상태머신 (이미 구현돼 있음 — 새로 만들지 말 것)

`app/services/chat.py:488` `post_message`는 **이미** 다음 구조다. 아래 Phase 1을 다시
"Phase A/B로 분리한다"고 읽으면 안 된다. 실제 작업은 이 구조 안에 새 단계를 끼우는 것이다.

```
Phase 1 (chat.py:512-567): 유저 advisory lock + read-only + 스칼라 캡처 → commit(락·커넥션 반납)
LLM 구간 (chat.py:569-620): DB 커넥션 0. mem0 갱신·LLM 호출·egress 백스톱
Phase 2 (chat.py:622-714): 재락 + 멱등 재확인 + 저장 + _accumulate_tokens + 멱등 응답
```

### 0.4 건드릴 파일 지도

| 파일 | 현재 역할 | 이 명세에서의 변경 |
|---|---|---|
| `app/services/chat.py` | 220줄 `post_message` 단일 함수 | 단계 분해, 상주 컨텍스트, TurnUsage, 도구 루프 위임 |
| `app/services/llm.py` | `generate()` 단발, 툴콜 미지원 | `generate_step()` 신설(기존 `generate` 유지) |
| `app/services/prompts.py` | 페르소나 ko/ja, 출력 체크 | 도구 정의·관계 프로필 블록 추가 |
| `app/services/memory.py` | mem0 읽기/쓰기 단일 파일 | 패키지로 분해(façade 유지) |
| `app/config.py` | 설정 | 회계 가중치·도구·데드라인 키 추가 |
| `worker/tick.py` | 전역 15분 틱 | 잡 소비자로 이관(W7 이후) |
| `app/services/notify.py` | 아침·저녁 푸시 | 아침 경로 제거(별건) |

---

### 0.5 다국어 지침 (한국어·일본어·영어 완전 지원)

**세 언어 모두 1급이다.** 한국어를 먼저 만들고 나중에 번역하는 방식이 아니라, 각 작업 단위의
완료 조건에 ko·ja·en 검증이 포함된다. (근거: SOMA-344·346·361·365, 해외 출시 RELEASE 3)

#### 언어 해석 — 서버 고정문구와 LLM 응답은 별도 경로

언어 경로를 하나의 폴백 규칙으로 합치지 않는다. **서버 고정문구**(알림·에러·라벨·카탈로그)는
`app/services/i18n.py`의 아래 세 진입점만 사용하며, 미지원 언어는 `en` 버킷으로 폴백한다. 새 코드에서
직접 문자열 비교를 하지 않는다.

| 함수 | 용도 | 폴백 |
|---|---|---|
| `resolve(language)` | BCP 47 태그 → 콘텐츠 버킷 `ko\|en\|ja` | `None`→ko, 미지원(zh 등)→en |
| `pick(table, language)` | 코드에 박힌 `{"ko":…, "en":…, "ja":…}` 표에서 선택 | 정확일치 우선(빈 값도 유효) → en → ko |
| `localized_name(name_i18n, language, default, kind=, key=)` | DB 카탈로그 이름(`products.name_i18n` 등) | resolve → en → ko → `default`(원문). 빈 문자열은 **누락**으로 보고 다음 폴백 |

- **금지**: `language == "ko"`, `lang.startswith("ko")` 같은 정확일치 비교. `ko-KR`이 영어로 새는
  버그(SOMA-344)의 원인이었고 `i18n.resolve`가 그걸 막기 위해 존재한다.
- 한국어 여부 게이팅이 필요하면 `i18n.is_korean(language)`를 쓴다.

**LLM 응답 언어**는 이 버킷 경로가 아니다. `prompts.system_prompt`는 `profile.language`의 원시 BCP 47
태그를 그대로 유지한다. ko 계열은 `CAPI_PERSONA` + 한글 강제 + `_OUTPUT_CHECK`, ja 계열은
`CAPI_PERSONA_JA` + 한글만 배제 + `_OUTPUT_CHECK_JA`, 그 외는 한국어 페르소나 본문 + **해당 원시 태그의
언어로 답하라는 지시** + 한글·한자·타 스크립트 배제를 사용한다. 따라서 zh 사용자를 `en`으로 폴백시키지
않고 중국어로 답하게 지시하는 것이 의도된 동작이다.

#### 작업 단위별 i18n 요구

| 단위 | 요구 |
|---|---|
| **W3 상주 컨텍스트** | 라벨(`[지금]`·`[모습]`·`[루틴]`)은 `pick`으로 3언어 제공. 착용 아이템·테마 이름은 **`localized_name(products.name_i18n, …)`** 사용(원문 직접 노출 금지). 시간대·활동 버킷 문구도 3언어 |
| **W4 `generate_step`** | 툴 정의(name·description·parameter description)는 **ASCII 고정 영어**. 언어별로 분기하면 프리픽스 캐시가 언어마다 쪼개지고 ja 세션의 스크립트 순수성(한글 배제)이 깨진다 |
| **W5 도구 루프** | `ToolContext.language`를 모든 도구에 전달. 최종 응답 언어는 페르소나 분기가 결정하며 도구 결과가 언어를 바꾸지 못한다 |
| **W6 도구 반환** | 유저가 쓴 원문(일기 발췌·루틴 이름)은 **번역하지 않고 그대로** 반환. 카탈로그성 이름만 `localized_name`. 날짜·수량 포맷은 유저 locale |
| **W8 메모리 추출** | 추출 지침은 실제 발화 언어가 아니라 **`i18n.resolve(profile.language)` 버킷(ko\|en\|ja)** 으로 준다(현행 `memory._instructions_for`가 그렇게 동작 — 미설정→ko, 미지원(zh 등)→en). `canonical_text`도 그 **버킷 언어**로 추출·저장한다. 예: ko 프로필 사용자가 영어로 말해도 기억은 한국어이고, **zh 프로필 사용자의 기억은 en으로 저장된다**(원시 태그가 아니라 버킷). LLM 응답 언어(원시 BCP 47)와 다른 규칙임에 주의. 새 정규화 구조도 이 규칙을 승계한다. CJK 닉네임 마스킹 조건(SOMA-365)도 유지 |
| **W9 관계 프로필** | `relationship_profiles.locale`별로 별도 행. 렌더 문자열이 해당 언어여야 한다. 유저가 언어를 바꾸면 새 locale 행을 만들고 기존 행을 대체하지 않는다 |
| **W11 대화 요약** | `profile.language`로 생성·저장 |
| **egress 백스톱** | 한국어 전용 단계(`strip_leading_meta`·`_repair_foreign_ko`·`_fix_qmarks`)는 **ko에서만** 발동. ja는 한자·가나가 정상이므로 복원기를 절대 태우지 않는다. `strip_symbols`의 하이픈 처리는 en에서 유지 |
| **페르소나** | ko=`CAPI_PERSONA`+한글 강제+`_OUTPUT_CHECK`, ja=`CAPI_PERSONA_JA`(번역이 아닌 네이티브 작성 — キャピー·1인칭 ぼく·タメ口)+한글만 배제+`_OUTPUT_CHECK_JA`, 그 외=`CAPI_PERSONA` 본문+원시 BCP 47 태그가 가리키는 언어 지시+한글·한자·타 스크립트 배제. **ja 페르소나를 ko의 기계번역으로 대체하지 않는다** |

#### 새 서버 고정문구를 추가할 때

에러 메시지·알림 문구·라벨을 새로 만들면 **ko·en·ja 세 값을 동시에** 넣는다. 한 언어만 넣고
나중에 채우는 방식을 금지한다(SOMA-361에서 ja 키 누락이 한국어 노출로 뒤집힌 이력).

```python
_LABEL = {"ko": "지금", "en": "Now", "ja": "いま"}
label = i18n.pick(_LABEL, language)
```

#### 테스트·검증 (각 작업 단위 완료 조건에 포함)

1. **3언어 각각 실행** — ko·ja·en 유저로 같은 시나리오를 돌린다. 대화 검증은 메모리
   `dialogue-eval-lang-split` 규칙에 따라 **언어별로 분리 실측**한다(합산 금지).
2. **언어 순수성** — ko 응답에 한자·가나 없음, ja 응답에 한글 없음, en 응답에 CJK 없음.
3. **폴백 경로** — **서버 고정문구에 한해** `zh-Hant-TW` 같은 미지원 태그가 en 버킷으로 가고
   `ko-KR`이 ko 버킷으로 가는지 확인한다. 별도로 LLM은 `zh-Hant-TW` 원시 태그를 유지해 중국어 응답을
   지시하는지 확인한다.
4. **카탈로그 누락** — 요청 버킷 키가 없거나 빈 문자열이면 `resolve → en → ko → default(원문)` 순서로
   다음 값을 찾고, 빈 문자열이 노출되지 않는지 확인한다.
5. **닉네임 렌더** — ko 조사 재계산, ja CJK 조건부 마스킹이 각 언어에서 정상인지.

---

### 0.6 DB 마이그레이션 절차

DDL은 **우리가 직접 관리한다.** 외부 담당자에게 넘기지 않는다.

```
1. db/migrations/YYYYMMDD_<이름>.sql 작성  (additive만 — 구버전이 읽는 동안 파괴적 변경 금지)
2. dev 적용·검증   python db/apply.py db/migrations/<파일>.sql            # dev, dry-run (기본)
                   python db/apply.py db/migrations/<파일>.sql --commit    # dev 실반영
                   PYTHONPATH=. python db/verify.py                        # 모델↔실DB 컬럼 일치
3. dev 브랜치 push → 자동배포(dev.moly.asia)에서 동작 확인
4. prod 적용       python db/apply.py db/migrations/<파일>.sql --env prod           # dry-run 먼저
                   python db/apply.py db/migrations/<파일>.sql --env prod --commit  # PROD 실반영
                   PYTHONPATH=. python db/verify.py --env prod
5. main 머지       ← 머지가 곧 prod 배포이므로 반드시 4번 다음
```

**핵심 규칙**: 머지=자동배포다. **마이그레이션이 머지보다 먼저** 적용돼야 한다.
순서가 뒤집혀 프로덕션 대화가 다운된 사고가 있었다(메모리 `migration-before-merge`).

**환경 파일** (둘 다 gitignore `.env.*`, 2026-08-03 기준):
- **`.env` = DEV**(`wywzjslvxwttxkecbyis`) — **기본값**. 인자 없이 실행하면 여기로 간다.
- **`.env.prod` = PROD**(`qkgjlgzsharnilxnkytd`) — `--env prod` 를 명시해야만 선택된다.
- 대상 해석은 공용 모듈 **`db/envfile.py`**(별칭 `dev`/`prod`, `load_conn`, `announce`)가 담당하며
  `db/apply.py`·`db/verify.py`·`scripts/seed_capi_diaries.py`가 모두 이걸 쓴다.
  앱은 `app/config.py`의 `env_file=os.getenv("MOLY_ENV_FILE", ".env")`로 같은 규칙을 따른다.
- **실행 시 대상을 stderr에 항상 출력**한다:
  `[대상] PROD ⚠️ 실유저 | env=.env.prod | project=qkgj… | COMMIT`. prod+commit이면 경고가 한 줄 더 붙는다.
- 설계 의도: "틀렸을 때 안전한 쪽이 기본". 못 찾은/임시 스크립트가 `open(".env")`를 해도 dev로 간다.
- `apply.py`는 파일 안의 `BEGIN;`/`COMMIT;`을 제거하고 자체 트랜잭션으로 감싼다. dry-run이 기본이라
  `--commit` 없이 먼저 돌려 오류를 확인한다.
- 앱 런타임은 DDL을 실행하지 않는다(부팅 시 자동 마이그레이션 없음). 적용은 위 스크립트로만 한다.
- 서버(EC2)는 docker compose가 `backend.env`(SSM 유래)를 실제 환경변수로 주입하고 환경변수가
  dotenv보다 우선하므로, 위 파일들은 **로컬 전용**이며 서버 동작에 영향이 없다.

⚠️ **`mcp__claude_ai_Supabase__*`(Supabase MCP)는 PROD 프로젝트만 연결돼 있다.** `execute_sql`·
`apply_migration`을 호출하면 **프로덕션에 직접 들어간다.** dev 작업에 쓰지 말 것.

**브랜치**: `main`에서 작업하지 않는다. `dev` 브랜치 → dev.moly.asia 자동배포로 검증 → `main` 머지(=prod 배포).
dev 서버(EC2 `moly-dev`)는 수동 Start/Stop이며 꺼진 채 push했으면 켠 뒤 GitHub Actions Re-run.

**W1~W3은 DDL이 없어 위 절차와 무관하게 착수 가능**하고, W7 이후가 여기에 걸린다.

각 단위는 **독립 배포 가능**하고 **롤백 가능**해야 한다. 의존은 §2에 정리한다.

---

### 0.7 대화 중심 회상 통합 구현 계약 (이번 작업의 규범 범위)

이 절은 2026-08-04 코드·DB·API 독립 재검증 결과를 반영한 최종 구현 계약이다. 뒤쪽 W1~W11 또는
과거 전환안과 충돌하면 이 절을 우선한다. 목표는 기능 플래그 뒤의 반쪽 구조가 아니라 Dev에서 실제
대화·저장·검색·망각·배치가 하나의 데이터 모델로 동작하는 상태다. 운영 DB와 운영 배포는 범위 밖이다.

#### I1. 단일 진실 소스와 tenant 무결성

- 원본은 PostgreSQL의 `messages`, `diaries`, 정규화 `memory_facts/evidence`다. 의미 검색은 같은 DB의
  pgvector projection이며 mem0나 별도 vector DB를 읽기·쓰기 진실 소스로 병행하지 않는다.
- `messages`, `diaries`, `routines`에 `(user_id,id)` 후보키를 두고 evidence, source, episode,
  reference는 복합 FK로 소유자를 강제한다. service-role이 RLS를 우회해도 교차 사용자 연결은 DB가 거부한다.
- 기억의 독립 근거는 inbound `sender=user` assertion만 허용한다. assistant echo는 사실을 새로 확정하지
  않으며, 사용자 메시지의 정확한 span/hash/extractor version을 provenance로 남긴다.

#### I2. 대화 직렬화·멱등·시간 예산

- HTTP 수신 시 absolute deadline과 request body hash를 만든다. 사용자별 active-turn lease는 동일 key와
  동일 body 재시도만 replay하고, 다른 동시 요청은 `409`와 `Retry-After`로 재시도 가능하게 거절한다.
- Phase B는 lease token, base context revision, selected reference 소유권을 다시 검증한 뒤 한 번만 commit한다.
- 한 요청의 LLM 호출은 최대 2회, tool round는 1회, 병렬 도구는 최대 3개다. grounded 응답 뒤 별도 repair
  LLM을 호출하지 않는다. embedding 같은 외부 호출 동안 DB session/transaction을 점유하지 않는다.
- idempotency는 request hash, reply message, schema version, 만료·redaction 상태를 저장한다. 구 JSON에는
  `references=[]`를 채우는 dual reader를 둔다. 24시간 동안 같은 field values를 replay하고, 이후 30일은
  body 없는 duplicate tombstone으로 새 턴 생성을 막는다.

#### I3. 서버 권위 컨텍스트와 최소 grounding sidecar

- 시간·관계 시작·장착 slot/item·테마·오늘 루틴은 user text 뒤에 붙이지 않고 system-owned typed snapshot으로
  전달한다. 루틴 날짜는 사용자 현지 00:00, 대화/일기 activity date는 현지 04:00 경계를 사용한다.
- 모델의 최종 내부 결과는 과도한 claim graph가 아니라
  `{text,response_mode,selected_refs[],focus_ref?,control_intents[]}`만 사용한다.
- 서버는 budget 전 원본 tool 결과의 stable ID allowlist와 현재 tenant/access를 Phase B에서 재검증한다.
  하나라도 위조·삭제·억제된 ref면 grounded 부분을 조용히 제거하는 것이 아니라 안전한 전체 fallback을 쓴다.
- 공개 API는 versioned `reply.references[]`/history references만 노출한다. `diary-reference-v1` capability가
  있으면 최대 3개의 결정적 diary card를, 없으면 `references=[]`와 자연스러운 요약을 반환한다.

#### I4. 답 완결형 자연 회상

- `recall_diaries`는 한 번에 존재 여부, exact aggregate count, coverage/has_more, stable ID, 표시 날짜, 제목,
  kind, excerpt와 요청된 전문을 제공한다. 검색 후 같은 tool round에서 다시 GET해야만 답할 수 있는 구조는 금지한다.
- `recall_memory`는 안정 사실과 사용자 원문 기반 episode를 구분한다. 정확 발언은 소유권·hash·suppression을
  다시 확인한 원본 span만 인용한다. HNSW 후보는 직접 distance `ORDER BY ... LIMIT` subquery로 제한한다.
- route는 날짜·“검색” 명령 키워드가 아니라 “그때 내가 뭐라고 했지?”, “요즘 잘하고 있어?”,
  “그 일기 기억나?”, “내가 씌운 거 어때?” 같은 필요한 앎을 판단한다. 잡담은 불필요하게 조회하지 않는다.
- zero result, timeout/unavailable, truncated/partial을 구분하고 결과 범위보다 강한 존재·개수 주장을 하지 않는다.

#### I5. 첫 만남과 일기 lifecycle

- 첫 만남 일기는 일일 슬롯이 아닌 `kind=welcome` 프롤로그다. 실제 첫 성공 대화의 Phase B에서 관계 시작
  좌표와 함께 원자적으로 1회 생성한다. 목록/detail GET은 write하지 않고 가짜 전날이나 근거 없는 감정을 쓰지 않는다.
- daily는 `shared_day|capi_day`와 `activity_date`로 welcome과 같은 display date에 공존한다. v1 API는 기존
  type/date를 호환 매핑하고 경계 날짜 동률을 모두 반환한다. v2는 `(display_date,id)` opaque cursor와 kind를 노출한다.
- 카드 전달과 detail GET은 읽음 처리가 아니다. 실제 펼침에서 기존 `/diaries/{id}/read`만 멱등 기록한다.
- diary recall은 suppression generation이 일치하는 projection만 사용한다. source user message가 하나라도
  억제되면 해당 일기를 모델 회상 후보에서 제외한다. 명시적 raw diary open은 record 접근 정책으로 별도 허용한다.

#### I6. 기억 저장·검색·망각의 한 구조

- 각 성공 대화는 원본 message/turn watermark를 먼저 commit하고 같은 트랜잭션에서 bounded extraction,
  embedding, checkpoint job을 enqueue한다. worker는 lease/fencing 뒤 user assertion만 추출해 reconcile한다.
- fact/profile은 정규화 검색, episode는 message ID와 embedding/version/hash만 갖는 projection 검색을 사용한다.
  원문을 vector table이나 job payload에 중복 장기 보존하지 않는다.
- `memory_source_closures`는 늦은 추출 publish 방지용이다. 사용자에게 다시 보이지 않게 하는 상태는 별도
  message/span 단위 suppression이다. recent transcript, checkpoint, fact/profile, episode, diary recall,
  focus, 과거 assistant echo 모두 같은 gate를 통과한다.
- 기본 “지금까지 잊어”는 `cut_watermark` 이하만 억제해 이후 사용자의 재진술을 새 evidence로 허용한다.
  명시적 “앞으로도 기억하지 마”만 predicate future-learning block을 만든다. 무관한 min~max 중간 turn을
  닫지 않는다. 원본 채팅/일기 삭제는 forget과 구분된 record deletion이다.

#### I7. focus·reference·배치·보존

- focus는 실제 제시한 ref 순서, facet, context revision, TTL/최대 20 committed turns를 저장한다. 새 대상,
  망각, 삭제, 만료 시 즉시 무효화하고 “그거/두 번째/내용은?”을 검증된 ref로만 해석한다.
- diary generation도 공용 `async_jobs` lease/fencing/finalize를 사용한다. late worker는 재클레임된 결과를
  publish/delete하지 못한다. 긴 embedding/extraction은 bounded batch+continuation으로 분리한다.
- terminal job payload는 성공/cancel 24시간, dead replay 7일 뒤 scrub하고 비민감 job metadata만 90일 둔다.
  focus 24시간/20턴, grounding 30일을 주기 maintenance가 정리한다.
- replay lineage는 같은 user/job type만 허용하고 operation id로 중복 replay를 막는다. 삭제 barrier가 있으면
  handler 시작과 finalize 직전에 fail-closed한다.

#### I8. Dev 전용 적용과 완료 게이트

1. checksum migration ledger와 실제 Supabase project ref allowlist를 먼저 검증한다. `.env`라는 파일명만으로
   Dev라고 믿지 않는다. destructive/dev script는 알려진 Dev ref가 아니면 fail-closed한다.
2. additive DDL → metadata/source projection/idempotency bounded backfill → 제약 검증 → new writer 순으로 적용한다.
   rollback은 writer kill switch와 호환 reader로 하며 suppression/deletion 기록을 되돌리지 않는다.
3. `/dev` route는 `local|development`이면서 명시 플래그와 operator allowlist가 모두 맞을 때만 동작한다.
   staging/production/unknown에서는 플래그가 켜져도 등록하지 않는다. 진단 endpoint는 실제 recall service와
   실제 chat runtime을 호출하며 모델·입력·timeout을 제한한다.
4. 단위/contract 전체 회귀, 실제 PostgreSQL tenant·동시성·migration 통합, ko/ja/en golden replay,
   timeout/truncation/adversarial prompt, dev p95/2-call 계측을 통과한다.
5. Dev DB에만 migration/backfill/contract gate를 적용하고 dev 서버·Swagger에서 신규/구 capability,
   첫 만남·일기 전문/개수·episode exact quote·forget 후 전 경로 비노출·루틴 경계·장착 snapshot을 확인한다.
6. 문서와 OpenAPI를 실제 코드/DB 상태로 다시 생성한다. 최종 상태는 **Dev 구현·검증 완료, Prod 미적용**으로
   기록한다. 운영 배포·운영 DB migration·main push는 이 작업에서 절대 하지 않는다.

계정 삭제 성공 응답과 backup deletion ledger의 원자 계약은 `moly-auth` 저장소가 소유하므로 이 저장소에서
거짓으로 완료 표시하지 않는다. 여기서는 삭제 barrier·참조/idempotency redaction·worker publish 차단까지
구현·검증하고, cross-repository 연동 항목을 명시적 외부 의존으로 문서화한다.

---

### W1 — 요금 회계 정정 + 호출별 usage 집계

**목적**: "일 한도 = 실비용" 불변식 복원. 턴 수 제약(§0.1)의 기준선을 만든다.

#### W1-1. 캐시 읽기 가중치 정정 (확정)

`app/config.py:72`
```python
bill_weight_cache_read_openai: float = 0.5   # ← 현재 (틀림)
bill_weight_cache_read_openai: float = 0.1   # ← 수정
```

근거: GPT-5.6의 캐시 읽기는 입력 단가의 10%(90% 할인)다. 현재 0.5는 **실비용을 5배 과대 계상**한다.
프로덕션 billable 구성이 캐시 읽기 65%·출력 32%·입력 2%이므로(메모리 `prompt-caching-system` 실측),
이 수정만으로 **턴당 billable이 약 47% 수준으로 떨어진다** = 같은 150k 한도에서 턴 수 약 2배.

> ⚠️ **비즈니스 결정**: 이 수정은 유저에게 정확히 청구하는 대신 **유저당 실제 지출을 최대 2배로
> 늘린다**(지금까지 과다 청구로 유저를 일찍 막고 있었음). 다만 절대액이 작다 —
> 150k × luna 입력 $0.20/M = **유저당 월 상한 $0.90**(현재 실지출은 조기 차단 때문에 약 $0.42).
> 따라서 `free_launch_token_limit=150_000` **유지가 기본**이며, 이 전제 위에서 도구 사용률 상한이
> 불필요해진다(§3.1). 재보정하면 여유가 사라져 사용률 캡이 다시 필요하므로 §3.1을 재계산할 것.
>
> **같이 고칠 것**: `config.py:129`의 `# … luna 기준 ~월 $4.5/인` 주석은 **stale**이다(입력 $1/M
> 시절 값, 현재 $0.20/M). `~월 $0.90/인`으로 정정한다.

#### W1-2. 캐시 쓰기 — 가중치는 1.25, 단 토큰 수는 추정해야 한다

**공식 요금표 확인 결과**(2026-08-03, OpenAI Pricing / Standard·short context):

| 모델 | 입력 | 캐시 읽기 | 캐시 쓰기 | 출력 |
|---|---:|---:|---:|---:|
| gpt-5.6-luna (chat·utility) | $0.20 | $0.02 | $0.25 | $1.20 |
| gpt-5.6-terra (diary) | $2.00 | $0.20 | $2.50 | $12.00 |

입력 대비 비율은 세 tier 모두 동일하다 — **읽기 0.1 · 쓰기 1.25 · 출력 6.0**.
현재 config의 `bill_weight_output_openai=6.0`은 맞고, 읽기 0.5는 틀렸으며(W1-1),
**쓰기는 실제로 과금된다**(무료 아님). long context 요금은 2배지만 우리 프롬프트는 5~6k 토큰
수준이라 항상 short context가 적용된다.

```python
bill_weight_cache_write_openai: float = 0.0    # ← 현재 (틀림)
bill_weight_cache_write_openai: float = 1.25   # ← 수정
```

**그런데 API가 캐시 쓰기 토큰을 보고하지 않는다.** 직접 확인함 — SDK 2.44.0의
`PromptTokensDetails` 필드는 `['audio_tokens', 'cached_tokens']`뿐이고 `CompletionUsage`에도
쓰기 항목이 없다. 따라서 `llm.py:183`의 `cache_write_tokens=0`을 그대로 두면 가중치를 1.25로
올려도 곱할 값이 없어 **여전히 0으로 계상된다**.

**해결: 3버킷 배타 추정.** 요금 모델상 `prompt_tokens`는 세 버킷 중 하나에 배타적으로 속한다
(캐시 읽기 / 캐시 쓰기 / 일반 입력). 우리가 아는 것은 `prompt_tokens`와 `cached_tokens`뿐이므로
나머지를 나눌 수 없다. `_generate_openai`에서 다음과 같이 추정한다.

```python
uncached = max(0, prompt_tokens - cached)
if prompt_tokens >= 1024:          # OpenAI 자동 캐시 최소 프리픽스
    cache_write_tokens = uncached  # 새로 처리된 부분이 캐시에 기록됨
    input_tokens = 0
else:                              # 캐시 대상 미만이면 전량 일반 입력
    cache_write_tokens = 0
    input_tokens = uncached
```

- **오차 크기**: 정상 턴(캐시 적중)에서 `uncached`는 새 유저 메시지 정도라 턴당 billable의 약 1%다.
  캐시 미스 턴(앵커 리셋·오랜 공백 후 첫 턴)에서는 프리픽스 전체가 uncached라 최대 25% 과대 계상된다.
  방향이 보수적(과대)이라 한도 초과는 나지 않는다.
- **검증 필수**: 배포 후 1주간 실제 OpenAI 인보이스와 `billable × 입력단가` 추정액을 대조한다.
  괴리가 5%를 넘으면 추정식을 재조정한다. **이 대조 전까지는 추정임을 문서·주석에 명시한다.**
- SDK가 나중에 캐시 쓰기 필드를 노출하면 추정식을 버리고 실측값으로 교체한다.

#### W1-3. TurnUsage — 턴 내 모든 LLM 호출 합산

**현재 결함**: `_repair_foreign_ko`(`chat.py:409-435`)가 실제 LLM을 최대 2회 호출하는데
**청구에 포함하지 않는다**(`chat.py:430` 로그 주석이 "청구엔 미포함"이라고 명시). 도구 루프가
들어오면 이 누락이 배수로 커진다.

신설: `app/services/chat.py` 또는 `app/conversation/models.py`
```python
@dataclass
class LlmCall:
    provider: str          # "openai" | "anthropic"
    model: str
    purpose: str           # "chat" | "tool_decide" | "tool_final" | "foreign_repair"
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    billable: int          # 이 호출 단독 _billable 결과

@dataclass
class TurnUsage:
    calls: list[LlmCall]
    @property
    def total_billable(self) -> int: ...
    @property
    def totals(self) -> dict[str, int]:  # input/output/cache_read/cache_write 합계
```

- `_billable()`(`chat.py:438`)은 **호출별로** 적용하고 `TurnUsage`가 합산한다.
- `_repair_foreign_ko`는 `LLMResult`를 버리지 말고 호출자에게 usage를 반환하도록 시그니처를 바꾼다
  (반환형 `str` → `tuple[str, list[LlmCall]]`).
- `_accumulate_tokens`(`chat.py:470`)에 넘기는 값은 `TurnUsage.total_billable`이다.
- `messages` 테이블의 기존 컬럼(`input/output/cache_read/cache_write/billable_tokens`)에는
  **합계**를 저장한다(스키마 변경 없음).
- 호출별 상세는 **로그·텔레메트리**로 남긴다(purpose·model 포함). 재감사용 `llm_call_usage` additive
  ledger는 W7 이후 별도 migration으로 넣되, W1에서는 기존 합계 컬럼을 진실 소스로 사용한다.

**보존**: 없음(회계만 변경).
**테스트**:
- `_billable`이 provider prefix로 가중치를 고르는 기존 동작 유지(회귀).
- 한자 복원이 발동한 턴의 `billable_tokens`가 복원 호출분을 포함하는지.
- 캐시 읽기 가중치 0.1로 계산되는지.

**완료 조건**: 한자 복원이 발동한 실제 턴에서 저장된 `billable_tokens`가 chat 호출 + 복원 호출의
합과 일치한다. `ruff` + 기존 테스트 전량 통과.

**롤백**: 가격 가중치는 config 원복, 합산 동작은 아래 킬스위치 또는 pre-W1 artifact로 되돌린다.

`TurnUsage` 회계 동작 자체의 롤백을 위해 `Settings.turn_usage_v2_enabled: bool = True`를 추가한다.
`False`에서는 기존 chat 주호출의 usage만 `_accumulate_tokens`에 넘기고 repair/tool 호출은 계측만 하며,
데이터클래스와 합계 컬럼 스키마는 그대로 둔다. 이미 확정된 message/quota 행은 소급 재계산하지 않는다.

---

### W2 — 계측 (제약 검증 근거)

**목적**: §0.1 두 제약을 판정 가능하게 만든다. **이게 없으면 W4·W5 활성화 판단이 불가능하다.**

**변경**: `app/services/chat.py` `post_message`에 단계별 monotonic 타이밍 기록.

측정 지점:
| 지표 | 측정 구간 |
|---|---|
| `phase1_ms` | 함수 진입 → Phase 1 commit |
| `memory_reload_ms` | `_reload_memory` 호출 구간(스킵 시 0) |
| `llm_ms` | `llm.generate` 호출 구간 |
| `repair_ms` | `_repair_foreign_ko` 구간(미발동 0) |
| `egress_ms` | 백스톱 체인 전체 |
| `phase2_ms` | Phase 2 진입 → commit |
| `total_ms` | end-to-end |
| `prompt_tokens` | 턴의 모든 `LlmCall.input_tokens + cache_read_tokens + cache_write_tokens` 합계 |
| `cache_read_tokens` | 턴의 모든 `LlmCall.cache_read_tokens` 합계 |
| `cache_write_tokens` | 턴의 모든 `LlmCall.cache_write_tokens` 합계(실측 필드가 생기기 전에는 W1-2 추정값) |
| `cache_read_ratio` | `cache_read_tokens / prompt_tokens`(분모 0이면 null) |
| `billable` | 턴 합계(`TurnUsage.total_billable`) |
| `lang` | `i18n.resolve(profile.language)` 버킷 `ko\|en\|ja` — **언어별 분해 집계용(§3.1.5)** |
| `used_tools` | 이 턴이 도구를 호출했는지(bool) — 도구 턴/미사용 턴 단가 분리용 |

- 구조화 로그 1줄로 배출(유저 id·본문 미기록, 길이·hash만).
- `messages` 테이블에 저장하지 않는다(스키마 변경 없음).
- 표본이 쌓이면 p50/p95/p99를 집계해 §0.1 판정에 쓴다.

**보존**: 전부(관측만 추가).
**테스트**: 로그 배출이 응답 경로를 막지 않는지(예외 삼킴).
**완료 조건**: 프로덕션에서 최소 1일치 표본으로 (a) chat p50/p95/p99, (b) **`lang` 버킷별 턴당
billable 중앙값**, (c) `used_tools` 여부별 단가를 각각 산출할 수 있다. (b)가 §3.1.5의 언어별
실측이고, (c)가 §3.1.3의 도구 턴 배수(1.75배 가정) 검증이다.

**이 결과로 결정할 것**:
- 현재 p95가 **2초 이하**면 도구 턴(호출 2회)이 5초에 들어갈 여지가 있다 → W4·W5 구현/측정 진행.
- 현재 p95가 **3초 이상**이면 도구 루프는 5초를 넘긴다 → W3까지만 하고 도구는 보류하거나
  최종 호출 출력 토큰을 줄이는 등 별도 대책이 필요하다.

---

### W3 — 상주 컨텍스트 (CurrentTurnContext)

**목적**: 캐피가 현재 시각·착용 아이템·테마·오늘 루틴을 알게 한다.
**추가 LLM 호출이 없어 응답시간 증가가 사실상 0이고, 비용은 입력 수십 토큰 증가뿐이다.**
→ §0.1 두 제약 모두 안전. **도구 루프보다 먼저 하는 이유가 이것이다.**

#### W3-1. 데이터 조회 (Phase 1 안에서)

Phase 1은 이미 DB 커넥션을 잡고 있으므로 여기서 읽는다. **커넥션 0 불변식과 무충돌.**

| 항목 | 소스 | 비고 |
|---|---|---|
| 시간대 버킷 | `core/time_utils` + `profile.timezone` | 아침/낮/저녁/밤 4종. 아래 경계 고정 |
| 오늘 첫 대화 | 기존 `chat.py:741` 로직 재사용 | |
| 마지막 활동 버킷 | `user_devices.last_active_at` | ⚠️ **이 레포에 갱신 코드 없음** — 값이 신뢰 불가면 이 항목 제외 |
| 착용 아이템 | `user_items.equipped_slot in (hat, glasses, neck, body)` + `products` | `products.name_i18n`을 `i18n.localized_name(..., default=products.name)`으로 해석한 이름만 |
| 테마 | `user_items.equipped_slot = 'theme'` + `products` | 같은 `i18n.localized_name` 계약, 최대 1개 |
| 오늘 루틴 요약 | `routines` + `routine_completions` | **이름 나열 아님**. 예정 N개·완료 M개 |
| D+N | `profile.created_at` | 가입 경과일 |

- 로컬 시각 경계는 현행 `greetings.time_bucket`의 `dawn`을 `morning`에 합친 값으로 고정한다.
  **아침 04:00:00~10:59:59, 낮 11:00:00~16:59:59, 저녁 17:00:00~20:59:59,
  밤 21:00:00~03:59:59**다. DST가 있는 지역도 `profile.timezone`으로 변환한 wall clock을 쓴다.
- 마지막 활동은 `now_utc - last_active_at`으로 계산해 **방금 `<10분`, 오늘 `10분 이상~24시간 미만`,
  최근 `24시간 이상~7일 미만`, 오랜만 `7일 이상`**으로 고정한다. NULL이면 줄을 생략하고 미래값은
  0으로 clamp하며 경고한다. 이 경계의 대화 품질은 **측정 필요**지만 구현 초기값은 최근성 표현을
  과도하게 정밀화하지 않는 위 네 구간이다. `last_active_at` 갱신 경로가 검증되기 전에는 이 필드의
  feature flag를 off로 둔다.
- 루틴은 이름을 주입하지 않고 `예정 N개/완료 M개`만 제공한다. 따라서 루틴명 살균은 W3 테스트 대상이
  아니며, 자유 입력 이름을 반환하는 W6 `get_routines`에서만 살균한다.
- “한 번의 배치 쿼리” 요구는 제거한다. 서로 실패 가능한 조회를 하나의 SQL에 넣으면 한 subquery 오류가
  전체 결과를 무효화해 항목별 fail-open을 지킬 수 없다. profile/첫 대화처럼 이미 읽은 값은 재조회하지
  않고, **장착+테마 1개 SELECT, 루틴 집계 1개 SELECT, 마지막 활동 1개 SELECT**를 각각
  `session.begin_nested()` savepoint 안에서 순차 실행한다. 각 SELECT 실패 시 그 savepoint만 rollback하고
  해당 DTO 필드만 `None`으로 둔다. 세션 자체가 끊긴 오류는 Phase 1 전체의 기존 DB 오류 계약을 따른다.
- 조회 지연을 W2 지표에 `context_ms`로 추가한다.
- `Settings`에 `current_turn_context_enabled: bool = False`와
  `current_context_last_active_enabled: bool = False`를 추가한다. 전자는 W3 전체 rollout 킬스위치,
  후자는 갱신 경로 검증 전 마지막 활동만 끄는 킬스위치다.

#### W3-2. 프롬프트 배치 — **반드시 배열 끝**

`app/services/chat.py` `_build_system`(`chat.py:319`)에 넣지 **않는다**.
시스템 프롬프트에 넣으면 매턴 프리픽스가 바뀌어 **캐시가 전부 깨지고 비용이 폭증한다**
(현재 프로덕션 cache_read 비중 65%).

넣는 위치: `_context()`가 만드는 대화 배열의 **마지막 user 메시지 직전 또는 함께**.
현재 절대 날짜 표식(`_mark_dates`, `chat.py:147`)과 같은 계층이다.

형식(예시 — 실제 문구는 페르소나 톤에 맞게 조정):
```
[지금] 밤 · 오늘 첫 대화 · 함께한 지 43일
[모습] 밀짚모자 · 방: 바닷가
[루틴] 오늘 예정 2개 중 1개 완료
```

- 값이 없는 줄은 **넣지 않는다**(빈 라벨 금지).
- ja/en 유저는 `i18n` 규칙에 따라 라벨을 번역한다. **`language == "ko"` 하드코딩 금지.**
- W3의 루틴 값은 숫자 집계뿐이다. 장착 상품명은 카탈로그 값이어도 `memory._sanitize`와 **동일한 살균**을
  통과시킨다(대괄호·제어문자 제거). 살균은 표현 정리이지 보안 경계가 아니라는 점을 주석에 남긴다.

#### W3-3. 페르소나 지침 추가

`app/services/prompts.py`에 "제공된 현재 상태를 자연스럽게 쓰되 나열하지 말 것" 취지의 짧은 규칙 추가.
**예시 발화를 리터럴로 넣지 않는다**(메모리 `persona-prompt-rules` 누적 규칙).

**보존**: §0.2 전부. 특히 i18n(4)과 캐시 프리픽스 안정성.
**테스트**:
- 상주 블록이 system 프롬프트가 아니라 대화 배열 끝에 들어가는지(캐시 회귀 방지).
- 항목 누락 시 빈 라벨이 안 생기는지.
- ja/en 유저에서 라벨이 해당 언어인지.
- 상품 이름에 `[규칙]` 같은 문자열이 들어와도 살균되는지.
**완료 조건**: 실 유저 replay에서 캐피가 시각·착용·테마·루틴을 인지하고, cache_read 비율이
배포 전후로 유의미하게 떨어지지 않는다(W2 지표로 확인).

**롤백**: 상주 블록 생성을 config 플래그로 끈다.

---

### W4 — `llm.generate_step()` (툴콜 계약)

**목적**: 도구 호출을 표현할 수 있는 LLM 계약 신설. **기존 `generate()`는 그대로 두고 추가한다**
(워커·일기·복원이 계속 쓴다).

**현재 제약**: `llm.py`는 텍스트 in/텍스트 out이고 `LLMResult`에 툴 필드가 없다. 또한
`_generate_openai`(`llm.py:170`)가 `choices[0].message.content`를 문자열로 가정하는데,
툴콜 응답은 `content`가 `None`이라 **빈 답변으로 오인**한다.

**신설 계약** (`app/services/llm.py` 또는 `app/agent/gateway.py`):
```python
TranscriptItem = UserText | AssistantText | AssistantToolCalls | ToolResult

@dataclass(frozen=True)
class UserText:
    text: str

@dataclass(frozen=True)
class AssistantText:
    text: str

@dataclass(frozen=True)
class AssistantToolCalls:
    calls: tuple[ToolCall, ...]

@dataclass
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict          # validation_error가 None일 때 input_model 검증 완료 값
    validation_error: str | None = None  # malformed JSON/unknown tool/schema mismatch

@dataclass(frozen=True)
class ControlIntent:
    kind: str                # "pin" | "forget"; 그 밖은 validation error
    target_fact_ids: tuple[uuid.UUID, ...] = ()
    value: str | None = None

@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: Literal["ok", "unavailable"]
    data: dict | list | None
    schema_version: Literal[1] = 1
    error_code: str | None = None   # timeout|cancelled|invalid_arguments|tool_call_limit|dependency_error|internal
    truncated: bool = False
    duration_ms: int = 0

@dataclass
class StepResult:
    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: LlmCall           # W1의 LlmCall
    control_intents: list[ControlIntent]

async def generate_step(
    system: str | list[str],
    transcript: list[TranscriptItem],
    *, tools: list[dict] | None, tool_choice: str,
    model: str, max_tokens: int, timeout: float,
) -> StepResult: ...
```

- **OpenAI 경로만 먼저 구현한다.** Anthropic 툴 경로는 dormant 정책에 맞춰 미구현(호출 시 명시적 오류).
- `call_id`로 `ToolResult`를 상관시킨다. 다음 홉 transcript에 assistant tool_calls 메시지와
  각 tool result를 짝지어 넣는다.
- `control_intents`는 **포함한다**. 단 final step에서만 허용하고 첫 step에 오면 버린 뒤 계측한다.
  제품 정책 확정 전에는 Phase B durable write로 연결하지 않고 shadow trace만 남긴다. 자유문 정규식으로
  intent를 보충하지 않는다.
- `content=None`을 빈 답변으로 처리하지 않는다.
- usage는 W1의 `LlmCall`로 반환해 `TurnUsage`에 누적한다.

**typed transcript → OpenAI wire 변환**(역변환도 동일 필드로 대칭):

| variant | wire message |
|---|---|
| `UserText(text)` | `{"role":"user","content":text}` |
| `AssistantText(text)` | `{"role":"assistant","content":text}` |
| `AssistantToolCalls(calls)` | `role="assistant"`, `content=null`, 각 call을 `{"id":call_id,"type":"function","function":{"name":tool_name,"arguments":json.dumps(arguments, ensure_ascii=False, separators=(",",":"), sort_keys=True)}}` |
| `ToolResult` | `role="tool"`, `tool_call_id=call_id`, `content`는 `{"schema_version":1,"tool":tool_name,"status":status,"data":data,"error_code":error_code,"truncated":truncated}`의 compact JSON |

`duration_ms`는 trace 전용이라 모델 wire에 넣지 않는다. `status='unavailable'`이면 `data=null`과
비어 있지 않은 `error_code`가 필수이고, `status='ok'`이면 `error_code=null`이다. assistant tool call
메시지 다음에는 원래 순서대로 모든 `call_id`의 tool message가 정확히 하나씩 와야 한다. 인자 JSON 파싱,
도구명 조회 또는 Pydantic 검증 실패도 실행하지 않은 `ToolResult(status='unavailable',
error_code='invalid_arguments')`로 닫는다.

**보존**: 기존 `generate()` 시그니처·동작 무변경(회귀 테스트로 확인).
**테스트**:
- 툴콜 응답(`content=None`)이 빈 답변으로 처리되지 않는지.
- `tool_choice="none"` 강제가 동작하는지.
- 모든 `tool_calls`에 `ToolResult`가 대응하지 않으면 다음 호출을 만들지 않는지(형식 완결성).
- Anthropic 모델로 호출 시 명확한 오류.

---

### W5 — 도구 루프 (AgentRuntime)

> **착수 조건**: W2 계측 결과 현재 p95가 5초 예산 안에 도구 호출 2회를 넣을 수 있음이 확인될 것.

**신설**: `app/services/agent/runtime.py`

```
turn_deadline = monotonic() + agent_turn_deadline_s      # 기본 5.0 (config)
reserved_final = agent_final_reserve_s                    # 최종 호출용 선예약
  ↓
1. 안전 게이트(현재 발화 기준) — 명확한 위기면 도구 스킵
2. step 1: generate_step(tools=..., max_tokens=agent_decide_max_tokens)
   ├─ tool_calls 없음 → 그대로 최종 답변 (호출 1회, 현재와 동일)
   └─ tool_calls 있음 ↓
3. 남은 시간 < reserved_final → 도구 시작하지 않고 step 2로 직행
4. 도구 병렬 실행 (최대 agent_max_tool_calls_per_turn개만 asyncio.gather, per-tool timeout)
   실패·타임아웃도 ToolResult(status="unavailable")로 채워 형식 완결
5. step 2: generate_step(tool_choice="none")  ← 예약 예산 사용
6. egress 백스톱 체인 (기존 순서 그대로)
```

**설정 키** (`app/config.py`, 전부 `app_config` 오버라이드 가능하게):
| 키 | 초기값 | 의미 |
|---|---|---|
| `agent_enabled` | `False` | 킬스위치 |
| `agent_turn_deadline_s` | `5.0` | **§0.1 하드 제약** |
| `agent_final_reserve_s` | `2.5`(측정 필요) | 최종 호출용 선예약 |
| `agent_max_tool_rounds` | `1` | 라운드 상한 |
| `agent_max_tool_calls_per_turn` | `3` | 한 라운드 fan-out 상한. v1 도구 4종 중 한 답변에 필요한 조회를 3개로 제한 |
| `agent_decide_max_tokens` | `192` | step 1 출력 상한(도구 결정만). **§3.1.3 부등식 `7.25D+1.25T ≤ 2307` 의 해 — 단독으로 바꾸지 말고 T와 함께 부등식을 다시 확인할 것** |
| `agent_tool_result_budget_tokens` | `600` | 한 턴의 도구 결과 **합계** 예산. W6의 도구별 글자 상한보다 우선하며 초과분은 절단(§3.1.3) |
| `agent_tool_timeout_ms` | `800` | 도구별 상한 |
| `agent_tool_inflight` | `8`(측정 필요) | 프로세스 전체 동시 도구 수 |
| `agent_canary_pct` | `0` | 카나리 비율 |

표의 키 전부를 같은 이름과 초기값으로 `Settings` 필드에도 추가한다. 환경값은 DB override가 없거나
불량일 때의 fallback일 뿐이다.

`측정 필요` 키는 rollout 전까지 override가 없으면 agent를 켤 수 없게 검증한다. 개발·shadow의
실행 초기값은 그대로 `agent_final_reserve_s=2.5`, `agent_tool_inflight=8`이다. 2.5초는 §3.2의 현재 단발 응답
p95 예산을 그대로 보존하기 위한 값이고, 8은 요청 하나의 최대 fan-out 3보다 크면서 현행 DB pool을
실측하기 전 무제한 동시 실행을 막는 보수적 프로세스 상한이다. 두 값 모두 **W2와 DB pool wait 측정 후
재조정 필요**하며, production에서 `agent_enabled=True`로 해석하려면 유효한 `app_config` 값이 명시돼야 한다.
모델이 3개를 초과해 반환하면 앞의 3개만 실행하는 것이 아니라, 모든 call id의 형식을 닫기 위해
4번째 이후에도 `ToolResult(status='unavailable', error_code='tool_call_limit')`를 붙인다.

- 각 LLM 호출의 timeout은 `min(llm_timeout_s, 남은 데드라인)`이다. **`llm_timeout_s=60`을 그대로
  쓰면 5초 제약이 깨진다.**
- 도구 실행은 **툴별 단명 read-only 세션**을 별도 세션팩토리로 연다. Phase 1의 세션을 재사용하지
  않는다(SOMA-374 유지).
- `agent_enabled=False`면 기존 단발 경로와 **바이트 동일** 동작.

#### W5-1. `app_config` override 배선

`Settings`는 환경/배포 기본값이고 DB override를 직접 읽지 않는다. `app/services/limits.py`의 토큰
`_KEYS`에 agent 키를 섞지 말고 `app/services/agent/config.py`에 다음 계약을 신설한다.

```python
AGENT_CONFIG_KEYS = (
    "agent_enabled", "agent_turn_deadline_s", "agent_final_reserve_s",
    "agent_max_tool_rounds", "agent_max_tool_calls_per_turn",
    "agent_decide_max_tokens", "agent_tool_result_budget_tokens", "agent_tool_timeout_ms", "agent_tool_inflight",
    "agent_canary_pct",
)

@dataclass(frozen=True)
class AgentConfigSnapshot:
    enabled: bool
    turn_deadline_s: float
    final_reserve_s: float
    max_tool_rounds: int
    max_tool_calls_per_turn: int
    decide_max_tokens: int
    tool_result_budget_tokens: int
    tool_timeout_ms: int
    tool_inflight: int
    canary_pct: float
    source: Mapping[str, Literal["app_config", "settings"]]

async def effective_agent_config(session: AsyncSession) -> AgentConfigSnapshot: ...
```

- **조회 시점**: Phase 1의 read-only 구간에서 `get_config_values(session, AGENT_CONFIG_KEYS)`를 한 번
  호출하고, ORM이 아닌 frozen `AgentConfigSnapshot`을 `Phase1DTO.agent_config`에 복사한다. agent phase는
  DB나 `settings`를 다시 읽지 않는다.
- **우선순위/fallback**: 키별로 `app_config.value`가 존재하고 아래 검증을 통과하면 그것을 쓰며,
  누락·JSON 타입 불일치·범위 위반이면 해당 키만 `settings.<key>`로 fallback하고 key/reason만 warning한다.
  DB 조회 자체가 실패하면 기존 Phase 1 DB 오류로 요청을 실패시킨다. 설정 장애를 숨기기 위해 같은
  트랜잭션에서 임의 fallback하지 않는다.
- **검증(개별 범위)**: bool은 실제 JSON bool만, `turn_deadline_s`는 `(0,5.0]`, `final_reserve_s`는
  `(0, turn_deadline_s)`, rounds는 `1`, calls는 `1..3`, `decide_max_tokens`는 `1..214`,
  `tool_result_budget_tokens`는 `1..732`, tool timeout은 `1..800`, inflight는 `1..64`의 정수,
  canary는 `0.0..100.0`의 유한 숫자다(bool을 숫자로 받지 않는다).
- **검증(비용 불변식) — 개별 범위만으로는 부족하다.** `decide_max_tokens`(=D)와
  `tool_result_budget_tokens`(=T)는 **조합**이 턴 수 제약을 정하므로 snapshot 조립 시 반드시
  다음을 함께 확인한다(§3.1.3).
  ```
  7.25 * D + 1.25 * T <= 2307      # 위반 시 override 거부 → 코드 기본값(192/600) 사용 + 경보
  ```
  개별 상한 `214`·`732`는 각각 **상대가 기본값일 때의 최대치**이므로, 둘 다 개별 범위를 통과해도
  조합이 위반할 수 있다. 그래서 부등식 검사가 따로 필요하다.
  예: `D=214, T=700` → 개별 범위는 둘 다 통과하지만 `2,426.5 > 2,307`이라 **거부**돼야 한다
  (실제로 32.73턴, -22.08%로 제약 위반).
  기본값 `D=192, T=600`은 2,142로 경계까지 **165의 여유**가 있다. 올릴 수 있는 폭은
  `7.25·ΔD + 1.25·ΔT ≤ 165` 를 만족하는 만큼이며 **소폭이면 동시 상향도 된다**
  (예: `D=193, T=601` → 2,150.5 ✅). 한쪽만 올릴 때의 최대치가 각각 `D=214`(T=600 유지)·
  `T=732`(D=192 유지)이고, 이 둘을 **동시에** 적용하면(`D=214, T=732` → 2,466.5) 위반이다.
- canary는 **롤아웃 속도 조절용이며 비용 캡이 아니다** — 비용 근거의 도구 사용률 상한은 두지 않는다(§3.1).
  `enabled=True`인데 final reserve/inflight가 `app_config` 출처가 아니면 production에서는 snapshot을
  `enabled=False`로 fail-closed하고 경보한다.
- **캐싱**: 프로세스 전역 TTL 캐시는 두지 않는다. Phase 1의 snapshot 자체가 그 턴의 캐시이며 요청당
  DB 조회 1회다. 이로써 override는 다음 턴부터 반영되고 두 EC2의 캐시 불일치가 없다. 설정 조회 p95가
  유의미하면 그때만 invalidation 계약이 있는 캐시를 별도 설계한다(**측정 필요**).
- canary는 `int.from_bytes(sha256(user_id.bytes).digest()[:8]) % 10000 < canary_pct * 100`으로 결정해
  0.01% 단위로 프로세스·재시작 간 안정화한다.

**보존**: §0.2 전부. 특히 egress 순서(3)와 멱등(5). 도구 결과는 Phase 2 저장 대상이 아니다
(`messages` 스키마 무변경, 턴 내 휘발).

**테스트**:
- 도구 미호출 턴이 기존 경로와 동일한 결과·호출 수인지.
- 데드라인 부족 시 도구를 시작하지 않고 최종 답변을 만드는지.
- 도구 일부 실패 시 모든 `call_id`에 결과가 붙는지.
- 도구 전부 실패 시에도 정상 응답이 나오는지.
- `agent_enabled=False`에서 기존 동작 회귀 0.

---

### W6 — 도구 4종

**신설**: `app/services/agent/tools/` — 도구당 파일 1개 + `registry.py`

```python
class Tool(Protocol):
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    timeout_ms: int
    async def execute(self, ctx: ToolContext, args: BaseModel) -> ToolResult: ...

@dataclass(frozen=True)
class ToolContext:
    user_id: uuid.UUID     # 서버가 주입. 모델 인자에 없음
    language: str
    activity_date: date
    deadline: float        # monotonic
```

| 도구 | 인자 | 반환 | 제한 |
|---|---|---|---|
| `search_memory` | `query` 1~200자, `time_hint?={from?,to?}` | fact/insight 구분, 관찰일 포함 | normalized만, 최대 5건, 조회폭 3,660일, 항목당 300자·전체 1,500자 |
| `search_diaries` | `query?` 1~200자, `from?`, `to?` | 발췌 + `weather` | `published_at <= now()`만, 최대 5건, 조회폭 90일, 발췌 400자·전체 2,000자 |
| `get_diary` | `date` | 해당 유저 일기 | 정확히 0/1건, 본문 4,000자, `activity_date` 기준 현재일 ±3,660일 |
| `get_routines` | `date?` | 루틴명·주기·그날 수행 | soft-deleted 제외, 최대 20건, 이름 100자·전체 2,000자, 현재 activity date ±31일 |

> ⚠️ **위 도구별 글자 상한은 개별 안전장치일 뿐이고, 비용을 결정하는 것은
> `agent_tool_result_budget_tokens`(턴 합계 600tok)다.** 세 도구가 각자 상한까지 채우면
> 합계가 5,000tok을 넘어 턴 수 제약을 깨뜨린다(§3.1.3). runner는 도구 결과를 transcript에
> 넣기 전에 **턴 합계 예산으로 절단**한다 — 호출 순서대로 채우고 예산 소진 시 나머지 도구 결과는
> `truncated` 표시와 함께 잘린다. 절단 사실은 trace에 남긴다.

- **모든 쿼리에 `WHERE user_id = ctx.user_id`.** 모델 인자에 `user_id`·SQL·자유 필터를 두지 않는다.
- `search_memory` fact 후보는 `memory_facts.user_id=ctx.user_id AND status='active'`이고, 같은 user의
  `scope='all'`, matching predicate, fact_id 또는 `(normalized_hash,normalization_version)` marker가
  **하나도 없어야** 한다. 마지막 쌍은
  `memory_facts.content_hash=memory_forget_markers.normalized_hash AND
  memory_facts.normalization_version=memory_forget_markers.normalization_version`로 비교한다. insight 후보는
  `memory_insights.user_id=ctx.user_id AND status='active'`이고
  `memory_insight_sources`의 모든 fact가 방금 fact 조건을 만족해야 한다. 현재 published profile에 이미
  들어간 source는 제외한다. `time_hint`는 fact의 `COALESCE(event_time,valid_from)`, insight의
  `valid_from`에 inclusive 적용한다. semantic 후보와 lexical/date 후보를 합친 뒤 relevance·importance·
  recency·confidence로 deterministic rerank하고 top 5만 반환한다. 점수 가중치는 shadow 평가 데이터로
  확정하기 전 registry를 켜지 않는다(**측정 필요**).
- diary 두 도구는 `diaries.user_id=ctx.user_id AND published_at IS NOT NULL AND published_at<=now()`를,
  routine 도구는 `routines.user_id=ctx.user_id AND deleted_at IS NULL` 및 completion의 같은 user/date를
  모두 조건으로 강제한다.
- 도구 name/description은 **ASCII 고정 영어**(언어별 분기 시 캐시가 언어별로 쪼개짐).
- 반환값은 Pydantic 스키마 + 행 수 + 글자 수 + 날짜 범위를 전부 제한한다.
- 반환 문자열은 `_sanitize` 통과.
- 날짜 인자가 없으면 `ctx.activity_date`를 쓴다. `from > to` 또는 위 범위 초과는
  `invalid_arguments`; DB 결과는 안정 정렬(관련도 내림차순 후 id, 날짜 도구는 activity_date 내림차순
  후 id)한 뒤 행·문자 상한을 적용하고 잘렸으면 `truncated=True`다. 이 수치는 5초/최종 prompt 예산을
  지키기 위한 **초기값이며 usefulness·토큰·DB p95 측정 후 조정 필요**다.

`search_memory`는 mem0 façade에 없는 임시 search를 만들지 않는다. **W8 normalized repository와
hard filter가 준비된 뒤에만 registry에 등록**한다. W8 전에는 이 도구를 schema에도 노출하지 않으며,
mem0 `load_for_context` 결과를 query search처럼 재사용하지 않는다.

**output_model의 wire data**도 고정한다. 모든 UUID/date/datetime은 문자열(UUID canonical,
date `YYYY-MM-DD`, datetime UTC ISO-8601 `Z`)로 serialize한다.

```text
search_memory  {"items":[{"id":uuid,"kind":"fact|insight","text":str,"observed_at":datetime|null}]}
search_diaries {"items":[{"diary_date":date,"excerpt":str,"weather":str}]}
get_diary      {"diary":null|{"diary_date":date,"content":str,"weather":str}]}
get_routines   {"items":[{"id":uuid,"name":str,"frequency_per_week":int,"days_of_week":[int],"completed":bool}]}
```

`not_found`는 transport 실패가 아니므로 `get_diary`의 0건은 `status='ok', data={"diary":null}`이다.
DB/의존성 실패만 `unavailable`이다. `days_of_week`은 현행 ISO 1=월…7=일을 그대로 쓰고
`frequency_per_week=len(days_of_week)`로 내려 새 주기 enum을 만들지 않는다. 상품·루틴 이름은 각각
현행 `products.name_i18n`/`routines.name_i18n`을 `i18n.localized_name`으로 해석한다.

**`search_diaries` 선결 과제**: `diaries`에 검색 색인이 없다(title 컬럼도 없음).
한국어 FTS 방식(tsvector+GIN vs pgvector)은 **측정 후 택1**. 그 전까지는 날짜 기반 조회만 제공하고
`search_diaries`를 비활성화한다.

**테스트 (필수)**:
- **cross-user negative test** — 타 유저 데이터가 0건 반환되는지. 도구별 전부.
- 반환 길이·행수 상한 초과 시 잘리는지.
- 유저 자유 입력(루틴명)에 인젝션 문자열이 있어도 살균되는지.

---

### W7 — 잡 플랫폼 (`async_jobs`)

**목적**: 대화 후 기억 처리를 내구적으로 만든다. W8의 전제.

**DDL** (§0.6 절차에 따라 `db/migrations/`에 작성 → dev 적용·검증 → prod 적용 → 머지):
```sql
CREATE TABLE async_jobs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  queue         text NOT NULL,
  job_type      text NOT NULL,
  user_id       uuid NULL REFERENCES profiles(id) ON DELETE CASCADE,
  dedup_key     text NOT NULL,
  payload       jsonb NOT NULL,
  state         text NOT NULL DEFAULT 'ready'
    CHECK (state IN ('ready','running','succeeded','dead','cancelled')),
  priority      integer NOT NULL DEFAULT 100,
  available_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NULL,
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
  UNIQUE (job_type, dedup_key),
  CHECK (
    (state = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
    OR (state <> 'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL)
  )
);
CREATE INDEX async_jobs_claim_idx
  ON async_jobs (queue, priority, available_at, created_at) WHERE state='ready';
CREATE INDEX async_jobs_reclaim_idx
  ON async_jobs (queue, lease_until) WHERE state='running';
```

상태 전이는 아래뿐이다. `succeeded/dead/cancelled`에서 같은 행을 `ready`로 되살리지 않는다. 운영 replay는
`dedup_key='replay:{old_job_id}:{operation_id}'`인 새 행으로 만들고 감사 로그를 남긴다.

```text
ready --claim--> running --success--> succeeded
  ▲                 ├--retryable failure, attempt 미소진--> ready(available_at=retry_at)
  │                 └--non-retryable/attempt 소진--> dead
  └--reaper: expired lease, attempt 미소진-- running
ready/running --expires_at 경과--> cancelled
```

**claim**은 `ready`만 대상으로 하는 아래 한 트랜잭션이다. lease reclaim을 claim에 섞지 않는다.
```sql
BEGIN;
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='ready' AND available_at <= now()
    AND attempt < max_attempts AND (expires_at IS NULL OR expires_at > now())
  ORDER BY priority ASC, available_at ASC, created_at ASC
  FOR UPDATE SKIP LOCKED LIMIT :batch_size
)
UPDATE async_jobs j
SET state='running', attempt=j.attempt+1, lease_owner=:worker_id,
    lease_token=gen_random_uuid(), lease_until=now()+(:lease_seconds * interval '1 second')
FROM candidate c WHERE j.id=c.id RETURNING j.*;
COMMIT;
```
**`attempt`는 claim 시점에 증가**한다. 크래시로 finalize를 못 한 잡도 재클레임마다 카운트돼
반드시 `dead`에 도달한다(poison job 무한 루프 방지).

**성공 finalize**는 fencing된 UPDATE와 도메인 반영·후속 job enqueue를 같은 짧은 트랜잭션에서 한다.
UPDATE가 0행이면 lease를 잃었으므로 도메인 반영도 하지 않는다.
```sql
UPDATE async_jobs
SET state='succeeded', result_code=:result_code, result_detail=:result_detail,
    finished_at=now(), lease_owner=NULL, lease_token=NULL, lease_until=NULL
WHERE id=:id AND state='running' AND lease_owner=:worker_id AND lease_token=:lease_token;
```
이하 `succeeded/foo`, `dead/foo` 표기는 새 state가 아니라 각각
`state='succeeded'|'dead', result_code='foo'`의 축약이다.

**retryable 실패 finalize**는 현재 claim을 포함한 `attempt >= max_attempts`에서 즉시 dead로 끝낸다.

```sql
UPDATE async_jobs
SET state=CASE WHEN attempt >= max_attempts THEN 'dead' ELSE 'ready' END,
    available_at=CASE WHEN attempt >= max_attempts THEN available_at ELSE :retry_at END,
    finished_at=CASE WHEN attempt >= max_attempts THEN now() ELSE NULL END,
    last_error_code=:error_code, last_error_at=now(),
    lease_owner=NULL, lease_token=NULL, lease_until=NULL
WHERE id=:id AND state='running' AND lease_owner=:worker_id AND lease_token=:lease_token;
```

재시도 시각은 attempt가 claim 때 증가했다는 전제로 다음 **equal-jitter exponential backoff**로 계산한다.

```python
raw_s = min(job_backoff_cap_s, job_backoff_base_s * (2 ** (attempt - 1)))
delay_s = uniform(raw_s / 2, raw_s)
retry_at = now_utc + timedelta(seconds=delay_s)
```

초기값은 `job_backoff_base_s=2.0`, `job_backoff_cap_s=60.0`이다. 0초 연속 재시도를 피하면서 3회 claim이
긴 장애에서 무한정 대기하지 않게 한 운영 시작값이며 **실제 429 Retry-After·복구시간 측정 후 조정
필요**다. provider가 유효한 `Retry-After`를 주면 `retry_at=max(위 계산값, Retry-After)`로 한다.

**reaper**는 모든 큐에 대해 **10초마다, 큐별로, statement당 50행**을 초기값으로 실행한다. 한 주기에서
(1) terminal running, (2) retryable running, (3) terminal ready 순서로 각각 별도 트랜잭션/commit한다.
10초는 최단 lease 20초보다 짧아 회수 지연을 한 lease 안으로 제한하고, 50은 무제한 UPDATE를 피하는
시작값이다. 둘 다 queue oldest age와 한 statement DB p95를 보고 **측정 후 조정 필요**다.

```sql
-- (1) expired running 중 terminal
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='running' AND lease_until < now()
    AND ((expires_at IS NOT NULL AND expires_at <= now()) OR attempt >= max_attempts)
  ORDER BY CASE WHEN expires_at IS NOT NULL AND expires_at <= now() THEN 0 ELSE 1 END,
           lease_until, id
  FOR UPDATE SKIP LOCKED LIMIT :reap_batch_size
)
UPDATE async_jobs j SET
  state=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'cancelled' ELSE 'dead' END,
  finished_at=now(),
  last_error_code=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now()
                       THEN 'expired' ELSE 'attempts_exhausted' END,
  last_error_at=now(), lease_owner=NULL, lease_token=NULL, lease_until=NULL
FROM candidate c WHERE j.id=c.id;

-- (2) expired running 중 retryable
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='running' AND lease_until < now()
    AND (expires_at IS NULL OR expires_at > now()) AND attempt < max_attempts
  ORDER BY lease_until, id
  FOR UPDATE SKIP LOCKED LIMIT :reap_batch_size
)
UPDATE async_jobs j SET state='ready', available_at=now(), finished_at=NULL,
  last_error_code='lease_expired', last_error_at=now(),
  lease_owner=NULL, lease_token=NULL, lease_until=NULL
FROM candidate c WHERE j.id=c.id;

-- (3) claim되지 않은 terminal ready
WITH candidate AS (
  SELECT id FROM async_jobs
  WHERE queue=:queue AND state='ready'
    AND ((expires_at IS NOT NULL AND expires_at <= now()) OR attempt >= max_attempts)
  ORDER BY available_at, id
  FOR UPDATE SKIP LOCKED LIMIT :reap_batch_size
)
UPDATE async_jobs j SET
  state=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now() THEN 'cancelled' ELSE 'dead' END,
  finished_at=now(),
  last_error_code=CASE WHEN j.expires_at IS NOT NULL AND j.expires_at <= now()
                       THEN 'expired' ELSE 'attempts_exhausted' END,
  last_error_at=now()
FROM candidate c WHERE j.id=c.id;
```

heartbeat도 `id + state='running' + lease_owner + lease_token` 조건으로만 `lease_until`을 연장한다.
외부 호출 중 row lock/session은 0개다.

- 큐: `critical`(결제) · `interactive_async`(대화 후속) · `content`(일기·요약·반추) ·
  `notification`(저녁 푸시) · `maintenance`. 프로세스는 나누지 않고 소비자 내부 슬롯으로 분리.

**consumer 1개당 초기 실행값**(두 EC2 각각 동일):

| queue | concurrency slots | claim batch | handler timeout | lease | max attempts | 근거 |
|---|---:|---:|---:|---:|---:|---|
| `critical` | 2 | 2 | 10s | 30s | 3 | 다른 큐와 분리한 예약 슬롯, 짧은 DB/provider 처리 |
| `interactive_async` | 2 | 2 | 30s | 45s | 3 | 대화 후속 지연을 content와 격리 |
| `content` | 1 | 1 | 120s | 150s | 3 | 현행 `worker_user_timeout_s=120`, LLM 장기 작업 직렬 시작 |
| `notification` | 1 | 1 | 10s | 20s | 3 | marker 선점 전 장애만 bounded retry; 선점 후 실패는 succeeded/send_failed_after_claim |
| `maintenance` | 1 | 1 | 60s | 90s | 3 | 사용자 경로보다 낮은 우선순위 |

이는 구현자가 즉시 쓸 **보수적 초기값**일 뿐 처리량 근거가 아직 없어 전부 **부하 측정 필요**다.
DB pool wait p95, queue oldest age, lease expiry율, provider p95를 본 뒤 큐별로 조정한다. content가 밀려도
critical/notification 슬롯을 빌려 쓰지 않으며, 각 claim batch는 그 큐의 빈 슬롯 수 이하로 clamp한다.
`Settings`에는 `job_backoff_base_s=2.0`, `job_backoff_cap_s=60.0`,
`job_reaper_interval_s=10.0`, `job_reaper_batch_size=50`과 위 표를 표현하는 queue별
`job_<queue>_{concurrency,claim_batch,timeout_s,lease_s,max_attempts}` 필드를 같은 값으로 추가한다.
이 worker 값은 초기에는 환경 설정만 사용하고 `app_config` hot override 대상으로 넣지 않는다.

**dead 경보 경로**: 어떤 finalize/reaper가 `dead`로 전이한 트랜잭션을 commit하면 (a) PII 없는 구조화
error 로그(`job_id,queue,job_type,attempt,error_code`)를 반드시 남기고, (b)
`settings.slack_alert_webhook_url`(없으면 `slack_webhook_url`)로 즉시 best-effort 경보하며,
`alert_dedup_window_sec` 동안 `(queue,job_type,error_code)`를 dedup한다. Slack 실패가 job 상태를 rollback하지
않는다. 놓친 경보는 `/health`의 queue별 persistent dead count/oldest dead age와 운영 대시보드가 보완하며
dead count 증가 또는 1건 이상 미확인 상태를 배포 gate 실패로 취급한다. **dead는 자동 삭제하지 않는다.**
운영 replay는 기존 행 수정이 아니라 위 새 replay 행으로만 한다.

**재시도 분류**:
| 오류 | 처리 |
|---|---|
| timeout·429·일시 네트워크/DB | backoff + jitter |
| 스키마 검증 실패·미지원 payload | 즉시 `dead` |
| 대상 유저 탈퇴·삭제 | `succeeded` 또는 `cancelled` |
| `expires_at` 경과 | `cancelled` |

**테스트**: 크래시 후 회수, fencing(늦게 돌아온 소비자가 확정 못 함), poison job이 `dead` 도달,
큐 A 적체가 큐 B를 막지 않음.

---

### W8 — 메모리 정규화

**DDL**은 아래를 그대로 additive expand migration으로 §0.6 절차(작성 → dev 적용·검증 → prod 적용 →
머지)에 따라 반영한다. 자연어 컬럼 writer는
저장 직전 `naming.to_placeholder`를 강제한다. `embedding vector`는 현재 설치된 pgvector의 무차원 vector를
사용하며 차원 고정은 embedder migration에서 별도로 검증한다.

```sql
CREATE TABLE memory_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  kind text NOT NULL,
  canonical_text text NOT NULL,
  subject text NULL,
  predicate text NULL,
  object_json jsonb NULL,
  event_time timestamptz NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','superseded','forgotten')),
  importance double precision NOT NULL,
  confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  content_hash text NOT NULL,
  normalization_version text NOT NULL,
  superseded_by uuid NULL,
  embedding vector NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, id),
  FOREIGN KEY (user_id, superseded_by) REFERENCES memory_facts(user_id, id) ON DELETE RESTRICT,
  CHECK ((status='active' AND valid_to IS NULL) OR status<>'active')
);
CREATE INDEX memory_facts_active_user_idx ON memory_facts(user_id, predicate, event_time)
  WHERE status='active';
CREATE INDEX memory_facts_hash_idx
  ON memory_facts(user_id, normalization_version, content_hash);

CREATE TABLE memory_evidence (
  fact_id uuid NOT NULL REFERENCES memory_facts(id) ON DELETE RESTRICT,
  source_type text NOT NULL CHECK (source_type='conversation_turn'),
  source_id bigint NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
  source_excerpt_hash text NOT NULL,
  observed_at timestamptz NOT NULL,
  PRIMARY KEY (fact_id, source_type, source_id)
);

CREATE TABLE memory_insights (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
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

CREATE TABLE memory_insight_sources (
  user_id uuid NOT NULL,
  insight_id uuid NOT NULL,
  fact_id uuid NOT NULL,
  PRIMARY KEY (user_id, insight_id, fact_id),
  FOREIGN KEY (user_id, insight_id) REFERENCES memory_insights(user_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, fact_id) REFERENCES memory_facts(user_id, id) ON DELETE RESTRICT
);

CREATE TABLE relationship_profiles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  version bigint NOT NULL CHECK (version > 0),
  locale text NOT NULL,
  memory_generation bigint NOT NULL,
  relationship_profile_input_revision bigint NOT NULL,
  document_json jsonb NOT NULL,
  rendered_text text NOT NULL,
  render_hash text NOT NULL,
  status text NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','published','invalidated','superseded')),
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz NULL,
  UNIQUE (user_id, id),
  UNIQUE (user_id, locale, version),
  CHECK ((status='published' AND published_at IS NOT NULL) OR status<>'published')
);
CREATE UNIQUE INDEX relationship_profiles_one_published_idx
  ON relationship_profiles(user_id, locale) WHERE status='published';

CREATE TABLE relationship_profile_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  relationship_profile_id uuid NOT NULL,
  item_key text NOT NULL,
  fact_id uuid NULL,
  insight_id uuid NULL,
  CHECK (num_nonnulls(fact_id, insight_id)=1),
  FOREIGN KEY (user_id, relationship_profile_id)
    REFERENCES relationship_profiles(user_id, id) ON DELETE CASCADE,
  FOREIGN KEY (user_id, fact_id) REFERENCES memory_facts(user_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (user_id, insight_id) REFERENCES memory_insights(user_id, id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX relationship_profile_sources_fact_uq
  ON relationship_profile_sources(user_id, relationship_profile_id, item_key, fact_id)
  WHERE fact_id IS NOT NULL;
CREATE UNIQUE INDEX relationship_profile_sources_insight_uq
  ON relationship_profile_sources(user_id, relationship_profile_id, item_key, insight_id)
  WHERE insight_id IS NOT NULL;

CREATE TABLE memory_forget_markers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  scope text NOT NULL CHECK (scope IN ('fact','predicate','all')),
  fact_id uuid NULL,
  normalized_hash text NULL,
  normalization_version text NULL,
  predicate text NULL,
  memory_generation bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NULL,
  FOREIGN KEY (user_id, fact_id) REFERENCES memory_facts(user_id, id)
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
  ON memory_forget_markers(user_id, scope, normalization_version, normalized_hash, predicate);

CREATE TABLE memory_source_turns (
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  source_watermark bigint NOT NULL CHECK (source_watermark > 0),
  representative_message_id bigint NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
  committed_at timestamptz NOT NULL,
  PRIMARY KEY (user_id, source_watermark),
  UNIQUE (user_id, representative_message_id)
);

CREATE TABLE memory_source_turn_messages (
  user_id uuid NOT NULL,
  source_watermark bigint NOT NULL,
  message_id bigint NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
  PRIMARY KEY (user_id, source_watermark, message_id),
  UNIQUE (user_id, message_id),
  FOREIGN KEY (user_id, source_watermark)
    REFERENCES memory_source_turns(user_id, source_watermark) ON DELETE RESTRICT
);

CREATE TABLE memory_source_closures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  source_kind text NOT NULL CHECK (source_kind='conversation_turn'),
  from_watermark bigint NOT NULL,
  through_watermark bigint NOT NULL,
  forget_operation_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (from_watermark <= through_watermark),
  UNIQUE (user_id, forget_operation_id, source_kind, from_watermark, through_watermark)
);
CREATE INDEX memory_source_closures_overlap_idx
  ON memory_source_closures(user_id, source_kind, from_watermark, through_watermark);

ALTER TABLE chat_contexts
  ADD COLUMN memory_mode text NOT NULL DEFAULT 'legacy'
    CHECK (memory_mode IN ('legacy','normalized')),
  ADD COLUMN memory_generation bigint NOT NULL DEFAULT 0,
  ADD COLUMN memory_source_watermark bigint NOT NULL DEFAULT 0,
  ADD COLUMN relationship_profile_input_revision bigint NOT NULL DEFAULT 0;
```

**후보·정규화·hash 계약**:

- extractor의 후보 payload는 extra field를 금지한 versioned schema다. 최상위는
  `{"schema_version":"memory-candidate-v1","candidates":[...]}`이고 각 후보는
  `kind`, `canonical_text`, `importance`, `confidence`, `evidence_message_ids`(비어 있지 않은 unique bigint
  배열)를 필수로, `subject`, `predicate`, `object_json`, `event_time`, `proposed_action`,
  `proposed_target_fact_id`를 nullable로 갖는다. `proposed_action`은
  `ADD|REINFORCE|SUPERSEDE|KEEP_BOTH|IGNORE`만 허용한다. `predicate`와 `object_json`은 둘 다 있거나 둘 다
  없어야 하고, `event_time`은 offset이 명시된 RFC 3339만 받는다. `kind`·`predicate`는 코드 registry의
  값만, 수치는 finite이고 DDL 범위를 만족해야 하며, evidence id는 payload의 `message_ids`에 속해야 한다.
  하나라도 어기면 상태 변경 없이 schema 실패로 처리한다. LLM의 action/target은 관측용 제안이며 아래 코드
  판정을 덮어쓰지 못한다.
- 초기 `normalization_version`은 **`memory-fact-v1`**이다. 첫 정규화 계약이라 기존 버전과 혼동하지 않는
  명시적 초기값이다. 문자열은 placeholder 치환 후 Unicode NFKC, 앞뒤 trim, 연속 Unicode whitespace를
  ASCII 공백 하나로 만든다. `kind`·`predicate`는 registry의 canonical key를 쓰고, `subject`에도 같은 문자열
  정규화를 적용한다. `object_json` 문자열 값은 재귀적으로 같은 정규화를 적용한 뒤 RFC 8785 JCS로
  직렬화한다. `event_time`은 UTC로 바꾸되 시각 정밀도를 버리지 않는 RFC 3339로 직렬화한다.
- hash 입력은 UTF-8 JCS 객체
  `{"v":normalization_version,"kind":kind,"subject":subject|null,"predicate":predicate|null,
  "value":object_json|normalized_canonical_text,"event_time":event_time|null}`다. structured 후보는
  `object_json`, predicate 없는 후보는 정규화한 `canonical_text`를 `value`로 쓴다. `content_hash`는 이
  바이트의 SHA-256 lowercase hex다. 즉 fact의 `(normalization_version, content_hash)`와 marker의
  `(normalization_version, normalized_hash)`는 **같은 산출물**이며, forget 시 `normalized_hash =
  memory_facts.content_hash`를 그대로 복사한다. 이름만 fact의 콘텐츠 식별자와 marker의 deny key 역할을
  구분한다.
- 정규화 결과가 달라지는 변경만 새 version을 발급한다. 기존 fact/marker를 제자리 재해시하지 않고 이전
  normalizer를 version registry에 유지한다. hard filter는 후보를 현재 version으로 한 번 계산하는 데서
  끝내지 않고, 해당 user의 영구 fact marker가 가진 **각 version의 normalizer로도** 계산해
  `(version, hash)`를 비교한다. 지원할 수 없는 marker version이 있으면 publish하지 않고 job을 실패·경보해
  잊은 사실이 되살아나는 무음 우회를 막는다. 검색 SQL은
  `f.normalization_version=m.normalization_version AND f.content_hash=m.normalized_hash`로 비교한다.

**코드 판정 규칙**:

1. schema/정규화 실패는 후보를 쓰지 않는다. closure에 걸리거나 `all`, predicate, 또는 위 versioned hash
   marker에 걸린 후보는 `IGNORE`다. 이 hard filter가 모든 LLM 제안보다 먼저다.
2. 같은 user의 active fact가 가진 각 normalization version으로도 후보 hash를 계산해
   `(normalization_version, content_hash)`가 같으면, 아직 없는 evidence가 하나라도 있을 때 `REINFORCE`하고
   confidence는 `max(existing,candidate)`로 갱신한다. 새 evidence가 없으면 retry/no-op이므로 `IGNORE`다.
3. predicate registry는 각 허용 predicate에 `cardinality=single|multi`를 코드로 고정한다(초기값 = 아래 표).
   registry에 없는 predicate는 schema에서 거부한다. **현행 코드에는 구조화 vocabulary가 없다**(mem0가
   자유문 기억만 만든다) — 따라서 이 registry는 이관이 아니라 **신설**이며, 아래 초기 표로 시작해 W8
   shadow 단계에서 실제 추출 분포를 보고 확장한다. 확장은 `normalization_version`을 올리지 않고
   registry에 항목을 추가하는 것으로 하되, 기존 predicate의 `cardinality` 변경은 버전을 올린다.

   | predicate | cardinality | 뜻 |
   |---|---|---|
   | `residence` | single | 사는 지역 |
   | `occupation` | single | 하는 일·학교 |
   | `household` | single | 함께 사는 형태 |
   | `relationship_status` | single | 연애·결혼 상태 |
   | `current_focus` | single | 지금 매달리는 일(시험·이직 등) |
   | `likes` | multi | 좋아하는 것 |
   | `dislikes` | multi | 싫어하는 것 |
   | `interest` | multi | 관심사·취미 |
   | `person` | multi | 주변 인물과의 관계 |
   | `pet` | multi | 반려동물 |
   | `habit` | multi | 반복하는 습관 |
   | `concern` | multi | 지속되는 고민 |
   | `event` | multi | 있었던 일 |

   `kind`는 `profile|preference|relationship|event|emotion` 다섯 값으로 고정한다. predicate가 없는
   자유문 후보도 `kind`는 반드시 갖는다. identity key는
   정규화한 `(kind, subject, predicate)`로 고정한다. 같은 key의 active fact가 없으면 `ADD`, 있되 hash가
   다르고 `multi`면 `KEEP_BOTH`다. predicate 없는 자유문 후보는 동일 hash만 같은 사실로 보므로, 동일
   hash가 없으면 `ADD`다.
4. hash가 다른 `single` 후보는 evidence가 가리키는 최대 `source_watermark`가 기존 fact evidence의 최대값보다
   클 때만 `SUPERSEDE`, 작으면 오래된 관찰이므로 `IGNORE`다. 두 값이 같은데 내용이 충돌하면 어느 쪽도
   publish하지 않고 reconcile 실패로 경보한다. 이 모호성은 LLM 제안으로 깨지 않는다.
5. 위 순서로만 판정한다. 따라서 `ADD`는 비교할 active identity가 없을 때, `REINFORCE`는 같은 hash,
   `SUPERSEDE`는 더 새로운 single 값, `KEEP_BOTH`는 다른 multi 값, `IGNORE`는 deny/no-op/오래된 값일 때다.
   terminal fact는 되살리지 않는다. forgotten은 marker가 차단하고, marker가 없는 superseded 내용이 새로
   관찰되면 기존 행 수정이 아니라 위 규칙에 따른 새 행을 만든다.

`memory_evidence` insert 전 repository는 source `messages.user_id`와 fact `user_id`가 같음을 한
트랜잭션에서 검증한다. v1 `source_type`은 `conversation_turn`만 허용한다. 일기·루틴·프로필 이벤트는
각자의 watermark/closure 계약이 생기기 전에는 enum CHECK에 추가하지 않는다.

watermark는 **대화 turn당 하나**다. `memory_source_turns.representative_message_id`는 그 turn을 시작해
`post_message`를 발생시킨 inbound user message다. v1에서는 이 메시지가 없는 비사용자 turn을 추출 소스로
enqueue하지 않는다. 같은 turn에서 evidence로 허용할 user/assistant message 전부(대표 메시지 포함)를
`memory_source_turn_messages`에 같은 watermark로 연결하며, 한 message는 정확히 한 watermark에만 속한다.
`memory_evidence.source_id`의 watermark는 이 연결 테이블로 찾는다. coalesce payload의 `message_ids`는
`source_from_watermark..source_through_watermark` 연속 구간에 연결된 메시지의 정확한 합집합이어야 하며,
worker는 누락·추가·타 user id가 있으면 publish하지 않는다. 따라서 turn 대표행은 하나여도 복수
`message_ids` 각각이 closure 검사와 evidence provenance에 참여한다.

**상태 전이와 reconcile 적용**:

- `ADD`: 새 `active` fact와 evidence를 같은 트랜잭션에 insert한다.
- `REINFORCE`: 기존 `active` fact에 새 evidence를 dedup insert하고 confidence/updated_at만 갱신한다.
- `SUPERSEDE`: 새 `active` fact를 먼저 insert하고 기존 `active`를 `superseded`, `valid_to=now()`,
  `superseded_by=new.id`로 바꾼다.
- `KEEP_BOTH`: 양쪽을 `active`로 유지하고 새 fact/evidence만 insert한다. `IGNORE`: 아무 상태도 바꾸지 않는다.
- fact의 `forgotten`과 `superseded`는 terminal이다. 같은 내용을 다시 관찰해도 기존 행을 active로
  되살리지 않고 marker/closure 검사를 거친 새 행 또는 IGNORE로 처리한다.
- insight는 `active→invalidated|superseded`만, profile은 `draft→published|invalidated`,
  `published→superseded|invalidated`만 허용한다. terminal 행을 되살리지 않는다.
- fact/evidence 또는 insight 내용·source·상태가 실제로 바뀐 트랜잭션은
  `chat_contexts.relationship_profile_input_revision`을 정확히 1 증가시키고 그 값을 refresh payload와
  dedup key에 넣는다. no-op/retry/embedding 재색인은 증가시키지 않는다.

**흐름**: `ConversationTurnCommitted` → extract → 정규화·중복 제거 → 기존 active와 비교
(`ADD|REINFORCE|SUPERSEDE|KEEP_BOTH|IGNORE`) → profile refresh 큐잉.
**LLM은 후보와 판정을 제안만 하고 상태 변경은 코드가 한다.**

**추출 실행 정책**: 턴마다 잡을 만들되 **실행은 같은 유저 것을 묶어서** 한다
(턴당 LLM 호출 방지). 묶는 기준은 메시지 수·대화 끊긴 시간·명시적 가치.

각 Phase 2 커밋은 같은 user lock에서 `memory_source_watermark+1`을 배정한 `memory_source_turns`, 그 turn의
`memory_source_turn_messages`, `async_jobs` extraction 행을 메시지와 함께 쓴다. payload에는
`schema_version`, `memory_generation`,
`source_kind`, `source_from_watermark`, `source_through_watermark`, `message_ids`가 필수다. worker가 같은
유저 ready job을 bounded coalesce하더라도 개별 message id를 evidence로 보존한다. 중간 watermark가
closure와 하나라도 겹치면 부분 publish하지 않고 전체를 `source_range_closed`로 끝낸 뒤 열린 source만
새 generation job으로 다시 묶는다. extract→reconcile→profile refresh 후속 job은 직전 단계의 fenced
finalize와 같은 트랜잭션에 enqueue한다.

**v1 범위 제한**: 장기기억 추출 소스는 **`conversation_turn`만** 허용한다.
일기·루틴·프로필 이벤트는 각각 watermark/closure 계약을 갖추기 전까지 넣지 않는다.

---

### W9 — 관계 프로필 (Relationship Profile)

- 칸이 고정된 렌더러로 만든다. 자유 문장 한 덩어리 금지.
  `stance` / `known_facts`(≤5) / `recent_threads`(≤3) / `inferred_tendencies`(≤2, 신뢰도 낮음 표시)
- **≤400 토큰**. 프롬프트의 안정 프리픽스에 넣는다(하루 몇 번만 변경 → 캐시 유지).
- `render_hash`가 바뀔 때만 `version`을 올린다(내용 같으면 캐시 유지).
- **locale별로 만든다**(ja/en 유저는 해당 언어).
- publish는 같은 유저의 source만 참조하는지 검증하고, 렌더 시점에 각 항목의 근거가 지금도
  유효한지 다시 대조한다(재생성 지연 중에도 잊은 내용이 안 들어오게).
- **저장되는 문자열에 실명 금지** — `naming.to_placeholder` 적용(§0.2-1).
- 기존 `chat_contexts.memory_text`(최근 20개 주입)를 이걸로 **대체**한다. 대체 시점은 W10의 cutover.

**draft 작성/publish 검증 계약**:

1. `document_json` 각 항목은 불변 `item_key`와 `source_refs=[{"type":"fact|insight","id":uuid}]`를
   가지며, 모든 ref를 `relationship_profile_sources`에도 같은 draft 트랜잭션에서 쓴다.
2. publish 트랜잭션은 `chat_contexts.user_id=:user_id FOR UPDATE`와 해당 `(user_id,locale)`의 현
   published 행을 `FOR UPDATE`로 잠근다.
3. JSON ref와 edge 행이 `type/id/item_key`까지 양방향으로 정확히 같아야 한다.
4. 모든 edge는 같은 user의 현재 `active` fact/insight를 가리켜야 한다. insight의 모든 source fact도
   같은 user·active이고 fact id/hash/predicate/all forget marker 어느 것에도 걸리지 않아야 한다.
5. draft의 `memory_generation`과 `relationship_profile_input_revision`이 잠근 `chat_contexts` 현재값과
   각각 같아야 한다. generation 불일치는 job을 `succeeded/stale_generation`, revision 불일치는
   `succeeded/stale_profile_input`으로 결과 폐기한다.
6. 3~4를 어기면 publish 없이 job을 `dead/invalid_provenance`로 끝내고 자동 재시도하지 않는다.
7. 모두 통과하면 기존 published를 먼저 `superseded`로 바꾸고 draft를 `published`,
   `published_at=clock_timestamp()`로 바꾼 뒤 한 번만 commit한다. 두 UPDATE 사이 commit 금지다.

renderer도 매 턴 edge/JSON 일치와 source의 active/marker 상태를 다시 검사해 무효 source가 하나라도
있는 항목은 렌더하지 않는다. 따라서 refresh 지연 중에도 forgotten 내용은 projection에서 노출되지 않는다.
`render_hash`가 같으면 새 version/publish를 만들지 않고 job을 `succeeded/unchanged`로 끝낸다.

---

### W10 — forget과 cutover

**legacy write 차단 DDL**을 cutover 전에 적용한다. mode-aware `_save_memory`도 conflict UPDATE에
`WHERE chat_contexts.memory_mode='legacy'`를 붙인다.

```sql
CREATE OR REPLACE FUNCTION guard_normalized_memory_snapshot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='INSERT' AND NEW.memory_mode='normalized' THEN
    NEW.memory_text := NULL;
    NEW.memory_refreshed_at := NULL;
  ELSIF TG_OP='UPDATE' AND OLD.memory_mode='normalized' THEN
    NEW.memory_mode := 'normalized';
    NEW.memory_text := NULL;
    NEW.memory_refreshed_at := NULL;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER chat_contexts_normalized_snapshot_guard
BEFORE INSERT OR UPDATE ON chat_contexts
FOR EACH ROW EXECUTE FUNCTION guard_normalized_memory_snapshot();
```

**cutover 절차**는 다음 순서로만 수행한다.

1. additive DDL/trigger 배포 후 API-A/API-B/consumer-A/consumer-B heartbeat가 모두 같은 mode-aware
   release인지 배포 inventory와 대조한다. 하나라도 구버전·누락·stale이면 **전 cohort gate를 닫는다**.
2. 기존 turn의 inbound user 대표 메시지를 `(created_at,id)` 고정 순서로 `memory_source_turns`에, 그 turn의
   모든 user/assistant 메시지를 `memory_source_turn_messages`에 backfill하고 user별 최대 watermark와
   `chat_contexts.memory_source_watermark`가 같은지 확인한다. enqueue/replay/finalize closure guard를 먼저 켠다.
3. 대상 user의 shadow extract/reconcile 완료 watermark가 2와 같고, 해당 locale의 publish 검증을 통과한
   Relationship Profile이 준비됐는지 확인한다. 하나라도 아니면 그 user는 legacy에 둔다.
4. 짧은 트랜잭션에서 `chat_contexts` user 행을 `FOR UPDATE`로 잠그고 1~3 조건을 fresh read한다.
5. 같은 UPDATE로 `memory_mode='normalized'`, `memory_generation=memory_generation+1`,
   `memory_text=NULL`, `memory_refreshed_at=NULL`을 확정하고 commit한다.
6. 다음 읽기부터 profile/normalized search만 사용한다. 장애·빈 성공·profile 미생성에도 mem0 또는
   legacy 문자열로 fallback하지 않고 빈 기억으로 fail-open하며 경보한다.
7. cohort replay에서 normalized legacy fallback 0, stale generation 폐기, closed-range enqueue/replay/
   finalize 거부를 확인한 뒤에만 다음 cohort로 넓힌다. 구버전 전부 제거 뒤에도 trigger는 contract
   migration까지 유지한다.

forget은 normalized cutover와 제품의 확인 UX 결정이 끝난 user에게만 성공 응답한다. legacy user에게는
성공으로 가장하지 않는다. **forget 7단계**는 한 트랜잭션에서 `chat_contexts` user 행을 `FOR UPDATE`로
잠근 뒤 정확히 아래 순서로 실행한다.

1. 대상 fact evidence의 최소/최대 source watermark(또는 predicate/all의 cut watermark)를 확정하고
   `memory_source_closures`에 영속 기록한다.
2. `memory_generation`과 `relationship_profile_input_revision`을 각각 1 증가시킨다.
3. scope별 forget marker를 기록한다. fact scope는 `fact_id`와 함께 그 행의 `content_hash`를
   `normalized_hash`로, 그 행의 `normalization_version`을 같은 이름의 marker 컬럼으로 그대로 복사한다.
   사용자 marker의 `expires_at`은 NULL이다.
4. 같은 user의 matching active fact를 `forgotten`, `valid_to=now()`, `updated_at=now()`로 바꾼다.
5. `memory_insight_sources.user_id=:user_id`에서 그 fact를 근거로 하는 active insight를 모두
   `invalidated`, `valid_to=now()`로 바꾼다.
6. 같은 user의 profile source가 4~5의 fact/insight를 참조하면 published profile을 `invalidated`로 바꾼다.
7. 2에서 얻은 generation/revision으로 profile refresh job과 외부 vector delete job을 직접 enqueue하고
   commit한다. 이는 같은 Postgres 안의 파생 무효화이며 saga/exactly-once를 요구하지 않는다.

**모든 memory 잡의 finalize**는 (a) source 구간이 닫혔는지 (b) generation이 최신인지를 확인하고,
어긋나면 결과를 버리고 `succeeded` + 사유 코드로 끝낸다.

검사 순서는 closure overlap이 먼저다. 겹치면 `succeeded/source_range_closed`; 겹치지 않지만 generation이
다르면 `succeeded/stale_generation`; profile revision만 다르면 `succeeded/stale_profile_input`이다.
queued/running handler 전부가 finalize 직전에 marker도 다시 검사한다. vector 삭제 실패는 별도 job으로
재시도하지만 검색은 항상 Postgres active+marker hard filter를 먼저 적용하므로 즉시 비노출이다.

retention 종료 시에는 closure 존재·source backfill 완료·enqueue/replay/finalize 거부·외부 vector 삭제를
먼저 검증한다. 그 뒤 한 트랜잭션에서 영향 profile invalidation → profile source edge → insight source edge
→ insight → evidence → fact 순으로 hard delete하고 **마지막 statement로 marker를 삭제**한다. deferred
marker FK 때문에 중간 실패는 전부 rollback된다. closure는 계정 삭제 전 삭제하거나 축소하지 않는다.

**normalized 유저는 legacy 문자열로 fallback하지 않는다.** 빈 결과는 빈 기억으로, 저장소 장애는
기억 없는 응답으로 처리하고 경보를 남긴다. 현행 "빈 성공이면 과거 문자열 재사용"
(`chat.py:276-294`)은 legacy 모드에서만 유효하다.

---

### W11 — 대화 요약 checkpoint

```sql
CREATE TABLE conversation_checkpoints (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  through_message_id bigint NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
  summary text NOT NULL,
  version text NOT NULL,
  source_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, through_message_id, source_hash)
);
CREATE INDEX conversation_checkpoints_latest_idx
  ON conversation_checkpoints(user_id, through_message_id DESC);
```

**생성 조건과 사용 순서**:

1. Phase 2에서 유저·assistant 메시지를 insert한 결과 segment가 `context_reset_messages=40` 또는
   `context_reset_chars=30000` 중 하나에 도달할 때만 summary job을 그 메시지와 같은 트랜잭션에
   enqueue한다. handler가 claim하는 시점에는 원본 메시지가 모두 커밋돼 있어야 한다.
2. 최신 tail을 현행 `context_keep_messages=20`과 `context_keep_chars=12000`을 모두 만족하도록 뒤에서부터
   보존하고, 그 직전 마지막 메시지를 `through_message_id`로 삼는다. 이전 checkpoint보다 앞으로
   진행할 메시지가 없으면 만들지 않는다.
3. `source_hash`는 이전 checkpoint의 `(id,source_hash)`(없으면 빈 값)와 이번 원본 메시지의 정렬된
   `(id,sender,kind,placeholder content)`를 길이-prefix로 직렬화한 SHA-256 hex다. job dedup key는
   `user:{user_id}:through:{through_message_id}:source:{source_hash}:summarizer:{version}`이다.
4. handler는 해당 범위 메시지가 전부 커밋됐고 source hash가 여전히 같은지 확인한 뒤 비동기로 생성한다.
   저장 직전 `naming.to_placeholder`를 적용하며 실명 stem 회귀 검사를 통과해야 한다.
5. 다음 턴은 `through_message_id`가 가장 큰 유효 checkpoint 하나 + 그 이후 메시지를 사용한다.
   checkpoint가 늦거나 실패하면 anchor를 전진시키지 않고 기존 메시지 window로 계속 답한다.
6. 새 summary가 이전 summary를 입력으로 받을 수 있지만 매 **10번째 checkpoint**는 해당 범위 원본으로
   재검증해 누적 왜곡을 계측한다. 10은 품질 근거가 없는 운영 초기값이므로 **측정 후 조정 필요**다.
7. Summary는 Fact가 아니다. checkpoint나 summary 결과에서 장기 사실 extraction job을 만들지 않고
   원본 `conversation_turn` evidence에서만 추출한다.

---

## 2. 의존 순서

```
W1 회계 정정 ──┐
W2 계측    ────┼──▶ [측정 게이트: p95·턴당 billable 확인] ──▶ W4 ──▶ W5 런타임
W3 상주 컨텍스트┘

W7 잡 플랫폼 ──▶ W8 메모리 정규화 ──┬─▶ W6 도구 4종 ──▶ W5에 연결 ──▶ shadow→카나리
                                    └─▶ W9 관계 프로필 ──▶ W10 forget·cutover ──▶ W11 요약
```

**규칙**:
- W1·W2·W3는 서로 독립이고 다른 것에 의존하지 않는다. **가장 먼저 한다.**
- **측정 게이트를 통과하지 못하면 W4·W5를 production 활성화하지 않는다.** typed gateway와 도구
  repository는 off-path로 구현·테스트할 수 있다.
- W9는 W8 없이는 내용이 없다. W10은 W9 없이 하면 대체할 대상이 없다.
- W6 전체는 W8의 normalized hard filter가 완성된 뒤 등록한다. **임시 mem0 search 경로는 없다.**
- W7 consumer를 먼저 상주 실행하고, 기존 전역 틱의 각 producer/handler는 해당 queue로 옮겨 health의
  queue별 ready/running/dead·oldest age가 보인 뒤 하나씩 비활성화한다. 구·신 producer가 겹치는 동안은
  `(job_type,dedup_key)`와 `ON CONFLICT DO NOTHING`으로 합친다.

---

## 3. 제약 검증 계산 (근거)

### 3.1 턴 수 — **비용 근거의 도구 사용률 상한은 두지 않는다**

#### 3.1.1 W1 회계 정정 효과 (선행 조건)

```
현재 billable 구성(프로덕션 실측): cache_read 65% + output 32% + input 2%
읽기 0.5→0.1  : 0.65 × (0.1/0.5) = 0.130
쓰기 0→1.25   : 일반입력 2%가 쓰기 버킷으로 이동 → 0.02 × 1.25 = 0.025
출력          : 0.320 (가중치 6.0 불변)
합계 = 0.475  → 턴당 billable 약 48% → 같은 150k 한도에서 턴 수 약 2.1배
```
정정 후 구성이 뒤집힌다 — **출력이 67%로 지배적**이 되고 캐시 읽기는 27%로 내려간다.
따라서 이후 계산에서 언어별 출력 토큰 수가 턴 수를 좌우한다(§3.1.5).

#### 3.1.2 턴당 토큰 실측 역산

현재 한도 150k에서 헤비 유저 42턴이라는 실측값과 위 구성비로 역산한 턴당 토큰:

| 항목 | 토큰 | 정정 후 billable 기여 |
|---|---:|---:|
| 캐시 읽기(프리픽스) | 4,640 | 464 |
| 출력 | 190 | 1,140 |
| 새 유저 메시지(쓰기 버킷) | 71 | 89 |
| **합계** | | **1,693** |

→ 도구 미사용 시 **150,000 / 1,693 ≈ 89턴**(현재 42턴의 2.1배).

#### 3.1.3 도구 100% 사용 시 (상한 검증)

> **누가 이 제약에 걸리는가**: 일 한도에 실제로 도달하는 유저다. 프리픽스가 작은 라이트 유저는
> 애초에 150k를 소진하지 않으므로 턴 수가 billable로 제한되지 않는다. 따라서 아래는 **한도를
> 소진하는 헤비 유저(프리픽스 4,640tok)** 기준이며, 그게 이 제약의 binding case다.

**먼저 각 기호가 무엇인지.**

| 기호 | 설정 키 | 실제로 무엇인가 |
|---|---|---|
| `D` | `agent_decide_max_tokens` | **첫 번째 AI 호출의 출력 길이 상한.** 도구 턴은 AI를 2회 부르는데, 1회차는 "어떤 도구를 부를지" 정하는 호출이고 그 출력은 사람이 읽을 답변이 아니라 함수 호출 지시문(`{"tool":"search_memory","query":"…"}`)이다. 도구 1개당 ~50tok이므로 192면 3개에 충분 |
| `T` | `agent_tool_result_budget_tokens` | **도구 결과를 2회차 프롬프트에 넣을 때의 한 턴 합계.** 600tok ≈ 한국어 900자. 기억·일기·루틴 결과를 전부 합쳐 이 안에 들어와야 하며 초과분은 잘린다 |

**왜 계수가 7.25와 1.25인가.** 토큰은 종류별로 단가가 다르다(W1-1·W1-2의 `bill_weight`: 출력 6배, 캐시 쓰기 1.25배, 캐시 읽기 0.1배).
- `T`는 2회차의 **입력**으로만 들어가므로 **1.25배** 한 번.
- `D`는 두 번 계산된다 — 1회차에서 **출력**(6배), 그리고 그 함수콜이 대화 기록에 남아 2회차의
  **입력**으로 다시 들어가(1.25배). 합쳐서 **6 + 1.25 = 7.25배**.

**왜 예산이 2,307인가.**
```
헤비 유저 현재 42턴 → 20% 이내 = 최소 33.6턴
150,000 ÷ 33.6 = 턴당 4,464tok 까지 허용
도구 턴의 고정비 2,157 = 캐시읽기 464×2(호출 2회) + 최종 출력 1,140 + 새 메시지 89
4,464 − 2,157 = 2,307  ← D와 T 에 쓸 수 있는 나머지
```

---

도구 턴은 LLM 2회다. 첫 홉 출력 상한을 `D`, 도구 결과 턴 합계를 `T`(토큰)라 하면:

```
호출1(도구 결정): 464(캐시읽기) + 6D(출력) + 89(새 메시지, 쓰기버킷)   = 553 + 6D
호출2(최종 답변): 464(캐시읽기) + 1.25(T + D)(신규입력) + 1,140(출력)  = 1,604 + 1.25T + 1.25D
도구 턴 = 2,157 + 7.25D + 1.25T
```

**-20% 경계**: 턴 수 ≥ 0.8 × 42 = 33.6 → 턴당 billable ≤ 150,000 / 33.6 = **4,464**
```
2,157 + 7.25D + 1.25T ≤ 4,464   →   7.25D + 1.25T ≤ 2,307
```

**⚠️ 이 부등식이 상한값을 강제한다.** W5·W6의 상한을 그대로 다 쓰면 위반한다 —
초안값이던 첫 홉 출력 256tok + 도구 3개 최대 반환(4,000+2,000+2,000자 ≈ 5,300tok)이면
도구 턴이 **10,638**, 약 **14턴(-66%)**이다. 따라서 다음 두 값을 확정한다.

| 설정 | 값 | 근거 |
|---|---:|---|
| `agent_decide_max_tokens` (=D) | **192** | 함수콜 JSON 3개(각 ~50tok)에 충분 |
| `agent_tool_result_budget_tokens` (=T) | **600** | 턴 합계. W6의 도구별 글자 상한보다 **이쪽이 우선**하며 초과분은 잘라낸다 |

검산: `7.25(192) + 1.25(600) = 1,392 + 750 = 2,142 ≤ 2,307` ✅
```
도구 턴 = 2,157 + 2,142 = 4,299   →   150,000 / 4,299 ≈ 34.9턴   →   42턴 대비 -17%
```

**결론: 위 두 상한을 지키는 한 도구 사용률 100%여도 -17%로 제약(-20%)을 만족한다.
따라서 비용 근거의 사용률 캡은 두지 않는다**(`agent_canary_pct`는 롤아웃 속도 조절용이지 비용 캡이 아니다).

**여유는 165다**(2,307 − 2,142). 기본값에서 올릴 수 있는 폭은 `7.25·ΔD + 1.25·ΔT ≤ 165` 를 만족하는
만큼이며, 소폭이면 **동시 상향도 가능하다**(예: `D=193, T=601` → 2,150.5 ✅).
한쪽만 올릴 때의 최대치가 각각 `D=214`(T=600 유지) 또는 `T=732`(D=192 유지)이고, 이 둘을 **동시에**
적용하면(`D=214, T=732` → 2,466.5) 위반한다.
**두 값은 W5-1의 override 검증에서 부등식으로 함께 확인한다** — 개별 범위 통과만으로는 부족하다.

#### 3.1.4 이 결론의 전제 (하나라도 깨지면 재계산)

1. **W1이 먼저 배포될 것.** 회계 정정 없이 도구를 켜면 옛 계산이 적용돼 최악 -23.5%로 제약을 넘는다.
   구현 순서(§2)에서 W1이 W5·W6보다 앞인 이유가 이것이다.
2. **`free_launch_token_limit`을 150k로 유지할 것.** 실비용 기준으로 낮추면 여유가 사라진다.
   참고: 150k × luna 입력 $0.20/M = **유저당 월 상한 $0.90**이다(`config.py:129`의 "월 $4.5" 주석은
   luna 가격 인하 전 값이라 **stale** — W1에서 함께 정정할 것).
3. **§3.1.3이 확정한 두 상한을 지킬 것** — `agent_decide_max_tokens=192`(첫 홉 출력)와
   `agent_tool_result_budget_tokens=600`(도구 결과 턴 합계). 이 값은 임의 설정이 아니라
   `7.25D + 1.25T ≤ 2,307` 부등식의 해다. **바꿀 때는 반드시 부등식을 다시 풀 것**(여유 165 안에서만
   가능하며 W5-1 검증이 이를 강제한다). W6의 도구별 글자 상한은
   개별 안전장치일 뿐이고 실제 비용을 막는 것은 턴 합계 예산이다(runner가 절단 담당).

#### 3.1.5 언어별

정정 후 **출력이 billable의 67%**를 차지하므로 같은 의미를 몇 토큰으로 쓰느냐가 턴 수를 정한다.
한국어·일본어는 영어보다 토큰을 더 쓰고(대략 1.5~1.7배), 프리픽스는 세 언어가 비슷하다 —
`system_prompt`가 en 유저에게도 `CAPI_PERSONA`(한국어 본문)를 주고 언어 지시만 바꾸기 때문이다
(ja만 별도 `CAPI_PERSONA_JA`). 따라서 **en > ko ≈ ja** 순으로 턴 수가 나올 것으로 예상되나,
언어별 실측 데이터가 없다. W2 계측에 **언어 태그를 포함**해 ko·ja·en 각각의 턴당 billable을
측정하고, 필요하면 언어별 한도 보정을 별건으로 검토한다. **현재는 측정 필요.**

### 3.2 응답 시간

도구 턴 = step 1(도구 결정) + 도구 실행(병렬) + step 2(최종 답변).
step 2는 출력 길이가 현재 한 턴과 같으므로 **현재 응답시간 + step 1 + 도구 실행**이 하한이다.

5초를 지키기 위한 배분(초기값, W2 측정 후 재조정):
```
step 1 : ≤ 1.2s   (출력 192토큰 상한 = `agent_decide_max_tokens`, §3.1.3)
도구   : ≤ 0.8s   (병렬, per-tool timeout)
step 2 : ≤ 2.5s
여유   :   0.5s   (egress·저장)
```
- 각 LLM 호출 timeout = `min(llm_timeout_s, 남은 데드라인)`. **`llm_timeout_s=60` 그대로 쓰면 안 된다.**
- 남은 시간 < `agent_final_reserve_s`면 도구를 시작하지 않는다.
- **현재 p95가 2.5초를 넘으면 이 배분이 성립하지 않는다** → W2 결과로 판정.

---

## 4. 롤백

| 작업 | 롤백 방법 |
|---|---|
| W1 | 가격 가중치만 되돌릴 때는 config 원복. `TurnUsage` 적용 후 legacy 회계까지 되돌리려면 pre-W1 앱 artifact 재배포 또는 `turn_usage_v2_enabled=False`가 필요하다. 이미 기록·차감한 usage는 소급 변경하지 않는다 |
| W2 | 로그만 추가라 롤백 불필요 |
| W3 | 상주 블록 생성 플래그 off |
| W4 | 신규 함수라 기존 경로 무영향 |
| W5·W6 | `agent_enabled=False` |
| W7 | 잡 생성 중단, 기존 인라인 경로 유지 |
| W8~W10 | 새 cohort cutover/forget을 중단하고 미전환 user는 `legacy` 유지. 이미 `normalized`인 user는 trigger가 downgrade를 금지하므로 legacy로 되돌리지 않고 normalized 빈-memory fail-open을 유지한 채 roll-forward한다 |
| W11 | checkpoint 사용 플래그 off |

DB 변경은 전부 **추가만**(additive) 한다. 구버전이 읽는 동안 파괴적 스키마 변경을 하지 않는다.

---

## 5. 측정·제품 활성화 게이트 (구현 설계 미결 아님)

아래 항목은 이 문서의 코드/DDL 구현 착수를 막지 않는다. 해당 기능을 production에서 켜거나 수치를
올리기 전에만 결정·측정한다. 결정 전 동작은 각 항목에 적힌 fail-closed/비활성 경로다.

1. ~~`free_launch_token_limit` 150k 유지 vs 재보정~~ → **결정됨: 150k 그대로 유지**(2026-08-03).
   유저당 월 상한 $0.90으로 절대액이 작다. 이 전제 위에서 §3.1의 "도구 사용률 비용 캡 없음"이 성립한다.
   나중에 한도를 낮추면 §3.1을 반드시 재계산할 것.
2. **캐시 쓰기 토큰 추정식 검증** (W1-2) — 과금은 요금표로 확인됨(1.25×). API가 토큰 수를
   보고하지 않아 추정하며, 배포 후 실제 인보이스 대조로 오차 5% 이내인지 확인해야 한다.
3. **`user_devices.last_active_at` 갱신 코드 부재** (W3-1) — 검증 전 feature flag off; 검증되면 문서의 4버킷으로 활성화.
4. **`search_diaries` 한국어 검색 방식** (W6) — tsvector+GIN vs pgvector, 측정 후 택1.
5. **"기억해줘/잊어줘" 확인 UX와 범위** (W10) — 제품 결정. 그전까지 분류만 하고 저장 안 함.
6. **안전 경로 구체 정책** — 이 작업에서는 현행 안전 분류·egress 정책을 byte-equivalent로 승계한다.
   새 위기 분류/차단 정책은 별도 제품·법무 승인 전 추가하지 않는다.
7. **언어별 턴당 billable 실측** (§3.1.5) — ko·ja·en 각각. 출력이 billable의 67%를 차지하게 되므로
   언어별 차이가 턴 수를 좌우한다. W2 계측에 언어 태그 포함. 필요 시 언어별 한도 보정을 별건 검토.
   (도구 사용률은 §3.1.3에 따라 비용 근거 상한이 없다 — 실질 상한은 §3.2 지연이다.)

---

## 6. 비규범 참고 자료

- 설계 의도·근거: `agentic-chat-ARCHITECTURE.md`(짝 문서). **구현 계약은 이 문서가 완결한다** —
  두 문서가 어긋나면 이 문서를 따르고 짝 문서를 고친다.
- 워커·배치 구조(전역 틱 폐기·스케줄러/소비자 분리·큐별 실패 도메인)는 짝 문서 §11~12에 있다.
  이 명세의 W7이 그 기반(`async_jobs`)을 만든다.
- 작업 규칙: 레포 `CLAUDE.md`, 메모리 `dev-workflow-rules` · `migration-before-merge` ·
  `persona-prompt-rules` · `prompt-caching-system` · `dialogue-eval-lang-split`
