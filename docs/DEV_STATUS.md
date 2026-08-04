# 몰리 백엔드 — API 개발 체크리스트

> 2026-08-04: 자연스러운 대화에서 일기·기억·루틴·착용 상태를 회상하는 단일 구조를 구현했다.
> 규범 설계는 `agentic-chat-ARCHITECTURE.md` §0, 구현 계약은
> `agentic-chat-IMPLEMENTATION.md` §0.7, 물리 구조는 `ERD.md` §7이 소유한다.
> 이 문서의 상태는 **로컬 구현·회귀 검증 완료, Dev DB/서버 반영 및 E2E 완료, Prod 미적용**이다.

---

## ✅ 대화·기억 새 구조 — Dev 구현·검증 완료 (2026-08-04)

런타임 설계는 `ARCHITECTURE.md` §5.2, 데이터 계약은 `ERD.md` §7, HTTP 계약은 `openapi/openapi.yaml`이다.
PR #90에서 PostgreSQL 정규화 기억·pgvector·관계 프로필·기억 API·일기 검색을 `dev`에 병합했고,
PR #91에서 `python -m worker.consumer`의 이중 모듈 적재로 실제 registry가 비던 문제를 수정했다.
PR #92~#95에서 대화 중심 회상, 기존 사용자 projection 백필, 독립 감사 하드닝과 nullable SQL bind
수정을 Dev에 반영했다. 현재 로컬 전체 테스트는 **1,090개 통과**다.

### 현재 런타임 계약

- mem0, 외부 vector store, legacy/normalized 유저별 분기는 없다.
- 모든 성공 대화 턴은 user/assistant 메시지, 턴 순번, source watermark, `memory_extract`,
  user-message episode projection 잡을 같은 Phase B 트랜잭션에서 만든다.
- consumer는 추출 → 코드 판정 → fact/episode/diary embedding → 관계 프로필 publish를 처리한다.
- 관계 프로필은 매 턴 기본 주입되며 `agent_enabled`와 무관하다.
- `recall_diaries`, `get_routines`, `recall_memory`가 읽기 도구로 등록돼 있다. 존재·개수·목록·전문은
  검색 후 재조회하지 않고 한 라운드에서 완결한다.
- `forget_memory`는 최종 agent hop의 intent만 만들고, 실제 망각은 chat Phase 2 트랜잭션이 처리한다.
- 첫 성공 대화에서 관계 시작 시각과 `kind=welcome` 프롤로그를 같은 트랜잭션으로 한 번만 만든다.
  목록 GET은 쓰지 않는다. welcome과 같은 날짜의 daily 일기는 공존한다.
- `GET/POST /memory*`, `GET /v2/diaries*`, Dev의 `/dev/recall/memory`와
  `/dev/recall/diaries`로 Swagger에서 저장 결과와 실제 agent recall을 각각 검증한다.
- `X-Moly-Capabilities: diary-reference-v1`을 보낸 클라이언트의 전문 요청에는 검증된 DB 원문 카드가
  응답/이력에 붙는다. 카드 전달은 읽음 처리하지 않는다.

### 킬스위치

| 플래그 | 기본 | 켜면 |
|---|---|---|
| `current_turn_context_enabled` | False | 시각·장착 slot/item·테마·오늘 루틴을 서버 소유 system snapshot에 추가 |
| `current_context_last_active_enabled` | False | 마지막 활동 시점 bucket 추가 |
| `agent_enabled` | False | 캐피가 기억·일기·루틴 읽기 도구를 제한된 1라운드에서 호출 |
| `agent_canary_pct` | 0 | agent 대상 사용자 비율 설정 |
| `context_checkpoint_enabled` | False | 긴 대화에서 앵커 밖 구간을 요약·주입 |

기억 추출, 관계 프로필 기본 주입, 명시적 `/memory` API에는 별도 legacy mode나 forget 킬스위치가 없다.

### Dev 완료 근거

1. `20260804_zzz_conversational_recall.sql` 적용 및 제약·RLS 검사 **완료**
2. 기존 사용자 welcome·episode/diary projection backfill 적용 및 consumer drain **완료**
3. Dev 서버 설정(`ENABLE_DEV_ROUTES`, operator allowlist, agent/context 플래그) 반영 및 배포 **완료**
4. 지정 테스트 사용자로 첫 만남 → 일기 전문/개수 → fact/episode 회상 → 루틴/장착 snapshot →
   멱등 replay를 Swagger/E2E로 확인 **완료**
5. 기존 dead 기억 잡 원본 보존 replay 및 queue drain **완료**

