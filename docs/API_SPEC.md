# Moly MVP — API 명세서 (Frontend 연동용)

> 기준: 2026-07-07 회의 확정 · `ARCHITECTURE.md`(구조) · `ERD.md`(데이터). 이 문서가 **앱↔서버 계약·가격 정책의 단일 소스**.
> 대상: iOS 프론트엔드 · 서버 = 단일 API 서버(+배치 워커)
> **통신 = 전부 HTTP 요청-응답(JSON). 스트리밍·소켓·폴링 없음.** 서버가 먼저 보내는 건 푸시(APNs) 알림뿐.
> 필드·enum 명칭은 `ERD.md`와 통일(예: `hay_transaction_type`, `equipment_slot`). DTO 변환 최소화.

---

## 0. 엔드포인트 한눈에 보기

| 그룹 | Method · Path | 설명 |
|---|---|---|
| 시스템 | `GET /app-config` | 부팅 설정(강제 업데이트·점검·낮/밤 구간) — 인증 불필요 |
| 계정 | `GET /me` | 앱 부팅 집계(프로필·등급·토큰·잔액·장착) |
| | `POST /onboarding` | 온보딩 저장(닉네임·타임존·언어) → 체험 시작 정보 반환 |
| | `PATCH /me` · `GET/PATCH /me/notifications` | 프로필·알림 설정 |
| | `POST /me/push-token` · `POST /auth/logout` · `DELETE /me` | 푸시토큰·로그아웃·탈퇴 |
| 대화 | `GET /chat/state` | 오늘 토큰 사용량·한도·경고 임계 |
| | `GET /chat/messages` | 대화 이력(양방향 스크롤 + 날짜 점프) |
| | `POST /chat/messages` | 메시지 전송 → 몰리 응답(완성본) |
| | `GET /chat/greeting` | 선발화(먼저 말 걸기) |
| 일기 | `GET /diaries` · `GET /diaries/{id}` · `POST /diaries/{id}/read` | 목록·상세·열람표시 |
| 구독 | `GET /subscription` · `GET /subscription/plans` | 상태·플랜 |
| | `POST /subscription/verify` · `POST /subscription/restore` | 검증·복원 |
| 건초 | `GET /wallet` · `GET /wallet/transactions` | 잔액·내역(원장) |
| 충전소 | `GET /charging-station` | 오늘 획득 현황 |
| | `POST /charging-station/attendance` · `/routine-reward` · `POST /ads/reward` | 출석·루틴·광고 보상 |
| | `POST /wallet/purchases` | 건초 현금구매(IAP) |
| 상점 | `GET /shop/products` · `POST /shop/purchases` | 카탈로그·구매 |
| 꾸미기 | `GET /inventory` · `GET/PUT /inventory/equipment` | 보유·장착/해제 |
| 루틴 | `GET/POST /routines` · `PATCH/DELETE /routines/{id}` | CRUD |
| | `POST/DELETE /routines/{id}/complete` · `GET /routines/{id}/statistics` | 완료·통계 |
| 리뷰 | `POST /review/prompted` | 리뷰 팝업 노출 기록 (노출 판정은 채팅 응답 `review_prompt`, 9. 리뷰) |
| 웹훅 | `POST /webhooks/appstore` · `/webhooks/ad-ssv` | 구독 상태·광고 SSV(서버-서버) |

---

## 1. 공통 규칙

**Base URL** `https://api.moly.app/v1`
**포맷** `application/json; charset=utf-8`
**인증** 전 엔드포인트 `Authorization: Bearer <Supabase JWT>` (웹훅·`GET /app-config` 제외). 401 = 토큰 만료/무효 → 클라가 Supabase 세션 갱신 후 재시도.

**등급(플랜) 3단** — 2026-07-07 확정 명칭. 서버는 티어를 컬럼으로 저장하지 않고 **조회 시 판정**(ERD 6.1):
| 등급 | 조건 | 혜택 |
|---|---|---|
| **체험** `trial` | 가입 시각 +48시간(2일) 자동 시작 — 절대 시각(하루 중간 종료 가능, 의도된 정책) | 구독 수준(확장 토큰 한도·개인 일기·광고 제거) — **건초 증정·구독 전용 배경 제외**(ERD 6.1 확정) |
| **무료** `free` | 체험 종료 후 | 일 토큰 소량(TBD) + 배너 광고 노출 |
| **구독** `monthly`·`yearly` | 결제 활성(`active`·`grace_period`) | 확장 토큰 한도·개인 일기·광고 제거·구독 전용 배경·건초 증정(플랜별 최초 1회) |

> 클라 게이팅은 `plan` 문자열 분기가 아니라 **`entitlement`의 파생 플래그**(`ads_removed`, `subscriber_theme_unlocked` 등, 2. 계정 참조)로 판단한다. 혜택 정책이 바뀌어도 API는 불변.

