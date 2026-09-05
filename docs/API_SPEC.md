# Moly — API 명세 (Frontend 연동)

> 앱↔︎서버 **계약·가격 정책의 설명 문서**. 기계 판독 원본은 `openapi/openapi.yaml`이다. 통신은 전부 HTTP 요청-응답(JSON)이고 스트리밍·소켓·폴링은 없다. 서버 선발신은 FCM 푸시뿐이다(iOS는 APNs로 릴레이).
>

## Base URL

- **계정/auth** (`/me`·`/onboarding`·`/me/notifications`·`/me/push-token`·`/auth/logout`·`DELETE /me`) → **`https://moly-server.vercel.app`** (moly-auth)
- **그 외 전부** → **`https://voice.moly.asia`** (moly-backend)
- **격리 개발/Swagger** → **`https://dev.moly.asia/docs`**. Dev 전용 route는 개발 환경 + 명시 플래그 +
  operator allowlist를 모두 통과해야 하며 운영에는 등록되지 않는다.

**포맷** `application/json; charset=utf-8` · **인증** 전 엔드포인트 `Authorization: Bearer <Supabase JWT>` (웹훅·헬스 제외 — `/health`·`/health/ready` 공개, `/health/deep`·`/health/synthetic`는 `X-Health-Token` 헤더 인증). 401 = 세션 갱신 후 재시도.

---

## ⚡ 2026-07-14 변경점 (선발화 — FE는 이 표만 보면 됨)

**🔴 코드 수정 필요 — 1건**

| 무엇 | 변경 | 안 고치면 |
| --- | --- | --- |
| `GET /chat/greeting` 응답 | `greeting_id`·`content`가 **null일 수 있음**(200 OK). 인사가 없는 정상 상태 | 파싱 실패/크래시. 또는 빈 말풍선 |

**대응**: 두 필드가 null이면 **말풍선을 띄우지 않는다**(에러 아님). 그 외 흐름·경로·에러코드는 그대로.

**왜 바뀌었나** — 선발화가 **하루 1회**로 고정됐다. 그날 유저가 한 마디라도 했거나 이미 인사를 발급했으면 더 내주지 않는다. 기존엔 채팅창에 들어올 때마다 같은 인사를 계속 돌려줘서, 대화 중에 캐피가 뜬금없이 인사하는 것처럼 보였다. (상세 = 3장)

---

## ⚡ 2026-07-13 변경점 (커머스 스키마 리팩토링)

서비스 흐름·경로·에러코드는 전부 그대로. iOS가 대응할 것은 아래가 전부다.

**🔴 코드 수정 필요 — 1건**

| 무엇 | 변경 | 안 고치면 |
| --- | --- | --- |
| `GET /charging-station` 응답 | 키 `hay_packs` → **`hay_products`** (배열 내부 `{product_id, amount}` 동일) | 충전소 건초 IAP 목록만 빈 값 — 그 외 화면 정상 |

**🟢 자동 반영 — 코드 수정 없음 (알고만 있으면 됨)**

| 무엇 | 내용 |
| --- | --- |
| 가입 기본 지급 | 신규 유저는 시작부터 **아이템 3종 보유**(배경 집·운동, 선글라스) — 기본 집 테마는 자동 장착되고 나머지는 미장착. 상점에는 `owned:true`, 재구매 시도는 `409 ALREADY_OWNED` |
| 가입 기본 루틴 | 루틴 목록에 **"이불 정리하기"·"물 마시기" 2건** 기본 존재(주 7회, 리마인더 off — 일반 루틴처럼 수정·삭제 가능) |
| `POST /shop/purchases` 응답 | `order_id` 필드 **추가**(구매 추적용) — 기존 필드 유지, 파싱 안 해도 무방 |

---

## 0. 엔드포인트

| 그룹 | Method · Path | 설명 |
| --- | --- | --- |
| 계정 (moly-auth) | `GET /me` | 부팅 집계(프로필·등급·토큰·잔액·장착) |
|  | `POST /onboarding` | 온보딩 저장(닉네임·타임존·언어) |
|  | `PATCH /me` · `GET/PATCH /me/notifications` | 프로필·알림 설정 |
|  | `POST /me/push-token` · `POST /auth/logout` · `DELETE /me` | 푸시토큰·로그아웃·탈퇴 |
| 대화 | `GET /chat/state` | 오늘 토큰 사용량·한도 |
|  | `GET /chat/messages` · `POST /chat/messages` · `GET /chat/greeting` | 이력·전송·선발화 |
| 일기 | `GET /diaries` · `GET /diaries/{id}` · `POST /diaries/{id}/read` | 목록·상세·열람 |
| 구독 | `GET /subscription` · `GET /subscription/plans` | 상태·플랜 |
| 건초 | `GET /wallet` · `GET /wallet/transactions` | 잔액·내역 |
| 충전소 | `GET /charging-station` · `POST /charging-station/attendance` · `/routine-reward` · `POST /reward-ad-sessions` | 획득 현황·보상·광고 세션 |
| 상점 | `GET /shop/products` · `POST /shop/purchases` | 카탈로그·구매 |
| 꾸미기 | `GET /inventory` · `GET/PUT /inventory/equipment` | 보유·장착 |
| 상점·꾸미기 v2 | `GET /v2/shop/products` · `GET /v2/inventory` · `GET/PUT /v2/inventory/equipment` | hat/glasses 분리 신계약 (구버전은 레거시 경로) |
| 루틴 | `GET/POST /routines` · `PATCH/DELETE /routines/{id}` · `POST/DELETE /routines/{id}/complete` · `GET /routines/{id}/statistics` | CRUD·완료·통계 |
| 리뷰 | `POST /review/prompted` | 리뷰 노출 기록 |
| 문의 | `POST /feedback` | 사용자 문의 접수 |
| 오늘의 운세 | `GET/PUT/DELETE /fortune-profile` | 생년월일·성별 조회·저장·삭제 |
|  | `GET /daily-fortune/status` · `POST /daily-fortune/reveal` · `POST /daily-fortune/ad-sessions` | 상태·공개·운세 전용 광고 세션 |
| 설치 귀속 | `POST /attribution/meta-referrer/decrypt` | Android Meta 설치 리퍼러의 `utm_content` 복호화(로그인 전 공개 경로) |
| 웹훅 | `POST /webhooks/revenuecat` · `GET /webhooks/ad-ssv` | 구독·광고 SSV(서버-서버) |