Dev DB 최종 E2E 표본은 user message 28건/episode 28건, welcome 1건/diary recall document 1건,
검증된 대화 diary card 1건이며
누락 vector·활성 recall job·dead recall job·생성 시각 이후 잘못 연결된 provenance·삭제 barrier projection이
모두 0이다. 망각 비노출은 fact/episode/diary/focus/history 공통 SQL hard-filter와 contract 테스트로
검증했으며, 지정 사용자의 실제 기억을 파괴하는 E2E forget은 수행하지 않았다.

실제 HTTPS 검증에서 `/dev/recall/diaries`는 welcome 1건의 렌더된 전문, `/dev/recall/memory`는
검증된 episode 5건을 반환했다. 자연어 “우리 처음 만난 날 일기 기억나? 전문 그대로 보여줘”는
`diary-reference-v1` 원문 카드를 만들었고 history 재조회에서도 available card로 재수화됐다.
동일 `Idempotency-Key` 재전송 응답은 field-equivalent JSON이었고, 루틴 2건과 v2 장착 payload도 확인했다.

production은 이 dev 마무리 작업의 대상이 아니며 변경하지 않는다.

---


> 이 문서 하나로 개발 가능하게 API별 핵심만. 상세 계약은 `API_SPEC.md`.
> ☑ = 구현 완료(머지) · ☐ = 미구현. 최종 갱신 2026-07-27