**시간·하루 경계**
- 절대시각은 ISO 8601 UTC.
- 일 단위 로직(토큰 리셋·출석·광고·루틴·일기 귀속)의 날짜 = **`activity_date`**(앱 기준일, ERD 명칭) = 유저 로컬 **오전 04:00** 경계.
- 타임존은 **클라가 명시 전송**(IANA, 온보딩에서 최초 전송 / 변경 시 `PATCH /me`) — 표준 HTTP 헤더가 없으므로 자동 추출에 의존하지 않는다.
- 타임존을 바꿔도 **서버가 마지막 적용 경계를 기억해 리셋 되돌림을 차단**(하루 2회 리셋·보상 이중 수령 불가, ERD 3.2).

**토큰 집계(대화 한도) — 정의**
- **집계 대상 = LLM 입력+출력 합산** (`messages.input_tokens + output_tokens`, 모델 API 실측 usage). `kind='normal'`만 합산 — 선발화(greeting)는 미차감.
- **판정 = 사전 차단 + 사후 집계**: ① 요청 시 `tokens_remaining > 0`이면 통과, 0 이하면 `403 DAILY_LIMIT_REACHED` ② 응답 생성 후 실측 사용량을 누적. 출력 토큰은 사후에만 확정되므로 **마지막 응답은 한도를 약간 초과하고 완결될 수 있다(의도된 동작 — 응답을 중간에 자르지 않는다).** 초과폭은 응답 `max_tokens` 상한(1~3문장 분량)으로 유한.
- 초과 상태의 `tokens_remaining`은 **0으로 클램프**해 반환(음수 노출 금지). `limit_reached` = `tokens_remaining == 0`.
- **카운터는 하나뿐**: 일 한도 판정 · 개인일기 임계 · 리뷰 임계(9. 리뷰) 모두 **같은 당일 카운터**(`user_daily_stats.tokens_used`, `activity_date` 기준 절대량)를 본다 — 기능별 별도 집계 없음, 임계값만 다름(값은 서버 `app_config`).

**언어**
- `profile.language` = **앱 콘텐츠 언어**(서버 생성물의 언어, ISO 639-1). 최초값 = 온보딩 시 클라가 보낸 **기기 시스템 언어**, 이후 `PATCH /me`로 변경 가능.
- **서버 생성 콘텐츠(몰리 응답·선발화·일기·푸시 문구)는 유저가 어떤 언어로 입력하든 항상 `profile.language`로 생성** — LLM 시스템 프롬프트에 응답 언어 고정 지시. (MVP 지원 언어 = ko부터, 언어 추가는 서버 페르소나 프롬프트 추가만으로 가능)
- 언어 변경은 **이후 생성분부터** 적용(과거 대화·발행된 일기 재생성 없음). 일기·푸시는 배치 생성 시점의 language 사용.
- 앱 UI 문자열(버튼·라벨·다이얼로그)은 클라 로컬라이제이션(String Catalog) — 서버 무관(10. 클라 전용).

**서버 권위** — 건초·토큰·등급·상품가격은 서버가 원본(원장 = `hay_transactions`, 잔액은 캐시). 클라는 응답값(특히 `balance_after`, `tokens_remaining`)을 캐시로만 반영, 직접 계산 금지. **미확정 수치(한도·임계·낮밤 구간 등)는 서버 `app_config`로 운영** — 클라는 API 응답값만 신뢰, 하드코딩 금지. **클라의 DB 직접 쓰기 없음 — 모든 쓰기는 이 API 경유**(2026-07-07 확정, ERD 8장. RLS는 읽기·심층 방어).

**페이지네이션(커서)** — `?limit=30&cursor=<opaque>` → `{ "data":[…], "next_cursor": null }` (null이면 끝). 대화 이력만 양방향(3. 대화 참조).

**멱등**
- `POST /chat/messages`는 `Idempotency-Key: <uuid>` **필수** (응답 수 초 소요 → 타임아웃 재시도 시 이중 전송·이중 차감 방지).
- 재화 이동 POST는 `Idempotency-Key` 허용. 출석/루틴은 `(user, activity_date)`로 자연 멱등(ERD `user_daily_stats` 유니크), **결제·건초구매·광고는 트랜잭션ID로 자연 멱등**(`/subscription/verify`·`/subscription/restore`·`/wallet/purchases`=JWS transaction_id, `/ads/reward`=`ssv_transaction_id` — 재시도해도 중복 지급 없음, 별도 키 불필요).

**에러 형식**
```json
{ "error": { "code": "INSUFFICIENT_HAY", "message": "건초가 부족합니다.", "details": { "required": 1000, "balance": 640 } } }
```
HTTP: 400 형식 / 401 미인증 / 402 건초 부족(의도적 채택) / 403 **플랜 게이트**(업셀 유도) / 404 없음 / 409 상태충돌 / 422 검증실패 / 429 **횟수 상한** / 5xx 서버. 비즈니스 코드 = 부록 B.

---

## 2. 시스템 · 계정 · 인증