---

## 1. 공통 규칙

### 등급(플랜) — 서버가 조회 시 판정(저장 아님)

| 등급 | 조건 | 혜택 |
| --- | --- | --- |
| `trial` | 가입 +48시간 | 확장 토큰 한도·개인 일기·광고 제거 (건초 증정 제외) |
| `free` | 체험 종료 후 | 일 토큰 소량 + 배너 광고(현재 미출시 → 전 등급 `ads_removed=true`) |
| `monthly`·`yearly` | 결제 활성(`active`·`grace_period`) | 확장 토큰·개인 일기·광고 제거·건초 증정(플랜별 최초 1회) |
- 클라 게이팅은 `plan` 문자열보다 `entitlement` 파생값을 우선한다. `subscriber_theme_unlocked`는 호환을 위해 남은 필드이며 현재 꾸미기 접근 제어에는 사용하지 않는다.
- `entitlement`는 **moly-auth `/me`** 가 내려주고, 서버측 한도 집행은 moly-backend가 자체 계산(두 서버 이중화, 기준값은 공유 `app_config`).

**🚀 런칭 무료 기간 (`2026-10-01 04:00 KST`까지, DB에서 조정 가능)**
- 이 기간엔 구독 없이 **전원 무료** — 등급 `trial`, 일 토큰 한도 = **런칭 한도 150,000**. `/chat/state`·`/subscription` 모두 `in_trial:true`, `trial_ends_at`=런칭 종료. 실제 구독자는 항상 우선.
- 종료일 `app_config.free_launch_until`·한도 `free_launch_token_limit` → **재배포 없이 조정**. 종료 시 자동으로 정상 등급 복귀.

### 시간·하루 경계

- 절대시각 = ISO 8601 UTC. 대화 한도·일기 귀속의 `activity_date`는 유저 로컬 **04:00** 경계다.
- 출석·루틴·광고 보상 날짜는 유저 로컬 **00:00** 경계다. `/charging-station.activity_date`라는 필드명은 호환 때문에 유지하지만 값의 의미는 이 보상 날짜다.
- 타임존은 클라가 명시 전송(IANA, 온보딩 시 / 변경 시 `PATCH /me`). 서버가 마지막 경계를 기억해 리셋 되돌림 차단.

### 토큰 집계(대화 한도)

- 집계 = **LLM 입력+출력 실측 합산**(`kind='normal'`만, 선발화 미차감).
- 판정 = **사전 차단 + 사후 집계**: 요청 시 `tokens_remaining>0`이면 통과(0 이하 = `403 DAILY_LIMIT_REACHED`), 응답 후 실측 누적. 마지막 응답은 한도를 약간 초과하고 완결될 수 있음.
- `tokens_remaining`은 0으로 클램프(음수 없음). `limit_reached` = `tokens_remaining==0`.
- 일 한도·리뷰 임계는 **같은 카운터**(`user_daily_stats.tokens_used`)를 봄, 임계값만 다름(`app_config`).
- **개인 일기 발행 분기는 별도 기준** — 당일 **유저 메시지 문자수**(`app_config.diary_min_user_chars`, 서버 배치가 판정). 응답의 `personal_diary_eligible`·`personal_diary_token_threshold`는 토큰 기준의 **참고 지표**(UI 힌트용)로, 실제 발행 여부와 다를 수 있음.

### 언어

- `profile.language` = 앱 콘텐츠 언어(ISO 639-1). 최초 = 온보딩 시 기기 언어, `PATCH /me`로 변경.
- **서버 생성 콘텐츠(캐피 응답·선발화·일기·푸시)는 입력 언어와 무관하게 항상 `profile.language`**. 변경은 이후 생성분부터.
- 앱 UI 문자열은 클라 로컬라이제이션.

### 서버 권위 · 페이지네이션 · 멱등

- 건초·토큰·등급·상품가격은 서버가 원본. 클라는 응답값을 캐시로만, 직접 계산 금지. 미확정 수치는 `app_config`. **클라 DB 직접 쓰기 없음.**
- 커서: `?limit=30&cursor=<opaque>` → `{ "data":[…], "next_cursor":null }`(null=끝). 대화 이력만 양방향.
- `POST /chat/messages`는 `Idempotency-Key: <uuid>` **필수**. 출석/루틴 = `(user, activity_date)` 자연 멱등. 구독·IAP = RevenueCat 웹훅(transaction_id 멱등). 광고 = `ssv_transaction_id` 멱등.