> **해외 출시(RELEASE 3, SOMA-352) 백엔드 반영** — 2026-07-27, PR #75(검증·머지대기) + moly-infra #8:
> - **i18n 완성**: 서버 고정문구(푸시·선발화·구독혜택·웰컴일기) ja풀(SOMA-361), DB 카탈로그 `products/routines.name_i18n`(SOMA-346). resolve(lang)→en→ko→원문 폴백.
> - **워커**: 중복실행 lease = `diary_gen_claims` 클레임테이블(SOMA-373) · 타임존 정시화 15분 케이던스(SOMA-348, infra #8).
> - **보안**: SSV refetch DoS 스로틀 + `/dev` 명시 게이팅(`enable_dev_routes` 기본 off, fail-closed) (SOMA-376).
> - **과거 버그픽스 이력**: 폐기된 mem0의 임베딩 8192 초과 유실 대응(SOMA-385) · 과거 대화 실명 마스킹 백필 506행(SOMA-322).
> - **마이그레이션 적용됨**(프로덕션): `20260727_catalog_i18n.sql`(name_i18n) · `20260727_diary_gen_claims.sql`. public 테이블 24개.
> - ⚠️ **언어 변경 클라 미지원**: `profile.language`는 온보딩 1회 캡처. moly-auth `PATCH /me`·백엔드는 갱신 지원하나 moly-ios가 재전송 안 함(언어 설정 화면 없음) → 시스템 언어 바꿔도 캐피는 온보딩 언어 고정. 클라 후속 필요.

**공통**: Base = **계정/auth `https://moly-server.vercel.app`(moly-auth)** · **그 외 `https://voice.moly.asia`(moly-backend)** — **`/v1` 없음, 루트 경로**. 인증 `Bearer <Supabase JWT>`(웹훅·`/app-config` 제외) · 에러 `{error:{code,message,details}}` · 일 단위 = `activity_date`(로컬 04:00 경계) · 재화·토큰·구독은 서버가 원본(클라 쓰기 없음).

---

## 시스템
- [x] `GET /health` — 헬스체크.
- ~~`GET /app-config`~~ — **제거**(강제업데이트·점검·낮밤 → Firebase 이관). `app_config` **테이블**은 서버 판정용 값(토큰 한도·임계)에 계속 사용.

## 계정 (account) — PR #24 moly-auth 서버로 이관됨
> ⚠️ 아래 엔드포인트는 **moly-auth 서버(Next.js/TS)** 소유. API 계약은 동일. moly-backend엔 읽기 헬퍼(user_id 조회)만 잔류.

- ~~`POST /onboarding`~~ — **moly-auth 이관**(계약 동일: 닉네임·타임존·언어 저장 → `{profile, entitlement}`. `409 ALREADY_ONBOARDED`).
- ~~`GET /me`~~ — **moly-auth 이관**(계약 동일: 부팅 집계 `profile·entitlement·wallet·equipment`. 런칭 무료기간 반영).
- ~~`PATCH /me`~~ — **moly-auth 이관**(계약 동일).
- ~~`GET/PATCH /me/notifications`~~ — **moly-auth 이관**(계약 동일: `morning_diary`·`evening_chat` on/off).
- ~~`POST /me/push-token`~~ — **moly-auth 이관**(계약 동일).
- ~~`POST /auth/logout`~~ — **moly-auth 이관**(계약 동일: push_token 무효화).
- ~~`DELETE /me`~~ — **moly-auth 이관**(같은 PostgreSQL의 사용자 데이터 FK CASCADE 삭제).

## 대화 (chat)
- [x] `GET /chat/state` — 오늘 토큰 사용량·한도·경고임계·`limit_reached`.
- [x] `GET /chat/messages` — 이력(양방향 커서, `anchor_date` 점프). 항상 오래된→최신 정렬.
- [x] `POST /chat/messages` — 전송 → 바라 응답 완성본. **`Idempotency-Key` 필수**, 초과 시 `403 DAILY_LIMIT_REACHED`, `greeting_id` 있으면 선발화 커밋.
- [x] `GET /chat/greeting` — 선발화 1건(`context`별). 토큰 미차감, 같은 context·날짜는 캐시.

## 일기 (diary)
- [x] `GET /diaries` — 레거시 목록(date 커서). `published_at ≤ now`만 노출.
- [x] `GET /v2/diaries` — `welcome|shared_day|capi_day`와 안정 `(display_date,id)` 커서.
- [x] `GET /diaries/{id}` — 상세(`body`, `conversation_ref`).
- [x] `GET /v2/diaries/{id}` — 저자·종류·표시일·실제 사건 시각을 분리한 상세.
- [x] `POST /diaries/{id}/read` — 열람 표시(멱등).

## 건초·충전소 (economy)
- [x] `GET /wallet` — 잔액.
- [x] `GET /wallet/transactions` — 원장 내역(커서).
- ~~`POST /wallet/purchases`~~ — **제거**(건초 IAP JWS 직접검증 → RevenueCat 웹훅으로 이관).
- [x] `GET /charging-station` — 오늘 획득 현황(출석·광고·루틴·건초팩). **`routine_pair`·`attendance`에 `claimed` 추가**(당일 수령 여부 — 체크 해제해도 유지, 재수령 차단). *PR #28*
- [x] `POST /charging-station/attendance` — 출석 +10. `409 ALREADY_CLAIMED`.
- [x] `POST /charging-station/routine-reward` — 루틴 2개 완료 +10. `422 ROUTINE_GOAL_NOT_MET`.

## 광고 (ads) — PR #28 SSV 자동지급 전환
- [x] `POST /reward-ad-sessions` — 광고 시청 전 세션 발급(오늘 한도 확인, 초과 `429`). 반환 `{reward_session_id, admob_user_id, views_used, views_limit}` → 클라가 SSV custom_data/userIdentifier에 실음.
- [x] `GET /webhooks/ad-ssv` — 서명검증 후 **세션으로 자동 +10 지급**. 멱등 = 세션당 1회 + `ssv_transaction_id` UNIQUE, 한도 = 지급 시 원자 체크.
- ~~`POST /ads/reward`~~ — **제거**(클라가 모르는 transaction_id 요구 → 깨진 플로우). 클라는 광고 후 `/wallet` 폴링.
- 테이블 `ad_rewards` → **`reward_ad_sessions`** 로 교체(실 DB 반영됨).

## 상점·꾸미기 (shop)
> v2 현행(신규 클라용) · v1 레거시(구버전 호환 유지)

**v2 현행**
- [x] `GET /v2/shop/products` — 배경·아이템(`owned`/`equipped`/구독전용). hat/glasses 슬롯 분리.
- [x] `GET /v2/inventory` — 보유 목록(구매분만). hat/glasses 분리.
- [x] `GET /v2/inventory/equipment` — 장착 조회. hat/glasses 슬롯.
- [x] `PUT /v2/inventory/equipment` — 장착 교체. hat/glasses 슬롯, `null`=해제.

**v1 레거시**
- [x] `GET /shop/products` — 배경·아이템(`owned`/`equipped`/구독전용).
- [x] `GET /inventory` — 보유 목록(구매분만).
- [x] `GET/PUT /inventory/equipment` — 4슬롯 장착(background 포함). PUT=전체교체, `null`=해제.

**공통**
- [x] `POST /shop/purchases` — 건초 차감 구매. `402 INSUFFICIENT_HAY` / `409 ALREADY_OWNED`.

## 루틴 (routine) — PR #25 요일별 스케줄·주간 통계 추가
- [x] `GET/POST /routines` — 목록/생성. 생성 시 `days_of_week`(배열, 요일별 반복) 포함.
- [x] `PATCH/DELETE /routines/{id}` — 수정/삭제(soft delete). `days_of_week` 수정 가능.
- [x] `POST/DELETE /routines/{id}/complete` — 완료체크/해제(멱등, `(routine,activity_date)`).
- [x] `GET /routines/{id}/statistics` — streak · completed_today · target_count · `this_week{completed_count, by_weekday}`.

## 구독·Entitlement (subscription) — PR #26 #27
- [x] `GET /subscription` — 상태(`status`·`plan`·`expires_at`).
- [x] `GET /subscription/plans` — 플랜·가격·건초증정.
- ~~`POST /subscription/verify`~~ — **제거**(RevenueCat 이관).
- ~~`POST /subscription/restore`~~ — **제거**(RevenueCat 이관).
- ~~`POST /webhooks/appstore`~~ — **제거**(ASSN → RevenueCat 이관).
- [x] `POST /webhooks/revenuecat` — RevenueCat 이벤트 수신(Authorization 헤더 인증, 멱등). 이벤트→구독 활성화·갱신·해지·환불회수·건초IAP 증정 매핑. 실서버 E2E 검증 완료. **App Store/Play Store 다스토어 지원**(payment.py `store` 분기, product `play_store_product_id` 컬럼, RC 스토어 매핑).

**Entitlement 정책 (PR #26 #27)**
- `ads_removed` = **전 등급 항상 true** — 배너 광고 미출시, 추후 재검토.
- **런칭 무료기간** (`app_config.free_launch_until` = `2026-09-01T04:00+09:00`): 해당 시각 이전엔 구독 미보유자도 구독급(`plan=trial`) + 토큰 한도 `free_launch_token_limit`(50,000) 적용. 실 구독자 우선, 기간 종료 시 자동 복귀, fail-safe(DB 조회 실패 시 보수 적용).
- **Entitlement 이중화**: moly-backend(gating) · moly-auth(`/me`)가 각자 계산, `app_config` 값 공유. → 장기 단일 소스화 검토(기술부채).

## 리뷰 (review)
- [x] `POST /review/prompted` — 리뷰 노출 기록(계정당 1회). 노출 판정은 chat 응답 `review_prompt`.

## 문의 (feedback)
- [x] `POST /feedback` — 인앱 문의 저장(204). 슬랙 알림 발송. 테이블 `feedback`.

## 배치 워커 (worker)
- [x] 04:00 — 일기 생성(개인/캐피) *(15분 케이던스 1틱, 멱등)*. 기억은 매 대화 후 durable consumer가 별도 처리.
  - [x] 캐피 자기일기 **날짜별 지정** 지원 — `moly_life_ments.diary_date`(그날 지정본 우선→랜덤 풀 폴백). 입력=`db/capi_diaries.csv`+`scripts/seed_capi_diaries.py`
- [x] 09:00 아침 일기 푸시 · **20:00** 저녁 안부 푸시 *(FCM, 자격증명 등록 대기)*

---

## 프롬프트 캐싱 + 실비용 회계 (2026-07-11)

| 항목 | 상태 |
|---|---|
| 대화 프롬프트 캐싱(3-breakpoint: 페르소나/기억/마지막메시지) — PR #29 머지 | ✅ 라이브검증(캐시히트) |
| 컨텍스트 앵커 append-only + published 관계 프로필 기본 주입 | ✅ |
| 실비용 토큰 회계(billable=input+5·out+0.1·read+1.25·write, ×단가=실청구액) — PR #30 | ✅ |
| 일 한도 30,000 billable = 표준가 $3/월(런칭 한도) | ✅ |
| 마이그레이션 `chat_contexts`+캐시컬럼 실 DB 반영·verify(RLS/grant) 통과 | ✅ |
| 기억 자연어 살균·장애구분·한도 fail-closed | ✅ |
| 개인일기 게이트 토큰→유저 문자수(`diary_min_user_chars`) 분리 | ✅ |
| 대화 모델 Haiku A/B 테스트(페르소나<Haiku 캐시임계 2048 주의) | 🔲 예정 |

## 인프라·백엔드 완료 (2026-07-09)

| 항목 | 상태 |
|---|---|
| EC2 + Docker(ECR) + GitHub Actions CI/CD | ✅ 라이브 |
| SSM Parameter Store 시크릿 관리 | ✅ |
| 워커 systemd timer | ✅ |
| 프로덕션 URL `https://voice.moly.asia` (`/health` 200, `env=production`) | ✅ |
| RevenueCat 웹훅 연결 · test event 200 확인 | ✅ |
| 실 DB 스키마 21테이블 + 가입 트리거 + hay_packs 시드 | ✅ |
| 캐릭터 몰리→바라 전면 개편(페르소나·선발화·일기·푸시 문구) | ✅ |
| PostgreSQL 정규화 기억 + pgvector 검색 + 망각 계약 | ✅(dev 최종 E2E 진행 중) |
| Swagger HTTPBearer 버튼 + `scripts/dev_token.py` | ✅ |
| moly_life_ments 임시 시드 10건(바라 자기일기, weather 배분) | ✅ |
| 유닛 테스트 111개 + 실 Supabase 통합 테스트 | ✅ |
| 계정 API → moly-auth(Next.js/TS) 이관(PR #24) | ✅ |
| 루틴 `days_of_week` + 주간 통계 확장(PR #25) | ✅ |
| `ads_removed` 전 등급 true(PR #26) | ✅ |
| 런칭 무료기간 게이팅 `free_launch_until`/`free_launch_token_limit`(PR #27) | ✅ |

---

## 남은 작업 (대기)

| 항목 | 비고 |
|---|---|
| `app_config` 시드 | `free_launch_until`·`free_launch_token_limit` 시드 완료. 나머지 토큰 4값 확정 필요: 일한도(free/trial/sub), 개인일기 임계 `diary_min_user_chars`(기본 **60**), 리뷰 임계(기본50000), 경고(기본3000) |
| `shop_items` 시드 | 상품·가격·에셋 확정 후 입력. 현재 0행 |
| `moly_life_ments` 카피 교체 | 임시 카피 → 팀 실제 카피 |
| iOS RevenueCat SDK `logIn(Supabase user_id)` | 미적용 시 웹훅 user 매핑 불가 |
| FCM 서비스계정 JSON 등록 | 푸시 워커 미동작 상태 |
| AdMob SSV 콜백 URL 등록 | |
| RevenueCat 대시보드 App Store Connect API Key 등록 | Key ID / Issuer ID / .p8 |
| RevenueCat 웹훅 Authorization 값 SSM 등록 | |
| 실 sandbox 결제 E2E (iOS 구매 → 웹훅 → DB 확인) | |
| AdMob SSV 실 콜백 테스트 | |
| (기술부채) moly-auth·moly-backend entitlement 이중화 → 단일 소스화 | 현재 각자 계산, app_config 공유로 회복력 확보 |
| 정규화 기억 과거 대화 backfill·replay·최종 contract migration | 진행 중 |
| (기술부채) RC `TRANSFER` 이벤트 핸들링 | 엣지 케이스 |

---

## 에러 코드 (프론트 화면 분기용)

> FE는 **`code`로 분기**(안정). `message`는 표시 문구. 모든 에러는 `{error:{code,message,details}}`.
> ⚠️ 코드 추가/변경 시 백엔드가 **알림** → FE 반영. 아래는 현재 정의된 전부(= API_SPEC 부록 B).

| code | HTTP | 의미 | details |
|---|---|---|---|
| `UNAUTHORIZED` | 401 | 토큰 없음/만료/무효 → 재로그인 | — |
| `FORBIDDEN` | 403 | 접근 권한 없음 | — |
| `ALREADY_ONBOARDED` | 409 | 온보딩 완료 후 재호출 → FE는 /me로 이동 | — |
| `VALIDATION` | 422 | 필드 검증 실패(닉네임·메시지 길이·슬롯 등) | `errors` |
| `DAILY_LIMIT_REACHED` | 403 | 대화 토큰 소진(업셀) | — |
| ~~`SUBSCRIBER_ONLY`~~ | ~~403~~ | ~~구독 전용 리소스~~ — **미사용**(코드 미발행) | — |
| `INSUFFICIENT_HAY` | 402 | 건초 부족 | `required`,`balance` |
| `ALREADY_CLAIMED` | 409 | 출석/루틴 보상 중복 | — |
| `ALREADY_OWNED` | 409 | 상점 중복 구매 | — |
| `ALREADY_PROCESSED` | 409 | 결제/광고 트랜잭션 중복 | — |
| `ROUTINE_GOAL_NOT_MET` | 422 | 루틴 2개 미완료 | — |
| `AD_LIMIT_REACHED` | 429 | 광고 일 10회 초과 | — |
| `AD_VERIFY_FAILED` | 422 | SSV 확정 레코드 없음 | — |
| `NOT_OWNED` | 422 | 미보유 장착 | — |
| `INTERNAL` | 500 | 서버 내부 오류(상세 미노출) | — |

**공통**: 모든 에러 `{error:{code,message,details}}` · `404` = `NOT_FOUND`.