### `GET /app-config`  *(인증 불필요 — 부팅 최초 호출)*
강제 업데이트·점검 게이트 + 클라 렌더용 서버 설정. 값 원본 = ERD `app_config`.
```json
{ "min_supported_version":"1.0.0",
  "maintenance": { "active":false, "message":null },
  "day_night_schedule": TBD }        // 낮/밤 구간 시각(배경 전환용) — 값 미정
```
- `min_supported_version` 미만 → 강제 업데이트 화면. `maintenance.active` → 점검 화면(문구는 서버).
- ⚠️ **`app_config`(ERD 6.2)의 전부가 아니라 클라가 필요한 키만 노출.** 서버 판정용 임계(`diary_llm_min_tokens`·`review_prompt_min_tokens`)는 노출 안 함 — 일기 종류·리뷰 노출은 **서버가 판정해 결과만** 내려줌(일기 `type`, 채팅 `review_prompt`). 유저별 값(`daily_token_limit`·`tokens_remaining`·`personal_diary_token_threshold`)은 `GET /me`·`GET /chat/state`로, 경고 임계(`warning_threshold`)는 `GET /chat/state`로 전달.

### `GET /me`
앱 진입 시 1회. 부팅에 필요한 상태를 한 번에.
```json
{
  "profile": { "nickname":"지우", "timezone":"Asia/Seoul", "language":"ko", "onboarded":true },
  "entitlement": {
    "plan":"trial",                   // trial | free | monthly | yearly (서버가 조회 시 판정 — ERD 6.1)
    "is_subscriber":false,            // monthly·yearly만 true
    "trial_ends_at":"2026-07-09T02:11:00Z",   // 가입 +48h. trial 아니면 null
    "ads_removed":true,               // 배너 광고 숨김 여부 (free만 노출)
    "subscriber_theme_unlocked":false, // 구독 전용 배경 사용 가능 여부 — 구독(active·grace_period)만 true, 체험 제외(확정)
    "daily_token_limit":TBD,          // 등급별 일 토큰 한도(app_config, 값 미정)
    "tokens_used":1200,
    "tokens_remaining":TBD,
    "personal_diary_token_threshold":TBD  // 이 이상 대화하면 '개인 일기'(app_config.diary_llm_min_tokens)
  },
  "wallet": { "balance":640 },
  "equipment": { "background_id":null, "head_id":null, "neck_id":null, "body_id":null }   // null = 해당 슬롯 기본 상태
}
```

### `POST /onboarding`
```json
// req  { "nickname":"지우", "timezone":"Asia/Seoul", "language":"ko" }   // timezone=IANA·language=기기 시스템 언어
// 200  { "profile": {…}, "entitlement": {…} }           // entitlement = 튜토리얼의 "체험 혜택 안내·고지" 데이터 소스
```
- 온보딩 수집은 **닉네임뿐**(최대 10자 — 422 VALIDATION). 근황은 첫 대화에서 자연 수집(채팅→기억 파이프라인), 취침시간은 수집 안 함(알림 시각 고정).
- `profiles.nickname`이 NULL이면 온보딩 미완료 → 클라는 온보딩 화면으로 라우팅(ERD 3.2).
- 온보딩 완료 직후 클라는 **튜토리얼 + 체험 혜택 고지**를 진행(클라 렌더, 10. 클라 전용 참조). 체험은 **가입 시점**에 이미 시작되어 있음.

### `PATCH /me`
`{ "nickname":"…", "language":"ko", "timezone":"…" }` → 200 profile.

### `GET /me/notifications` · `PATCH /me/notifications`
알림은 **아침 09:00(일기 도착) · 저녁 21:00(몰리 안부) 2종 고정** — 시각 커스텀 없음, on/off만(기본 on — 미설정 = true, ERD 6.3). 루틴 알림은 클라 로컬 노티(8. 루틴) — 서버 설정 아님. 충전소 알림 없음.
```json
{ "morning_diary":true, "evening_chat":true }
```

### `POST /me/push-token`
`{ "token":"<APNs>", "platform":"ios" }` → 204. (기기별 등록 = ERD `user_devices`, 토큰 값 UNIQUE로 중복 제거)

### `POST /auth/logout`
`{ "push_token":"<APNs>" }` → 204. **해당 토큰만** 무효화(멀티 기기 안전). 세션 종료는 클라 Supabase signOut.

### `DELETE /me`  (회원탈퇴)
계정(Supabase `auth.users` 삭제 → 전 테이블 CASCADE)를 **동기 삭제 후 204** 반환. **mem0 기억은 FK 밖이라 서버가 mem0 삭제 API를 병행**(ERD 7) — 실패 시 백그라운드 재시도(204는 Supabase 삭제 완료 기준, mem0는 최종적 정리).
- ⚠️ **Apple 구독은 서버가 해지 불가** → 탈퇴 다이얼로그에서 "구독은 App Store에서 별도 해지" 안내 필수(문구는 클라, 몰리 톤).