### 로그인 전 공개 경로

`POST /attribution/meta-referrer/decrypt`는 Android 앱이 로그인 전에 받은 Meta 설치 리퍼러를
복호화하기 위한 예외 경로다. 상태를 저장하지 않으며 요청 본문 길이를 제한한다. 암호문이 없으면
`200 {"attribution":null}`, 복호화 실패는 `422`, 서버 키 미설정은 `503`이다. 정확한 필드 계약은
`openapi/paths/attribution.yaml`을 따른다.

### 에러 형식

```json
{ "error": { "code":"INSUFFICIENT_HAY", "message":"건초가 부족합니다.", "details":{ "required":1000, "balance":640 } } }
```

HTTP: 400 형식 / 401 미인증 / 402 건초부족 / 403 플랜게이트 / 404 없음 / 409 상태충돌 / 422 검증 / 429 횟수상한 / 5xx 서버. (코드 = 부록 B)

---

## 2. 계정 (moly-auth 서버)

> `app_config` 조회 엔드포인트(`GET /app-config`)는 제거됨(강제업데이트·점검·낮밤 = Firebase). `app_config` 테이블은 서버 판정용으로 유지(클라 미노출).
>

### `GET /me` — 부팅 집계

```json
{
  "profile": { "nickname":"지우", "timezone":"Asia/Seoul", "language":"ko", "onboarded":true },
  "entitlement": {
    "plan":"trial",                       // trial | free | monthly | yearly
    "is_subscriber":false,                // monthly·yearly만 true
    "trial_ends_at":"2026-10-01T04:00:00+09:00",  // trial 아니면 null
    "ads_removed":true,                   // 배너 광고 숨김
    "subscriber_theme_unlocked":false,    // 호환 필드. 현재 꾸미기 접근 제어에는 사용하지 않음
    "daily_token_limit":150000,
    "tokens_used":1200,
    "tokens_remaining":48800,
    "personal_diary_token_threshold":2000 // 개인 일기 참고 지표(토큰) — 실제 발행 분기는 문자수(1장)
  },
  "wallet": { "balance":640 },
  "equipment": { "background_id":null, "head_id":null, "neck_id":null, "body_id":null }  // null=기본. ⚠️ 두 서버 필드명 정합성 확인 필요(moly-auth: background_id, moly-backend: theme_id)
}
```

### `POST /onboarding`

```json
// req  { "nickname":"지우", "timezone":"Asia/Seoul", "language":"ko" }
// 200  { "profile":{…}, "entitlement":{…} }
```

- 수집은 **닉네임뿐**(최대 10자 — `422 VALIDATION`). `nickname` NULL이면 온보딩 미완료.

### `PATCH /me`

`{ "nickname","language","timezone" }` → 200 profile.

### `GET /me/notifications` · `PATCH /me/notifications`

알림 = **아침 09:00(일기) · 저녁 20:00(안부) 2종 고정**, on/off만(기본 on).

```json
{ "morning_diary":true, "evening_chat":true }
```

### `POST /me/push-token`

`{ "token":"<FCM registration token>", "platform":"ios" }` → 204. Android는 `platform:"android"`를 사용한다.

### `POST /auth/logout`

`{ "push_token":"<FCM registration token>" }` → 204. 해당 토큰만 무효화(세션 종료는 클라 Supabase signOut).

### `DELETE /me` — 회원탈퇴

`moly-auth`가 먼저 같은 DB에 삭제 장벽을 세워 신규 serving과 늦은 worker publish를 막고,
멱등 응답·일기 reference·job payload 사본을 redaction한 뒤 계정을 삭제한다. 도메인 원본과 파생 기억은
FK CASCADE로 제거되고, backend 삭제 ledger에는 본문 없이 operation/watermark만 남는다. 이 장벽 호출과
최종 204의 오케스트레이션은 `moly-auth`가 소유한다.
- ⚠️ Apple 구독은 서버가 해지 불가 → 탈퇴 다이얼로그에 “구독은 App Store에서 별도 해지” 안내 필수.

---

## 3. 대화

> 카톡식 하나의 연속 스레드(세션 없음). 한도 = **토큰**, 04:00 리셋. 기억 source와 episode job은
> 성공한 매 대화와 원자 저장되고, fact/profile/embedding은 durable worker가 비동기로 완성한다.
>

### `GET /chat/state`

```json
{ "activity_date":"2026-07-06", "plan":"trial",
  "tokens_used":1200, "daily_token_limit":150000, "tokens_remaining":148800,
  "warning_threshold":3000,          // 소진 경고 임계
  "personal_diary_eligible":false,   // 개인 일기 참고 지표(토큰 기준 UI 힌트 — 실제 발행 분기는 문자수, 1장)
  "limit_reached":false }
```

- `daily_token_limit`·`tokens_remaining`은 `int|null` — 한도 미설정(무제한) 시 `null`.

### `GET /chat/messages` — 이력(양방향)

```json
// 기본:      ?limit=30&cursor=…&direction=older
// 날짜 점프: ?anchor_date=2026-07-05  (그 activity_date 첫 메시지부터)
{ "data":[
  { "id":"…","sender":"moly","content":"왔네. 오늘은 좀 어땠는데?","created_at":"…" },
  { "id":"…","sender":"user","content":"그냥 그랬어","created_at":"…" }
], "older_cursor":"…", "newer_cursor":"…" }  // null=그 방향 끝
```

