# API 연동 기준

정확한 필드·enum·필수 여부·오류 응답은 [OpenAPI 원본](../openapi/openapi.yaml)과
[분할 경로](../openapi/paths/), [요청·응답 모델](../app/schemas/)이 원본이다.
이 문서는 클라이언트가 지켜야 할 공통 규칙과 도메인 경계만 설명하며 JSON 예제를 중복 관리하지 않는다.

## 서버와 인증

| 서버 | 소유 범위 |
|---|---|
| `moly-backend` | 대화·일기·감정·경제·루틴·상점·운세·구독·광고·배너·BGM |
| `moly-auth` | `/me`, 온보딩, 프로필·알림·기기 설정, 로그아웃·회원탈퇴 |

backend 운영/개발 API 주소는 각각 `https://voice.moly.asia`, `https://dev.moly.asia`다.
계정 API를 backend에 보내지 않는다. 인증은 Supabase access token의 Bearer 방식이며 JWKS로 검증한다.
public health·설치 귀속·서버 간 웹훅은 예외이고, 웹훅의 서명/공유 비밀 검증을 사용자 JWT로 대체하지 않는다.
Swagger는 local/development 환경에서만 열리며, 개발 전용 라우트에는 명시적인 활성화 플래그도 필요하다. 둘 다 운영 공개 계약이 아니다.

## 공통 규칙

### 날짜와 언어

| 의미 | 경계·입력 |
|---|---|
| 대화 한도·일기 귀속 | 프로필 IANA 시간대의 04:00 `activity_date` |
| 출석·루틴·광고 보상·운세 | 현지 00:00 달력 날짜 |
| 감정 기록 | `/moods/{date}`로 보낸 날짜를 변환 없이 보존 |
| 대화 내부 오늘 감정 | 서버가 프로필 시간대의 현재 달력 날짜를 계산 |

00:00~03:59에는 한도 날짜와 보상 날짜가 다르다. `/charging-station.activity_date`는 호환 필드명이며
보상 날짜를 뜻한다. 기기의 임의 계산으로 보상·토큰을 리셋하지 않는다.

생성 콘텐츠는 정규화된 프로필 언어 `ko/en/ja`를 따른다. 미지원 프로필 언어는 `en` 폴백이다.
운세의 `X-App-Locale` 등 별도 입력은 해당 API 허용 범위를 따른다. 허용하지 않는 locale을 임의 번역해
응답한다고 가정하지 않는다. 과거 생성·저장된 콘텐츠는 언어 변경만으로 다시 쓰지 않는다.

### 권한·멱등·오류

- 권한·한도·가격·잔액은 서버 응답을 사용한다. `plan`과 `entitlement_source`를 함께 해석한다.
  체험 종류별 광고·콘텐츠 권한은 [구독](SUBSCRIPTIONS.md)을 따른다.
- 채팅 전송과 주제 준비는 안정된 `Idempotency-Key`를 사용한다. 같은 키를 다른 본문에 재사용하지 않고
  네트워크 오류 시 같은 논리 요청에 새 키를 마구 만들지 않는다. 서버 예약 prefix를 위장하지 않는다.
- 페이지 커서는 opaque 값이다. 대화는 older/newer 방향과 anchor 계약, 다른 목록은 각 경로의
  `next_cursor` 계약을 따른다. 메시지 ID는 숫자 모양의 문자열이므로 임의 정수 변환에 의존하지 않는다.
- 오류 봉투는 `{ "error": { "code": "...", "message": "...", "details": {} } }`다.
  HTTP 상태와 `code`로 분기하며 메시지 문장을 파싱하지 않는다. 재시도 가능 여부는 해당 오류 계약을 따른다.
- 모델 미완성/일시 실패의 `503 AI_UNAVAILABLE`, 겹친 턴의 오류, 한도 초과와 결제 실패를 구분한다.
  결과를 못 받았다고 광고·과금·보상을 클라이언트에서 확정하지 않는다.

## 엔드포인트 지도

아래는 연동 위치를 찾는 지도다. 세부 요청·응답은 연결한 원본을 사용한다.

| 영역 | 경로·동작 | 원본 |
|---|---|---|
| 대화 | `GET /chat/state`, `GET·POST /chat/messages`, `GET /chat/greeting`, `POST /chat/topic-entries` | [conversations](../openapi/paths/conversations.yaml) |
| 일기 | `GET /diaries`, `GET /diaries/{diary_id}`, `POST /diaries/{diary_id}/read` | [conversations](../openapi/paths/conversations.yaml) |
| 감정 | `GET /moods`, `PUT·DELETE /moods/{date}` | [mood](../openapi/paths/mood.yaml) |
| 재화 | `GET /wallet`, `GET /wallet/transactions`, `GET /charging-station`, `POST /charging-station/attendance`, `POST /charging-station/routine-reward`, `POST /charging-station/mood-reward`; 출석 체크·연속 `POST /attendance/check-in`, `GET /attendance` | [economy](../openapi/paths/economy.yaml) |
| 루틴 | 목록·생성 `/routines`; 수정·삭제 `/{routine_id}`; 체크·해제 `/complete`; 스킵·해제 `/skip`; 통계 `/statistics`; 날짜별 `/history`, 기간 `/stats`; 템플릿 조회·선택 `/routine-templates` | [routines](../openapi/paths/routines.yaml) |
| 상점·장착 | `/shop/products`, `/shop/purchases`, `/inventory`, `/inventory/equipment`; 지원되는 `/v2/` 조회·장착 경로 | [shop](../openapi/paths/shop.yaml) |
| 구독 | `GET /subscription`, `GET /subscription/plans`, `POST /webhooks/revenuecat` | [subscriptions](../openapi/paths/subscriptions.yaml) |
| 광고 | `POST /reward-ad-sessions`, `GET /webhooks/ad-ssv` | [economy](../openapi/paths/economy.yaml) |
| 운세 | 프로필 `/fortune-profile`; `/daily-fortune/status`, `/reveal`, `/ad-sessions` | [fortune](../openapi/paths/fortune.yaml) |
| 오늘의 글귀 | `GET /daily-affirmation` | [affirmation](../openapi/paths/affirmation.yaml) |
| 배너·BGM | `GET /banners`, `POST /banners/resolve`, `GET /bgm/tracks` | [banners](../openapi/paths/banners.yaml) · [bgm](../openapi/paths/bgm.yaml) |
| 리뷰·문의 | `POST /review/prompted`, `POST /feedback` | [core](../openapi/paths/core.yaml), [feedback](../openapi/paths/feedback.yaml) |
| 공개 경로 | `GET /health`, `POST /attribution/meta-referrer/decrypt` | [core](../openapi/paths/core.yaml) · [attribution](../openapi/paths/attribution.yaml) |