---

## 3. 대화 (연속 채팅)

> 카톡식 하나의 연속 스레드 — 세션·종료 개념 없음, 과거 이력 전부 스크롤. 대화 한도는 **토큰**으로 측정, 04:00 리셋. 일기·기억 생성은 이 경로가 아니라 서버 일 배치(ARCHITECTURE.md).

### `GET /chat/state`
```json
{ "activity_date":"2026-07-06", "plan":"free",
  "tokens_used":1200, "daily_token_limit":TBD, "tokens_remaining":TBD,
  "warning_threshold":TBD,           // 소진 경고 임계(서버 설정 app_config.token_warning_threshold) — Limit Warning UI 기준
  "personal_diary_eligible":false,   // 오늘 누적이 임계 이상 → 개인 일기 대상
  "limit_reached":false }
```

### `GET /chat/messages`  — 대화 이력 (양방향)
```json
// 기본(최신부터 과거로): GET /chat/messages?limit=30&cursor=…&direction=older
// 일기→그날 점프:        GET /chat/messages?anchor_date=2026-07-05   (그 activity_date 첫 메시지부터)
{ "data":[
  { "id":"…","sender":"moly","content":"왔네. 오늘은 좀 어땠는데?","created_at":"…" },
  { "id":"…","sender":"user","content":"그냥 그랬어","created_at":"…" }
], "older_cursor":"…", "newer_cursor":"…" }   // null = 그 방향 끝
```
- `sender` = `user` | `moly` (ERD `message_sender`). `data`는 항상 **오래된→최신** 정렬. 위로 스크롤 = `older_cursor`, (점프 후) 아래로 스크롤 = `newer_cursor`.
- 날짜 칩은 `activity_date` 기준(04:00 경계).

### `POST /chat/messages`  — 메시지 전송
유저 메시지 → 몰리 응답 **완성본**을 반환(스트리밍 아님). 둘 다 스레드에 영구 저장. **`Idempotency-Key` 필수.** 메시지 길이 상한 있음(422 VALIDATION — 비용 통제, ERD 5.2).
```json
// req
{ "text":"오늘 좀 힘들었어", "greeting_id":"…" }   // greeting_id = 화면에 떠 있던 미커밋 선발화(아래 선발화 참조). 없으면 생략
// 200
{ "greeting": { "message_id":"…", "content":"왔네. 어제 그 발표는 어떻게 됐고?", "created_at":"…" },  // 커밋된 선발화(kind=greeting). 없으면 null
  "user_message": { "message_id":"…", "created_at":"…" },
  "reply": { "message_id":"…", "content":"왔네. 무슨 일 있었고?", "created_at":"…" },
  "tokens_used":1320, "tokens_remaining":TBD,
  "review_prompt":false }                            // true = 리뷰 팝업 노출 시점(9. 리뷰 참조)
```
- `403 DAILY_LIMIT_REACHED` — 토큰 소진 → Limit Reached UI → 구독 유도.
- 응답 생성에 수 초 소요 → 클라는 로딩 표시 + 타임아웃 넉넉히(예 30s). 재시도는 같은 `Idempotency-Key`로.

### `GET /chat/greeting`  — 선발화
앱 진입/알림 진입 시 몰리가 먼저 건네는 말 1건. 토큰 소진 상태에서도 생성 가능(미차감).
```json
// GET /chat/greeting?context=onboarding | home_enter | morning | evening | comeback
{ "greeting_id":"…", "content":"왔네. 어제 그 발표는 어떻게 됐고?" }
```
- **커밋은 클라 주도**: 유저가 답하면 다음 `POST /chat/messages`에 `greeting_id`를 실어 보냄 → 그때 유저 메시지 직전에 `kind='greeting'` 메시지로 스레드 커밋(`created_at` = 커밋 시각). 발급분은 `greetings` 테이블에 보관(대화 이력 아님 — ERD 5.1).
- 미커밋 선발화는 이력(`GET /chat/messages`)에 나타나지 않고 만료 폐기(혼잣말이 안 쌓임).
- **같은 `context`·같은 `activity_date` 재호출 = 같은 건을 200으로 반환**(캐시 — LLM 재호출·에러 없음, 반복 진입해도 새 비용 안 남). 별도 rate-limit 에러 코드 없음.

---

## 4. 일기

> **매일 다음날 아침 09:00 발행, 절대 비지 않음(2-모드):**
> - `type:"personal"` (DB `source='llm'`) — **당일 누적 토큰**(`tokens_used`, 집계 정의 = 1. 공통 규칙)이 임계(`app_config.diary_llm_min_tokens`) 이상 → 대화 기반 관찰(사용자) 일기.
> - `type:"moly"` (DB `source='preset'`) — 임계 미달·**미접속 날 포함 매일** → '몰리의 삶' 멘트 풀(`moly_life_ments`)에서 배정. **유저 내용 반영 없음**(그날그날 몰리 자신의 일기로 남음). 본문은 스냅샷 저장(풀 수정이 과거 일기를 안 바꿈, ERD 5.3).
> - 발행 대상 = **전원 매일**(무료 유저 포함 — 2026-07-07 확정, ERD 5.3 반영 완료).
>
> **열람은 등급 무관 항상 무료** (체험/구독 만료 후에도). 구독 가치는 "개인 일기 발행"이지 열람이 아님.