- `sender` = `user`|`moly`. `data`는 항상 오래된→최신. 위로 스크롤 = `older_cursor`, 아래 = `newer_cursor`.

### `POST /chat/messages` — 전송 (`Idempotency-Key` 필수)

유저 메시지 → 캐피 응답 완성본. 둘 다 영구 저장. 메시지 길이 상한(`422 VALIDATION`).

```json
// 일반 req  { "text":"오늘 좀 힘들었어", "greeting_id":"…" }
// 운세 req  { "text":"내 운세를 풀어줘", "context_ref":{"type":"daily_fortune","local_date":"2026-08-28","locale":"ko"} }
// 200
{ "greeting": { "message_id":"…","content":"…","created_at":"…" },  // 커밋된 선발화, 없으면 null
  "user_message": { "message_id":"…","created_at":"…" },
  "reply": { "message_id":"…","content":"왔네. 무슨 일 있었고?","created_at":"…",
             "references":[] },
  "tokens_used":1320, "tokens_remaining":48680,
  "review_prompt":false }                             // true = 리뷰 팝업 노출 시점
```

- `403 DAILY_LIMIT_REACHED` = 토큰 소진. 응답 수 초 소요 → 로딩 표시 + 타임아웃 넉넉히, 재시도는 같은 `Idempotency-Key`.
- 같은 key에 다른 body를 보내면 `409 IDEMPOTENCY_KEY_REUSED`. 응답 본문 replay는 24시간, 이후 30일까지는
  body 없는 tombstone으로 중복 턴 생성을 막고 새 key를 요구한다.
- `context_ref`는 이미 공개된 오늘 운세만 한 요청의 서버 컨텍스트로 붙인다. 날짜·프로필·결과가 바뀌면
  `409 FORTUNE_CONTEXT_STALE`이다. 현재 발화가 위기 또는 이어지는 고통 표현이면 운세 참조를 무시하고
  현재 대화를 우선한다. 운세에서 파생된 구간은 장기 기억·관계·일기·대화 요약의 근거로 쓰지 않는다.
- 일기 전문을 대화 안에서 받을 클라이언트는 `X-Moly-Capabilities: diary-reference-v1`을 보낸다.
  이때만 `reply.references`와 `GET /chat/messages`의 `references`에 DB 원문 카드가 붙는다. 카드 전달은
  읽음이 아니며 실제 펼침 시 `POST /diaries/{id}/read`를 호출한다.

### `GET /chat/greeting` — 선발화

> **⚠️ 2026-07-14 변경 — 응답이 비어서 나갈 수 있음(nullable). iOS 대응 필수.**

```json
// ?context=onboarding | home_enter | morning | evening | comeback

// 인사 있음
{ "greeting_id":"…", "content":"왔네. 어제 그 발표는 어떻게 됐고?" }

// 인사 없음 (200 OK, 두 필드 모두 null)
{ "greeting_id":null, "content":null }
```

- **선발화는 하루(`activity_date`) 1회.** `context`와 무관하게 그날 한 번만 나간다.
- 다음 중 하나라도 해당하면 **빈 응답**(`greeting_id`·`content` = null):
    1. 오늘 유저가 이미 한 마디라도 보냈다 → 대화 중 인사 난입 방지
    2. 오늘 인사를 이미 발급했다 → 재진입 시 같은 인사가 다시 뜨지 않음
- 빈 응답 = **정상**(에러 아님). 말풍선을 띄우지 않으면 된다.
- 하루 첫 진입 **시각**에 따라 인사 톤이 갈린다(새벽·아침·낮·저녁·밤). 하루에 여러 시간대 인사를 받는 게 아니라, 그날 처음 만난 시각의 인사 하나를 받는다.
- 토큰 소진 상태에서도 발급(미차감). 유저가 답하면 다음 `POST /chat/messages`에 `greeting_id` 실어 커밋. 미커밋은 이력에 안 남고 만료 폐기.

장기 기억은 대화 내부 기능이다. 앱이 직접 조회·검색·삭제하는 `/memory` API는 없다. 사용자가 자연스럽게
이전 일을 꺼내면 서버가 관련 기억을 검색해 대화에 넣고, 계정 삭제 시 원본 대화와 파생 기억을 함께 지운다.

---

## 4. 일기

> 첫 성공 대화에는 일일 슬롯과 별개인 `kind:"welcome"` 관계 프롤로그가 즉시 생긴다. daily는 다음날
> 09:00 발행하며 `shared_day`(대화 기반) 또는 `capi_day`(캐피의 삶)다. 생성 재료가 없고 안전한 preset도
> 없으면 `diary_generation_results(no_entry)`만 남기며 가짜 빈 일기를 만들지 않는다. 열람은 항상 무료다.

### `GET /diaries`

```json
{ "data":[
  { "id":"…","diary_date":"2026-07-05","type":"personal","title":"지우의 하루","weather":"cloudy",
    "preview":"오늘 지우는 회의 얘기를…","published_at":"…","read":false }
], "next_cursor":"2026-07-04" }
```

- `published_at ≤ 현재`인 발행 상태의 본인 일기만 노출한다.
- 첫 만남 일기는 첫 성공 대화와 같은 트랜잭션에서 한 번만 만들며, 가입이나 목록 조회는 생성 계기가 아니다.
- 날짜 커서 경계에 같은 날짜의 일기가 둘 있으면 둘을 함께 반환하므로 누락되지 않는다. 이때 응답 건수는 요청 `limit`보다 한 건 많을 수 있다.
- `title`: 개인 일기 제목 문자열. 캐피 자기일기(`type:"moly"`)는 `null`.
- `next_cursor`는 `diary_date`(date isoformat, 예: `"2026-07-04"`) — 대화 이력 커서(불투명 숫자)와 다름.