## 대화와 참조

`POST /chat/messages`는 `text`와 선택적 `greeting_id`, `context_ref`, `topic_entry_id`, `locale`을 받는다.
주제 준비 ID를 보낼 때는 표시 locale이 필요하며 선발화/운세 참조와 함께 보내지 않는다.
선발화 조회는 이력 확정이 아니다. 전송이 해당 인사를 함께 확정했는지는 응답의 `greeting`을 따른다.

응답의 `tokens_used/remaining`은 서버의 가중 한도 사용량이다. 마지막 성공 턴이 한도를 넘어 완료될 수
있으며 다음 요청을 차단한다. 실제 USD 비용·캐시 사용량을 클라이언트가 역산하지 않는다.
`personal_diary_eligible`는 실제 사용자 발화 문자수와 권한 조건의 판정이며 토큰만으로 일기를 보장하지 않는다.

일기 카드 확장을 쓰려면 `X-Moly-Capabilities: diary-reference-v1`을 보낸다. 기본 이력/멱등 응답과의
호환을 유지하며 `unavailable` 참조는 본문을 표시하지 않는다. 카드를 펼쳤을 때 별도의 읽음 API를 호출한다.
원문 줄바꿈을 보존하되 이것을 여러 메시지/말풍선이 발행됐다는 뜻으로 해석하지 않는다.

대화에는 오늘 앱의 감정 선택명 5종만 제한된 내부 정보로 들어간다. 일반 기분 질문을 앱 기록 조회로
바꾸지 않고, 명시적으로 오늘 선택명을 물을 때만 답한다. 메모·과거 감정·기록 유무를 추정하지 않는다.

## 일기·감정·루틴

- 캐피 일기와 사용자 감정 메모는 별개다. 일기 목록·읽음·참조는 소유권과 공개 상태를 따른다.
  일기 발행에는 등급·대화 문자수·발행 정책이 함께 적용되며 매일 생성/공개를 보장하지 않는다.
- 감정 저장은 `(user,date)` upsert다. 감정 API가 받은 메모를 대화 컨텍스트에 자동으로 넣지 않는다.
- 루틴의 생성·요일 변경·soft delete·완료·스킵·주간 통계는 서버 날짜와 이력을 사용한다.
  완료/스킵은 같은 날짜 상태이며 과거 수령 보상을 체크 해제로 되돌리지 않는다.
- 템플릿 선택은 별도 API로 기록한다. 계정 생성 시 임의 기본 루틴을 추가하는 것으로 대체하지 않는다.

## 결제·광고·상품·운세

- 구독과 실결제는 RevenueCat 검증 이벤트를 기준으로 반영한다. 웹훅 재전송은 중복 지급하지 않는다.
  Store 상품·오퍼·체험 자격을 고정 가격/기간 예제로 대신하지 않는다.
- 건초와 운세 광고는 서로 다른 세션이다. 서버 발급 세션의 사용자·보상·유효기간·거래 ID를 검증한 SSV만
  반영한다. 앱 광고 완료만으로 지급하거나 상세 해금을 표시하지 않는다.
- 상점 v1/v2의 슬롯·에셋·구독 전용 조건을 혼용하지 않는다. `price_hay=null`은 무료가 아니라 비매품이다.
- 운세는 저장된 당일 snapshot을 기준으로 한다. 잠긴 응답의 `overall.flow`는 공개되고 `categories`만
  제외된다. 해볼 것·조심할 것의 화면 블러는 앱 표시 정책이며 API 권한 상태와 구분한다.
  광고/구독 해금과 문구 배포·프로필 변경의 보존 계약은 [운세](DAILY-FORTUNE.md)를 따른다.

## 계약 변경

OpenAPI 분할 원본과 Pydantic 응답을 함께 갱신하고 bundle을 재생성한다. SDK 생성과 실제 앱 렌더링은
별도 검증이다. 추가 필드라고 자동 호환을 단정하지 않으며, 멱등 캐시·구버전 저장본·양 버전 reader/writer와
탈퇴/비공개 상태도 검증한다. 배너·주제 피드는 사용자별 응답이며 공용 캐시로 재사용하지 않는다.