### `GET /diaries`  (커서)
```json
{ "data":[
  { "id":"…","diary_date":"2026-07-05","type":"personal","weather":"cloudy",
    "preview":"오늘 지우는 회의 얘기를…","published_at":"…","read":false }
], "next_cursor":null }
```
- `published_at ≤ 현재 시각` 건만 노출(배치 생성분의 발행 전 노출 방지). 하루 1건 보장(ERD 유니크 `(user, diary_date)`).

### `GET /diaries/{id}`
```json
{ "id":"…","diary_date":"2026-07-05","type":"personal","weather":"cloudy",
  "body":"7월 5일 토요일 · 흐림\n오늘 지우는 …",
  "conversation_ref": { "anchor_date":"2026-07-05" },   // GET /chat/messages?anchor_date= 점프용 (moly면 null)
  "published_at":"…","first_read_at":null }
```

### `POST /diaries/{id}/read`
열람 표시. → 204. (멱등, 최초 `first_read_at` 기록 — 아침 알림/뱃지 판정에도 사용)

---

## 5. 구독

> Apple StoreKit 2. 서버가 영수증(JWS) 검증 + 혜택/증정 관리(App Store Server API + Server Notifications V2). 스토어 무료체험 없음(앱 자체 2일 체험으로 대체, 1. 공통 규칙).

### `GET /subscription`
```json
{ "status":"active", "plan":"monthly", "auto_renew_enabled":true, "expires_at":"…",
  "in_trial":false, "trial_ends_at":"…" }
```
`status`: `none | active | grace_period | expired | revoked` (ERD `subscription_status` + 구독 이력 없음 = `none`). 앱 자체 체험 중 = `none` + `in_trial:true`.

### `GET /subscription/plans`
```json
{ "plans":[
  { "product_id":"app.moly.sub.monthly","period":"monthly","hay_grant":1000 },
  { "product_id":"app.moly.sub.yearly","period":"yearly","hay_grant":4000 }
], "benefits":["대화 한도 확장","개인 일기 발행","배너 광고 제거","구독 전용 배경","건초 증정"] }
```
> 가격 표시 문자열은 StoreKit에서. **월 ₩5,900 / 연 ₩59,000** (정가 9,900 "상시 할인" 노출).

### `POST /subscription/verify`
결제/갱신 후 검증 → 활성화 + **최초 1회 건초 증정**(월간 1,000 / 연간 4,000).
```json
// req  { "signed_transaction":"<JWS>" }     // plan·product는 서버가 JWS에서 파생 — 클라 값 신뢰 안 함
// 200  { "status":"active","plan":"monthly","expires_at":"…","hay_granted":1000,"balance_after":1640 }
```
- 증정은 **플랜별 최초 1회**(DB UNIQUE로 강제 — ERD `subscription_hay_grants`). 환불 후 재구독해도 재지급 없음 → 이미 받았으면 `hay_granted:0`.
- `422 RECEIPT_INVALID`.

### `POST /subscription/restore`
`{ "signed_transactions":["<JWS>",…] }` → 현재 상태(= `GET /subscription`).
- `409 RESTORE_CONFLICT` — 해당 구독(`original_transaction_id`)이 **다른 계정에 이미 연결됨**(같은 기기·다른 소셜 로그인). 처리 = 거부 + 안내(ERD 4.3 권장안).

### `POST /webhooks/appstore`  *(서버-서버, Apple 서명 검증)*
갱신·해지·환불·grace 수신(`DID_RENEW`·`EXPIRED`·`DID_FAIL_TO_RENEW`·`REFUND` 등) → 상태 동기. 프론트 무관. → 200.
- **환불(`REFUND` → status `revoked`) 시**: 혜택 즉시 회수 — 증정 건초 회수(원장 `refund_revoke`, 회수액 = min(증정량, 잔액) — 잔액 하한 0) + **구독 전용 장착 해제**(equipment 행 삭제 → 기본 복귀).

---

## 6. 건초 · 충전소

### `GET /wallet` → `{ "balance":640 }`  (원본 = `hay_transactions` 원장, balance는 캐시)
### `GET /wallet/transactions`  (커서 — 원장을 그대로 페이지네이션)
```json
{ "data":[ { "id":"…","type":"attendance","amount":10,"balance_after":650,"created_at":"…" } ], "next_cursor":null }
```
`type` = ERD `hay_transaction_type`(부록 A). `amount` = +획득/−소비.