### `GET /diaries/{id}`

```json
{ "id":"…","diary_date":"2026-07-05","type":"personal","title":"지우의 하루","weather":"cloudy",
  "body":"7월 5일 토요일 · 흐림\n오늘 지우는 …",
  "conversation_ref": { "anchor_date":"2026-07-05" },  // 점프용 (moly면 null)
  "published_at":"…","first_read_at":null }
```

### `POST /diaries/{id}/read` → 204 (멱등, 최초 `first_read_at` 기록)

---

## 5. 구독

> RevenueCat이 구독·IAP 진실 소스. 클라는 **RevenueCat SDK** 사용, 로그인 시 **RC `logIn(Supabase user_id)` 필수**(웹훅 매핑). 지원 스토어: App Store · Google Play · Amazon (RC 정규화). 스토어 무료체험 없음.
>

### `GET /subscription`

```json
{ "status":"active", "plan":"monthly", "auto_renew_enabled":true, "expires_at":"…",
  "in_trial":false, "trial_ends_at":"…" }
```

`status` = `none | active | grace_period | expired | revoked`. 체험/런칭 무료 중 = `none` + `in_trial:true`.

### `GET /subscription/plans`

```json
{ "plans":[
  { "product_id":"app.moly.sub.monthly","period":"monthly","hay_grant":1000 },
  { "product_id":"app.moly.sub.yearly","period":"yearly","hay_grant":4000 }
], "benefits":["대화 한도 확장","개인 일기 발행","배너 광고 제거","건초 증정"] }
```

표시 가격은 RevenueCat/스토어 응답을 사용한다. 서버는 가격 문자열을 고정하지 않는다.

### `POST /webhooks/revenuecat` *(서버-서버, 프론트 무관)*

- **인증**: `Authorization` 헤더 = 서버 시크릿(`REVENUECAT_WEBHOOK_AUTH`) 상수시간 비교. 불일치 = 401.
- **본문**: `{ "api_version", "event":{…} }`.
- **이벤트 매핑**:
    - 활성계열(`INITIAL_PURCHASE`·`RENEWAL`·`UNCANCELLATION`·`PRODUCT_CHANGE`·`SUBSCRIPTION_EXTENDED`·`REFUND_REVERSED`) → active + 증정(플랜별 최초 1회)
    - `CANCELLATION`(`CUSTOMER_SUPPORT`=환불) → revoked + 증정 건초 회수
    - 그 외 `CANCELLATION` → 자동갱신 off / `EXPIRATION` → expired / `BILLING_ISSUE` → grace_period / `NON_RENEWING_PURCHASE` → 건초팩 지급
- 스토어 서버 알림(ASSN 등)은 우리 서버에 연결하지 않음(RC가 소비). RC 웹훅 `event.store` 필드는 서버가 정규화(`APP_STORE`·`MAC_APP_STORE`→`app_store`, `PLAY_STORE`→`play_store`, `AMAZON`→`amazon`).

---

## 6. 건초 · 충전소

### `GET /wallet` → `{ "balance":640 }`

### `GET /wallet/transactions`

```json
{ "data":[ { "id":"…","type":"attendance","amount":20,"balance_after":660,"created_at":"…" } ], "next_cursor":null }
```

`amount` = +획득/−소비. `type` = 부록 A.

### `GET /charging-station`

```json
{ "activity_date":"2026-07-06",
  "attendance": { "claimable":true, "claimed":false, "reward":20 },
  "ad": { "views_used":3, "views_limit":5, "reward_per_view":20 },
  "routine_pair": { "completed_today":1, "required":2, "claimable":false, "claimed":false, "reward":20 },
  "hay_products":[
    { "product_id":"com.geniusjun.moly.hay.300","play_store_product_id":"com.geniusjun.moly.hay.300","amount":300 },
    { "product_id":"com.geniusjun.moly.hay.1500","play_store_product_id":"com.geniusjun.moly.hay.1500","amount":1500 },
    { "product_id":"com.geniusjun.moly.hay.3000","play_store_product_id":"com.geniusjun.moly.hay.3000","amount":3000 } ],
  "balance":640 }
```

- **2026-08-17 현재**: `product_id`는 App Store ID, `play_store_product_id`는 Google Play ID다. 현재 세 건초팩은 양쪽 스토어에서 같은 ID를 사용하며 RC SDK 구매 시 실행 중인 스토어의 필드를 선택한다.
- 팩 가격: 300 ₩1,500 / 1,500 ₩6,500 / 3,000 ₩10,000 (표시 문자열은 스토어).

### `POST /charging-station/attendance` — 출석(일1회 +20)

`200 { "granted":20,"balance_after":660 }` / `409 ALREADY_CLAIMED`

### `POST /charging-station/routine-reward` — 루틴 2개 완료(일1회 +20)

`200 { "granted":20,"balance_after":680 }` / `409 ALREADY_CLAIMED` / `422 ROUTINE_GOAL_NOT_MET`. 수령 후 체크 해제해도 회수 없음.

### 리워드 광고 (회당 +20, 일 5회) — 세션 발급 + SSV 자동 지급

```
(a) POST /reward-ad-sessions   (클라, 인증 — 광고 시청 전 세션 발급)
    200 { "reward_session_id":"…", "admob_user_id":"…", "views_used":3, "views_limit":5 }
    429 AD_LIMIT_REACHED (오늘 5회 소진)
    → 클라는 AdMob SSV 옵션에 custom_data=reward_session_id, userIdentifier=admob_user_id 설정
(b) GET /webhooks/ad-ssv       (AdMob→서버, 서명 검증 — 서버-서버, 클라 무관)
    검증 통과 시 해당 세션으로 +20 자동 지급. 멱등 = 세션당 1회 + ssv_transaction_id UNIQUE.
```

- **클레임 API 없음** — 지급은 SSV 콜백이 곧바로 처리. 클라는 시청 종료 후 `GET /charging-station`(또는 `GET /wallet`) 재조회로 반영 확인(SSV 지연 대비 짧은 재시도, 예: 2s×3).
- 건초 IAP는 RevenueCat `NON_RENEWING_PURCHASE` 웹훅으로 지급(주문·결제 기록 포함).

---

## 7. 상점 · 꾸미기

> 레거시 경로(`/shop/products`·`/inventory`·`/inventory/equipment`)는 slot=`theme|head|neck|body` 계약. 구버전 앱 유지. 신버전은 **v2 경로**(`/v2/…`) 사용 — `head` 슬롯을 `hat`·`glasses`로 분리.

### `GET /shop/products` (레거시) · `GET /v2/shop/products`

상점 탭 = `slot`(`theme`=배경 탭, 나머지=아이템 탭).

```json
{ "themes":[
    { "id":"…","name":"봄날","slot":"theme","price_hay":4000,"owned":false,"equipped":false,
      "asset_version":1,
      "assets":{
        "thumbnail_url":"…","detail_url":"…",
        "scene":{ "canvas":{"width":393,"height":852},
                  "character_frame":{"x":0,"y":0,"width":200,"height":300},
                  "character_url":"…",
                  "layers":[{"id":"bg","frame":{"x":0,"y":0,"width":393,"height":852},"z_index":0,"day_url":"…","night_url":"…"}] } } },
    { "id":"…","name":"봄밤","slot":"theme","price_hay":5000,"owned":false,"equipped":false,
      "asset_version":1,"assets":{…} } ],
  "items":[
    { "id":"…","name":"모자","slot":"head","price_hay":1000,"owned":false,"equipped":false,
      "asset_version":1,
      "assets":{"thumbnail_url":"…","detail_url":"…","upright_layer_url":"…"} },
    { "id":"…","name":"아령","slot":"body","price_hay":1200,"owned":false,"equipped":false,
      "asset_version":1,
      "assets":{"thumbnail_url":"…","detail_url":"…","upright_layer_url":"…"} } ] }
```

- 상품 필드: `id·name·slot·price_hay(null=비매품)·owned·equipped·asset_version·assets`.
- `assets` 구조: `thumbnail_url`·`detail_url` 공통. 테마는 `scene`(canvas·character·layers), 착용 아이템은 `upright_layer_url`. 낮/밤 레이어는 `scene.layers[].day_url`·`night_url`.
- `price_hay:null`은 비매품이다. 현재 꾸미기는 구독 전용으로 제한하지 않으며 실제 가격과 노출 여부는 DB 상품 카탈로그가 기준이다.
- v2: `items` 안에 `slot:"hat"`·`slot:"glasses"` 포함(레거시 `head` 없음). 응답 키(`themes`·`items`)는 동일.

### `POST /shop/purchases`

```json
// req { "product_id":"…" }
// 200 { "product_id":"…","order_id":"…","price_hay":1000,"balance_after":640 }
// 409 ALREADY_OWNED | 402 INSUFFICIENT_HAY
```

- **2026-07-13**: `order_id` 추가(주문 기록·CS 추적용) — 클라 필수 사용 아님.

### `GET /inventory` · `GET /v2/inventory` — 보유 목록

→ `{ "data":[ShopProduct, …] }` — 상품 **객체** 배열(레거시·v2 동일 구조, v2는 SlotProductV2). 신규 유저는 기본 지급 3종 포함.

### `GET /inventory/equipment` · `GET /v2/inventory/equipment` — 현재 장착

```json
// 레거시: GET /inventory/equipment
{ "theme_id":"…", "head_id":null, "neck_id":null, "body_id":null }
// v2: GET /v2/inventory/equipment
{ "theme_id":"…", "hat_id":null, "glasses_id":null, "neck_id":null, "body_id":null }
```

### `PUT /inventory/equipment` · `PUT /v2/inventory/equipment` — 장착 교체 (전체 슬롯 필수)

```json
// 레거시 req { "theme_id":"…", "head_id":null, "neck_id":null, "body_id":"…" }
// v2 req    { "theme_id":"…", "hat_id":null, "glasses_id":null, "neck_id":null, "body_id":"…" }
// 200 (동일 형태)
// 422 NOT_OWNED | 422 VALIDATION(슬롯 불일치)
```

- `theme_id` **필수 non-null**(테마는 해제 불가). 나머지 슬롯 `null`=해제.

---

## 8. 루틴

스케줄 = **요일별**(`days_of_week`) 또는 **주 N회**(`frequency_per_week`) 중 하나. `days_of_week`(ISO **1=월…7=일**) 있으면 요일별 모드(`frequency_per_week`는 요일 수로 파생), null이면 주 N회.