### `GET /charging-station`
```json
{ "activity_date":"2026-07-06",
  "attendance": { "claimable":true, "reward":10 },
  "ad": { "views_used":3, "views_limit":10, "reward_per_view":10 },
  "routine_pair": { "completed_today":1, "required":2, "claimable":false, "reward":10 },
  "hay_packs":[
    { "product_id":"app.moly.hay.300","amount":300 },
    { "product_id":"app.moly.hay.1500","amount":1500 },
    { "product_id":"app.moly.hay.3000","amount":3000 } ],
  "balance":640 }
```
> 팩 가격 확정: 300개 ₩1,500 / 1,500개 ₩6,500 / 3,000개 ₩10,000 (표시 문자열은 StoreKit, 목록 원본 = ERD `hay_packs`).

### `POST /charging-station/attendance`  — 출석(일1회 +10)
`200 { "granted":10,"balance_after":650 }` / `409 ALREADY_CLAIMED` (판정 = `user_daily_stats.attendance_claimed_at`)

### `POST /charging-station/routine-reward`  — 루틴 2개 완료(일1회 +10)
자동 지급 없음, 충전소에서 직접 수령. **수령 후 루틴 체크를 해제해도 회수 없음.**
`200 { "granted":10,"balance_after":660 }` / `409 ALREADY_CLAIMED` / `422 ROUTINE_GOAL_NOT_MET`

### 리워드 광고 (회당 +10, 일 10회) — 2단 구조
```json
// (a) POST /webhooks/ad-ssv   (광고 네트워크 → 서버, 서명 검증. AdMob SSV는 GET 콜백 — SDK 확정 후 형태 고정)
//     시청 확정 레코드 저장. 트랜잭션ID로 멱등(재전송 중복 지급 방지) + 카운터 원자 증가(ERD 4.2).
// (b) POST /ads/reward        (클라 수령 클레임)
// req  { "ssv_transaction_id":"…" }
// 200  { "granted":10,"balance_after":670,"views_used":4,"views_limit":10 }
// 422 AD_VERIFY_FAILED(확정 레코드 없음) | 429 AD_LIMIT_REACHED | 409 ALREADY_PROCESSED(중복 클레임)
```
> 클라는 서명을 다루지 않는다 — 시청 확정은 반드시 서버-서버 SSV로. SSV 콜백이 클레임보다 늦으면 클라는 짧은 재시도(예: 2s 간격 3회).

### `POST /wallet/purchases`  — 건초 현금구매(StoreKit consumable)
```json
// req  { "signed_transaction":"<JWS>" }     // 상품·수량은 서버가 JWS에서 파생. 흐름: pending → 검증 → verified + 원장 지급(ERD 4.6)
// 200  { "amount":1500,"balance_after":2140 }
// 422 RECEIPT_INVALID | 409 ALREADY_PROCESSED(transaction_id UNIQUE)
```

---

## 7. 상점 · 꾸미기

### `GET /shop/products`
배경·아이템(상점 탭 구분 = `slot`: `background` = 배경 탭, 나머지 = 아이템 탭). 보유/장착/잠금 포함.
```json
{ "backgrounds":[
    { "id":"…","name":"벚꽃","slot":"background","price_hay":4000,"is_subscriber_only":false,"owned":false,"equipped":false,
      "assets":{ "day":"…","night":"…","thumbnail":"…" } },
    { "id":"…","name":"구독 전용 배경","slot":"background","price_hay":null,"is_subscriber_only":true,"unlocked":false,"equipped":false,
      "assets":{ "day":"…","night":"…","thumbnail":"…" } } ],
  "items":[
    { "id":"…","name":"모자","slot":"head","price_hay":1000,"owned":false,"equipped":false,"assets":{ "head_layer":"…","thumbnail":"…" } },
    { "id":"…","name":"아령","slot":"body","price_hay":1200,"owned":false,"equipped":false,"assets":{ "body_layer":"…","thumbnail":"…" } } ] }
```
> **가격 확정(서버 원본 `price_hay`)**: 아이템 최소 1,000·+200 단위 / 배경(테마) 최소 4,000·+1,000 단위.
> **배경 에셋 = 낮/밤 2버전**(`assets.day/night`) — 전환 시각은 `GET /app-config`의 `day_night_schedule`, 렌더는 클라(기기 실시각).
> **구독 전용 배경**: `price_hay:null` = 비매품(구매 개념 없음, DB CHECK로 상호 강제 — ERD 4.7). `unlocked` = `entitlement.subscriber_theme_unlocked`. 잠금해제식 — 소유(owned) 행이 생기지 않음. **구독 활성만 사용 가능, 체험 제외(확정).**

### `POST /shop/purchases`
```json
// req  { "product_id":"…" }
// 200  { "product_id":"…","price_hay":1000,"balance_after":640 }   // 예: 잔액 1,640에서 1,000 차감
// 409 ALREADY_OWNED | 402 INSUFFICIENT_HAY | 403 SUBSCRIBER_ONLY(구독 전용 배경은 구매 대상 아님)
```