> **가입 기본 루틴(2026-07-13)**: 신규 유저는 "이불 정리하기"·"물 마시기" 2건이 기본 생성됨(주 7회, 리마인더 off). 일반 루틴과 동일하게 수정·삭제 가능 — FE 특별 처리 없음.

### `GET /routines`

```json
{ "data":[ { "id":"…","name":"자기 전 스트레칭","frequency_per_week":3,"days_of_week":[1,3,5],"reminder_enabled":true,"reminder_time":"22:00","completed_today":true } ] }
```

`days_of_week`: 요일별이면 `[1,3,5]`, 주 N회면 `null`.

### `POST /routines` → 201

- 주 N회: `{ "name","frequency_per_week":3,"reminder_enabled":true,"reminder_time":"22:00" }`
- 요일별: `{ "name","days_of_week":[1,3,5],"reminder_enabled":true,"reminder_time":"07:30" }`
- `frequency_per_week`·`days_of_week` 중 **하나 필수**. 요일 값 1~7·중복 없이 1개 이상(위반 = `422 VALIDATION`).

### `PATCH /routines/{id}` → 204

모드 전환: `days_of_week:[1,3,5]`=요일별 / `days_of_week:[]`=주 N회(이땐 `frequency_per_week` 동반) / 필드 생략=변경 없음.

### `DELETE /routines/{id}` → 204 (soft delete — 통계 보존, 목록 미노출)

### `POST /routines/{id}/complete` → `{ "completed_today":true, "completed_count_today":2 }` (멱등)

### `DELETE /routines/{id}/complete` — 체크 해제 (수령한 보상 회수 없음)

### `GET /routines/{id}/statistics`

```json
{ "streak":5, "completed_today":true, "target_count":3, "days_of_week":[1,3,5],
  "this_week":{ "completed_count":2, "by_weekday":{"1":true,"2":false,"3":true,"4":false,"5":false,"6":false,"7":false} },
  "last_30_days":["2026-07-06"], "completion_rate":0.7 }
```

- `streak` 연속 시행 일수(단순 달력일) · `completed_today` 오늘 완료 · `target_count` 설정 횟수 · `this_week` 이번 주(월 시작·00:00 보상 경계) 수행 횟수·요일별 완료 · `completion_rate` 최근 4주.
- 루틴 알림은 **클라 로컬 노티**(서버는 스케줄만 보관, 발송 안 함). 2개 완료 보상은 충전소에서 수령.

---

## 9. 리뷰

**노출 판정 = 서버, 전달 = 채팅 응답.** 당일 토큰이 리뷰 임계(`app_config.review_prompt_min_tokens`)를 **생애 최초로 넘은 시점**부터 `POST /chat/messages` 응답의 `review_prompt:true`(계정당 1회).

### `POST /review/prompted` → 204 (이후 영구 미노출, 보상 없음)

---

## 10. 문의

> Bearer 인증. 저장 후 슬랙 알림(백그라운드, best-effort).

### `POST /feedback` → 204

```json
// req { "message":"앱이 정말 좋아요!", "contact":"user@example.com" }
// 204 No Content
```

- `message`: 1~2,000자 필수.
- `contact`: 이메일·전화·인스타 등 자유 입력, 최대 200자, 선택.

---

## 11. 오늘의 운세

> 이번 운영 업데이트에 오늘의 운세와 운세 대화 연결을 포함한다. 애플리케이션 기본 플래그는 비상 차단을
> 위해 OFF로 유지하고 배포 설정에서 활성화한다. 제품 규칙·계산·문구·DB·API 상태 머신의 단일 설명은
> `DAILY-FORTUNE.md`, 정확한 필드 계약은 OpenAPI를 따른다.

- 최초 진입은 `GET /daily-fortune/status`의 `profile_required`를 확인하고 `PUT /fortune-profile`에
  `{ "birth_date":"2002-12-13", "gender":"man" }`을 보낸다.
- 생년월일은 1900-01-01 이상, 사용자 현지 날짜 기준 만 14세 이상이다. 위반 코드는
  `INVALID_BIRTH_DATE`, `UNDER_MINIMUM_AGE`다.
- `POST /daily-fortune/reveal`은 체험·구독이면 `revealed`, 무료면 문구가 없는 `locked`를 반환한다.
- 무료 사용자는 `POST /daily-fortune/ad-sessions`의 값을 AdMob에 넣고, 검증 완료 뒤 status를 다시 조회한다.
- `state`(`profile_required|unseen|locked|revealed`)와 `access`(`included|ad_required|unlocked_today`)를
  따로 분기한다.
- 공개 결과는 `overall`, `categories`, `lucky_color`의 중첩 구조이며 `schema_version=3`이다.
  `X-App-Locale`은 `ko|en|ja`와 `ko-KR|en-US|ja-JP` 같은 지역 태그를 받으며, 실제 반환 언어는
  기본 코드로 정규화되어 `result.locale`에 들어간다. `jp`와 지원하지 않는 언어는 422다.
- 프로필 수정 응답의 `result_invalidated`, `unlock_preserved`를 사용한다. 같은 날 광고 해제 권한은 유지된다.
- 광고 중 프로필이 바뀌어 SSV 후 `unseen + unlocked_today`가 오면 광고 없이 reveal을 다시 호출한다.
- `locked` 상태에는 점수·문구·버전이 없다. 이전 공개 결과와 섞어 사용하면 안 된다.

---

## 12. 클라 전용 (API 없음)

| 기능 | 처리 |
| --- | --- |
| 튜토리얼 + 체험 혜택 고지 | 온보딩 직후 클라 렌더(데이터 = `entitlement`) |
| Limit Warning | `GET /chat/state`의 `warning_threshold` 기준 클라 렌더 |
| 낮/밤 배경 전환 | 클라 렌더(기기 실시각 + Firebase 전환 시각) |
| 앱 UI 언어 | 클라 로컬라이제이션 (콘텐츠 언어는 `profile.language`) |
| 로딩 멘트 6종 | 클라 하드코딩 |
| 로그아웃/탈퇴 다이얼로그 | 클라(캐피 톤, 구독 별도 해지 안내 포함) |
| 캐릭터 레이어 합성 | 클라 렌더 |
| 배너 광고 | 클라 (`entitlement.ads_removed`) |
| 루틴 로컬 알림 발송 | 클라 (서버는 스케줄만) |

---

## 부록 A. Enum

| Enum | 값 |
| --- | --- |
| Plan | `trial` `free` `monthly` `yearly` |
| SubStatus | `none` `active` `grace_period` `expired` `revoked` |
| DiaryType | `personal` `moly` |
| Weather | `sunny` `cloudy` `rainy` `windy` |
| EquipSlot | 레거시: `theme` `head` `neck` `body` / v2: `theme` `hat` `glasses` `neck` `body` (null=해제) |
| HayTxType | `attendance` `ad_reward` `routine_reward` `iap_purchase` `subscription_grant` `shop_purchase` `refund_revoke` `admin_adjustment` |
| MessageSender | `user` `moly` |
| GreetingContext | `onboarding` `home_enter` `morning` `evening` `comeback` |
| NotificationType | `morning_diary` `evening_chat` |

## 부록 B. 비즈니스 에러 코드

> 모든 에러 = `{error:{code,message,details}}`. FE는 `code`로 화면 분기.
>

| code | HTTP | 발생 |
| --- | --- | --- |
| `UNAUTHORIZED` | 401 | 토큰 없음/만료/무효 |
| `FORBIDDEN` | 403 | 일반 접근 거부 |
| `DAILY_LIMIT_REACHED` | 403 | 대화 토큰 소진(업셀) |
| `INSUFFICIENT_HAY` | 402 | 건초 부족 |
| `ALREADY_ONBOARDED` | 409 | 온보딩 완료 후 재호출 |
| `ALREADY_CLAIMED` | 409 | 출석/루틴 보상 중복 |
| `ALREADY_OWNED` | 409 | 상점 중복 구매(기본 지급분 재구매 포함) |
| `ROUTINE_GOAL_NOT_MET` | 422 | 루틴 2개 미완료 |
| `AD_LIMIT_REACHED` | 429 | 광고 일 5회 초과 |
| `AD_VERIFY_FAILED` | 422 | SSV 서명 검증 실패(서버-서버 — 클라 미노출) |
| `PROFILE_REQUIRED` | 404 | 운세 생년월일·성별 미입력 |
| `FEATURE_UNAVAILABLE` | 403 | 운세 개발 기능 비활성 또는 미승인 규칙 |
| `AD_NOT_REQUIRED` | 403 | 운세 광고가 필요하지 않은 상태 |
| `DATE_ROLLOVER` | 409 | 운세 현지 날짜 전환 2분 보호 구간 |
| `NOT_OWNED` | 422 | 미보유 장착 |
| `VALIDATION` | 422 | 필드 검증 |
| `INTERNAL` | 500 | 서버 내부 오류 |

제네릭 HTTP 코드(비즈니스 에러 없음 — FE 분기 불필요): `BAD_REQUEST`(400)·`METHOD_NOT_ALLOWED`(405)·`CONFLICT`(409)·`RATE_LIMITED`(429). 목록 외 상태는 `HTTP_<status>` 형식.

## 부록 C. 확정 정책

| 항목 | 값 |
| --- | --- |
| 구독 | 표시 가격은 스토어 기준 · 건초 증정 월 1,000 / 연 4,000(플랜별 최초 1회) |
| 체험 | 가입 후 2일(48h) · 구독 수준 혜택(건초 증정 제외) |
| 런칭 무료 | `2026-10-01 04:00 KST`까지 전원 무료·일 150,000. `app_config`로 조정 |
| 건초 획득 | 출석 20 / 광고 회당 20(일 5회) / 루틴 2개 완료 20 |
| 건초 IAP | 300 ₩1,500 / 1,500 ₩6,500 / 3,000 ₩10,000 |
| 상점 | 가격·노출 여부는 DB의 활성 상품 카탈로그가 기준. 꾸미기 구독 전용 정책 없음 |
| 가입 기본 세팅 | 아이템 3종 지급(집·운동 배경, 선글라스 — 집 배경만 자동 장착) + 기본 루틴 2개(이불 정리하기·물 마시기, 주 7회) |
| 일기 | 09:00 발행. 임계↑=개인 일기 / 그 외=해당 날짜 지정본이 있을 때만 캐피 자기일기. 열람 항상 무료 |
| 리뷰 | 당일 토큰이 임계 생애 최초 초과 시 1회 노출, 보상 없음 |
| 알림 | 아침 09:00 일기 · 저녁 20:00 안부 — 2종 고정 on/off |
| 루틴 | 요일별 또는 주 N회 · 삭제 = soft delete |
| 미정(TBD) | 일반 토큰 한도·임계 수치(app_config) · 광고 SDK · 낮/밤 시각(Firebase) |