### `GET /inventory` — 보유 목록(구매분만 — 구독 전용은 미포함, 자격은 entitlement로 판정)
### `GET /inventory/equipment` — 현재 장착
```json
{ "background_id":null, "head_id":"…", "neck_id":null, "body_id":null }
```
### `PUT /inventory/equipment` — 장착 / 해제
**전체 교체(PUT)** — 4개 슬롯 키 모두 필수. **`null` = 해제**(DB에선 슬롯 행 삭제 = 기본 상태: 기본 배경/기본 몰리, ERD 4.9). 홈 즉시 반영.
```json
// req  { "background_id":"…", "head_id":null, "neck_id":null, "body_id":"…" }   // 모자 벗고 아령만
// 200  { "background_id":"…", "head_id":null, "neck_id":null, "body_id":"…" }
// 422 NOT_OWNED — 미보유(구독 전용은 owned 대신 구독 활성이면 장착 가능) | 422 VALIDATION — 슬롯 불일치(DB 복합 FK로도 강제)
```
- 슬롯 4종 독립(`background`·`head`·`neck`·`body` — ERD `equipment_slot`, 추후 확장 가능) → 동시 착용·부분 해제 가능.
- **구독 전용 배경 장착 중 자격 만료** → 서버가 장착 행 삭제(기본 복귀), 다음 `GET /me`·equipment 조회에 반영(ERD 4.9).

---

## 8. 루틴

### `GET /routines`
```json
{ "data":[ { "id":"…","name":"자기 전 스트레칭","frequency_per_week":3,"reminder_enabled":true,"reminder_time":"22:00","completed_today":true } ] }
```
주기 = **주 N회**(`frequency_per_week`, 요일 지정 아님 — DESIGN.pen `Routine / Add / 주 N회`·ERD 5.5 기준).
### `POST /routines`  `{ "name","frequency_per_week":3,"reminder_enabled":true,"reminder_time":"22:00" }` → 201
### `PATCH /routines/{id}` · `DELETE /routines/{id}` (→204. 삭제 = soft delete — 통계 보존, 클라에는 목록 미노출)
### `POST /routines/{id}/complete` — 오늘 완료 → `{ "completed_today":true, "completed_count_today":2 }` (멱등 — ERD 유니크 `(routine, activity_date)`. count = 유저 전체 기준, 충전소(6. 건초·충전소) 연동)
### `DELETE /routines/{id}/complete` — 체크 해제(행 삭제. 이미 수령한 보상은 회수 없음 — 6. 건초·충전소)
### `GET /routines/{id}/statistics` → `{ "streak":5, "last_30_days":[…], "completion_rate":0.7 }` (completions에서 파생 계산 — 산식은 주 N회 기준)
> 루틴 알림은 **클라 로컬 노티**(서버는 `reminder_*` 스케줄 데이터만 보관). 2개 완료해도 자동 지급 없음 → 충전소의 `routine-reward`로 수령(6. 건초·충전소).

---

## 9. 리뷰

**노출 판정 = 서버, 전달 = 채팅 응답.** **당일 토큰**(`tokens_used`, 1. 공통 규칙과 같은 카운터)이 리뷰 임계(`app_config.review_prompt_min_tokens`, 값 TBD)를 **생애 최초로 넘은 시점**부터 `POST /chat/messages` 응답의 `review_prompt:true` — 클라가 `SKStoreReviewController` 호출 후 아래로 기록하기 전까지 유지(유실 방어). 노출은 계정당 최초 1회(`profiles.review_prompted_at`).

### `POST /review/prompted` → 204. (이후 영구 미노출. 보상 없음)
> 계정 탭 '평가하기'는 App Store 링크 직행 — API 없음.

---

## 10. 클라 전용 (API 없음, 참고)
| 기능 | 처리 |
|---|---|
| **튜토리얼 + 체험 혜택 안내·고지** | 온보딩 직후 클라 렌더. 데이터는 `POST /onboarding`·`GET /me`의 `entitlement`(trial_ends_at 등) |
| Limit Warning 노출 | `GET /chat/state`의 `warning_threshold`(서버 설정) 기준으로 클라 렌더 |
| 낮/밤 배경 전환 | 클라 렌더(기기 실시각 + `app-config.day_night_schedule`). 에셋은 `shop/products`·`equipment` |
| 앱 UI 언어 | 클라 로컬라이제이션(String Catalog). 콘텐츠 언어는 서버(`profile.language`, 1. 공통 규칙) |
| 로딩 멘트 6종 | 클라 하드코딩(확정 문구 — 테이블 없음, ERD 5.4 주석. 언어별 로컬라이즈 대상) |
| 로그아웃/탈퇴 다이얼로그 문구 | 클라(몰리 톤). 탈퇴엔 App Store 구독 별도 해지 안내 포함(2. 계정 참조) |
| 캐릭터 레이어 합성(배경+머리+목+몸) | 클라 렌더 |
| 배너 광고 | 클라. 노출 여부 = `entitlement.ads_removed`(free만 노출) |
| 루틴 로컬 알림 발송 | 클라(서버는 스케줄 데이터만) |
| 문의하기·약관 | 클라(메일·웹 링크) |

---

## 부록 A. Enum (ERD와 1:1)
| Enum | 값 | ERD |
|---|---|---|
| Plan(파생) | `trial` `free` `monthly` `yearly` | 티어는 조회 시 판정(6.1) + `plan_type` |
| SubStatus | `none` `active` `grace_period` `expired` `revoked` | `subscription_status` (+무이력 = `none`) |
| DiaryType | `personal`(=`llm`) `moly`(=`preset`) | `diary_source` |
| Weather | `sunny` `cloudy` `rainy` `windy` | `diary_weather` |
| EquipSlot | `background` `head` `neck` `body` (null=해제/기본) | `equipment_slot` |
| HayTxType | `attendance` `ad_reward` `routine_reward` `iap_purchase` `subscription_grant` `shop_purchase` `refund_revoke` `admin_adjustment` | `hay_transaction_type` |
| MessageSender | `user` `moly` | `message_sender` |
| GreetingContext | `onboarding` `home_enter` `morning` `evening` `comeback` | (API 전용 파라미터) |
| NotificationType | `morning_diary` `evening_chat` | `notification_type` (2종 확정) |

## 부록 B. 비즈니스 에러 코드
| code | HTTP | 발생 |
|---|---|---|
| `DAILY_LIMIT_REACHED` | 403 | 대화 토큰 소진 (플랜 게이트 → 업셀) |
| `SUBSCRIBER_ONLY` | 403 | 구독 전용 리소스 |
| `INSUFFICIENT_HAY` | 402 | 건초 부족 |
| `ALREADY_CLAIMED` | 409 | 출석/루틴 보상 중복 |
| `ALREADY_OWNED` | 409 | 상점 중복 구매 |
| `ALREADY_PROCESSED` | 409 | 결제/광고 트랜잭션 중복 |
| `RESTORE_CONFLICT` | 409 | 다른 계정에 연결된 구독 복원 시도(거부+안내) |
| `ROUTINE_GOAL_NOT_MET` | 422 | 루틴 2개 미완료 |
| `AD_LIMIT_REACHED` | 429 | 광고 일 10회 초과 (횟수 상한) |
| `AD_VERIFY_FAILED` | 422 | SSV 확정 레코드 없음 |
| `RECEIPT_INVALID` | 422 | 스토어 영수증 검증 실패 |
| `NOT_OWNED` | 422 | 미보유 장착 |
| `VALIDATION` | 422 | 필드 검증(닉네임 10자·메시지 길이·슬롯 불일치 등) |

## 부록 C. 미확정 (TBD)
1. **일 토큰 한도·임계 값** — `daily_token_limit`(체험·구독/무료) + 개인일기 임계 + 리뷰 임계 + 경고 임계. 단위·집계·판정 규칙은 **확정**(1. 공통 규칙), 수치만 미정 — 전부 서버 `app_config`라 확정 시 배포 불필요.
2. **광고 SDK** — AdMob 등. `/webhooks/ad-ssv` 콜백 형태는 SDK 확정 후 고정.
3. **낮/밤 구간 시각** — `app_config.day_night_schedule`(GET /app-config로 서빙), 값 미정.

## 확정 정책 요약 (2026-07-07)
| 항목 | 값 |
|---|---|
| 구독 | 월 ₩5,900 / 연 ₩59,000 · 건초 증정 월 1,000 / 연 4,000(플랜별 최초 1회, 환불 후 재구독 재지급 없음) |
| 체험 | 가입 후 2일(48h, 절대 시각), 구독 수준 혜택(광고 제거 포함) — 건초 증정·구독 전용 배경 제외 |
| 건초 획득 | 출석 10 / 광고 회당 10(일 10회) / 루틴 2개 완료 10 — 충전소 직접 수령 |
| 건초 IAP | 300개 ₩1,500 / 1,500개 ₩6,500 / 3,000개 ₩10,000 |
| 상점 | 아이템 최소 1,000·+200 / 배경(테마) 최소 4,000·+1,000 · 구독 전용 배경 = 비매품(잠금해제식) |
| 일기 | 매일 09:00 발행. 임계 토큰↑=개인 일기 / 그 외=몰리 자기일기(유저 내용 반영 X). 열람 항상 무료 |
| 리뷰 | 당일 토큰이 임계를 생애 최초로 넘은 시점 1회 노출, 보상 없음 |
| 알림 | 아침 09:00 일기 · 저녁 21:00 안부 — 2종 고정 on/off(기본 on). 일기 내보내기·충전소 알림 = 없음 |
| 루틴 | 주기 = 주 N회. 삭제 = soft delete(통계 보존) |
