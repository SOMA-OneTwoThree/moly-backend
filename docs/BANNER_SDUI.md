# 홈 배너와 주제 대화

배너 시각 정의·문구·노출 조건은 서버 파일, 이미지는 Storage, 렌더링과 등록 화면 이동은 앱이 소유한다. 임의 코드·URL 실행이나 구매·보상을 배너 명령으로 수행하지 않는다. 서버 원본은 [스키마](../app/schemas/banners.py), [컴파일러](../app/services/banner_catalog.py), [HTTP 계약](../openapi/components/banners.yaml), [주제 HTTP 계약](../openapi/components/topics.yaml)이다.

## 정의와 응답

| 항목 | 원본·계약 |
|---|---|
| 시각 manifest | `app/resources/banners/home_blind.json` |
| 질문 catalog | `app/resources/conversation_topics/catalog.json` |
| placement/schema | `home_blind`, wire schema 1 |
| component/layout | `banner_canvas_v1`, `home_blind_v1` |
| 크기 | 고정 287.7×158.457. 서버가 외곽 크기·스크롤 구조를 바꾸지 않음 |
| 상한 | manifest 256 KiB, 응답 128 KiB·최대 5장 |
| revision | manifest **원본 UTF-8 bytes**의 SHA256. 순서 있는 버전 번호가 아님 |

revision이 같아도 사용자·언어·시간·DB binding에 따라 응답이 달라진다. 구버전 hash를 무조건 거절하면 롤백을 막는다. manifest는 배포 이미지에 포함되므로 변경 후 배포가 필요하며 DB만 바꿔 질문/시각 정의가 자동 갱신되지는 않는다.

### HTTP

- `POST /banners/resolve`: 현재 제안·주제 상태를 준비하는 경로다. 단순 읽기 감사로 호출하지 않는다.
- `GET /banners`: 읽기 전용 구버전 호환 경로이며 새 주제 카드를 포함하지 않는다.
- 두 경로 모두 Bearer 인증, `Cache-Control: private, no-store`. 개인 응답을 CDN·ETag·디스크 공용 캐시에 저장하지 않는다.
- 문맥: placement, schema_version, platform(ios/android), app_version(1~64자), capabilities(최대 32개). capability 이름은 소문자로 시작하는 영숫자/밑줄, 최대 64자다. 중복은 집합으로 해석한다.
- `X-App-Locale` 최대 64자, ko/ja 지역 변형은 해당 언어로 정규화하고 미지원은 en이다. 한 카드 전체 canvas로 fallback하며 필드별 언어를 섞지 않는다.
- `X-App-Timezone`은 IANA 이름이다. 없을 때 프로필로 fallback하며 잘못된 명시 값은 422 `APP_TIMEZONE_INVALID`다. 날짜 binding은 사용자 현지 **00시 달력일**이다.
- 미지원 placement/schema는 422 `BANNER_PLACEMENT_UNSUPPORTED`/`BANNER_SCHEMA_UNSUPPORTED`, 정의·문맥 조회 실패는 503 `BANNERS_UNAVAILABLE`다. 정상 비노출은 `items: []`다. 오류를 정상 빈 결과로 바꾸지 않는다.
- card 단위 검증·이미지 실패는 그 카드를 제외하며 가능한 다른 카드는 유지한다. 응답 크기·전체 정의 오류와 구분한다.

### 노출·binding

enabled → UTC start 포함/end 제외 → platform/app version → locale → capability → binding/조건 → 순서·5장 상한으로 결정한다. 지원하지 않는 action/component를 가진 카드는 앱에 내려주지 않는다.

| binding | 의미 |
|---|---|
| `user.local_date` | 현지 날짜. month_day/full_date 형식 |
| `routines.remaining_today` | 현지 오늘 남은 루틴. 0이면 해당 조건의 카드를 숨김 |
| `topic.question` | offer에 고정된 질문. 단독 alias로 사용 |
| `music.daily_title` | 같은 날짜·활성 곡 집합에서 안정적인 곡 제목 |

DB I/O는 서비스에서 하고 manifest 검증/렌더링은 순수 함수로 유지한다. 조회 실패를 가짜 0이나 샘플 제목으로 감추지 않는다. 음악은 `remote_bgm_v1` 지원 앱과 해당 binding이 있을 때만 DB 목록을 쓰며, 음악 목록 실패로 다른 카드를 지우지 않는다. [BGM 계약](REMOTE_BGM.md)을 따른다.

`served_at/valid_until`은 서버 시각 기준이다. 캠페인 종료와 binding의 다음 현지 날짜 중 빠른 경계로 만료한다. DST에서 `지금+24시간`으로 다음 날짜를 계산하지 않는다.

## 시각 계약

새로운 디자인은 지원 primitive 안에서만 작성한다. 새로운 primitive/action/폰트는 앱 지원과 capability 계약을 먼저 추가한다.

| 구분 | 허용 범위 |
|---|---|
| elements | `text_v1`, `button_v1`, `image_v1`, `shape_v1`, `action_region_v1` |
| 배경 | `solid_v1`, 두 색 `linear_gradient_v1`, `image_background_v1` |
| frame | 유한한 0~1 정규 좌표, 양수 크기, canvas 안에 완전히 포함 |
| 개수 | element 최대 12, action 최대 2, 이미지 최대 2(배경 포함) |
| 텍스트 | 1~120 codepoint, 버튼 1~20·줄바꿈 금지 |
| 폰트 | body/display, 크기 12~28, weight 400/500/600/700 |
| 행 | max_lines 1~3(버튼 1), line_height 1~2 |
| 모양 | radius 0~24, border 0~3, 색 `#RRGGBB` |
| 접근성 | semantics_order 0~11 중복 금지, 읽기 순서와 action label 명시 |

- 한국어/영어는 Pretendard, 일본어는 PretendardJP 대응을 유지한다. 동적 글자 크기에서도 overflow면 카드 제외를 우선하며 자동 축소·임의 말줄임·클리핑으로 승인 문구를 바꾸지 않는다.
- touch target은 최소 48×48이며 겹치거나 잘리지 않아야 한다. 단순 장식은 중복 낭독하지 않는다.
- image는 contain/cover와 0~1 alignment를 사용하고 비율을 보존한다. image background overlay는 opacity 0이어도 `image_background_overlay_v1` capability가 필요하다.
- action region의 `content_ids`는 1~10개 고유 text/image/shape를 가리키고 text 또는 image를 하나 이상 포함한다. 모든 대상이 region 안에 있어야 하며 여러 action이 같은 element를 공유하지 않는다. 하나의 접근성 버튼으로 읽는다.

### 등록 action

아래 여덟 action은 인수가 없다. raw route/함수/스크립트를 전달하지 않는다.

`open_shop`, `open_routines`, `open_conversation`, `open_fortune`, `open_diary`, `open_mood`, `open_timer`, `open_music`

- `open_music`은 선택 화면만 열며 자동 재생하지 않는다.
- `open_conversation`은 일반 대화다. 미응답 주제 질문을 자동 복원하지 않는다.
- 아홉 번째 `open_topic_conversation_v1`만 서버가 채운 `topic_ref`를 갖는다. offer_id(UUID), offer_sequence(양의 정수), topic_id, topic_revision(SHA256), locale(ko/en/ja)이며 같은 카드의 질문 snapshot과 일치해야 한다.
- 각 action capability를 지원하는 앱에만 카드를 제공한다. 새 action을 받은 구버전이 임의 route를 추측하지 않는다.

### 이미지 보안

이미지에는 공개 URL·SHA256·byte_length·pixel_width/height·media_type이 필수다. 허용 환경 origin의 Supabase Storage 공개 object 경로만 사용하며 query/fragment·인증 URL·redirect·상대 경로를 허용하지 않는다. 운영에 개발 origin을 섞지 않는다.

- PNG/JPEG/WebP 정적 이미지, 최대 512 KiB, 한 변 2,048px, 총 1,048,576pixel이다. SVG/애니메이션은 지원하지 않는다.
- immutable hash/버전 URL을 쓰고 같은 URL bytes를 덮지 않는다. Bearer/cookie를 이미지 서버로 전달하지 않는다.
- 서버는 사용자 요청마다 이미지 bytes를 프록시하지 않는다. 배포 전 validator가 원격 bytes를 검사하고 앱도 size/hash 확인 뒤 노출·클릭을 허용한다. 실패하면 카드 전체를 제외한다.
- 새 파일 게시 → 세 언어 시각 검수 → manifest 연결 → 검증·배포 순서다. 이전 배포/앱 snapshot을 위해 이전 파일을 즉시 삭제하지 않는다.

## 앱 유지 계약

이 절은 앱과 합의할 계약이며 서버 단위시험만으로 출시 앱 구현 완료를 인증하지 않는다.

- 홈 첫 진입/foreground·관련 화면 종료·언어/시간대 변경 때 refresh한다. swipe/rebuild마다 호출하거나 대화가 열린 상태에서 offer를 바꾸지 않는다.
- 문맥이 바뀌면 이전 요청 세대를 무효화한다. 늦게 온 다른 세대 응답을 적용하지 않는다. 루틴 변경 요청 중에는 이전 남은 수 카드를 숨긴다.
- 만료 시 카드/CTA를 숨기거나 막는다. `requestStartMonotonic + (valid_until-served_at)`과 wall-clock guard로 판단한다. background 복귀 후 검증 전 stale action을 실행하지 않는다.
- 응답 실패 때 무기한 오래된 사용자별 카드를 재사용하지 않는다. 정상 유효 카드와 빈 결과·오류를 구분한다.
- 새 resolve 미지원 fallback은 404/405/명시적 schema 미지원에 한정한다. 계정/업무 4xx를 GET 재시도로 우회하지 않는다.

## 주제 제안의 상태

질문은 운영자가 승인한 catalog의 원문이다. LLM으로 즉석 생성하거나 클라이언트가 보낸 text를 신뢰하지 않는다.

- topic_id는 질문의 의미, topic_revision은 문구 버전, offer_id는 사용자에게 제안한 한 번, entry_id는 대화 진입 준비 한 번을 식별한다. 같은 질문이 다시 와도 새 offer일 수 있다.
- `POST /banners/resolve`는 현재 offer를 유지한다. 한 달력일에 서로 다른 질문을 **최대 두 개 준비**한다. 첫 A 준비 때 다음 B로 pointer를 옮기고, 두 번째 B 준비 뒤에는 그날 B를 유지한다. 미응답·재시도·다시 열기·답변 자체로 추가 소진하지 않는다.
- 다음 현지 날짜는 현재 위치에서 한 번만 전진하고 일일 준비 수를 초기화한다. 여러 날 공백도 한 번이며 시간대 역행으로 날짜/pointer를 되돌리지 않는다. sequence는 순환한다.
- `user_topic_states`는 사용자+placement의 offer snapshot·문구·revision·sequence·날짜 high-watermark를 보존한다. `chat_topic_entries`는 pending/committed/superseded다. 사용자당 pending 하나, offer당 첫 답변 하나를 유지한다.
- 준비 수명은 `max(다음 현지 자정, 준비+30분)`이다. locale로 다시 열어도 연장하지 않는다. 자정을 지나도 수명 안의 A에 답할 수 있지만 다른 진입/일반 대화 성공으로 supersede될 수 있다. A 답변이 B를 완료하지 않는다.

### 준비와 첫 답변

1. `POST /chat/topic-entries`에 banner 문맥·banner_id·topic_ref와 별도 `Idempotency-Key`를 보낸다. 질문 text는 보내지 않는다. `topic-prepare` namespace는 대화 멱등키와 분리하며 30일 보존한다.
2. 준비 응답의 질문은 `topic_opening`으로 보여준다. 이 단계는 모델 호출·사용량 차감이 없다.
3. 첫 `POST /chat/messages`에 사용자 text·`topic_entry_id`·locale을 보낸다. 준비 응답의 `entry_id`를 `topic_entry_id`에 넣는다. 기존 대화 모델 경로가 답하고 질문/사용자 답/캐피 답·사용량을 하나의 발행 트랜잭션으로 처리한다. 질문 원문은 사용자 사실로 기억 추출하지 않는다.
4. 첫 답변의 `topic_entry_id`는 `greeting_id`/`context_ref`와 함께 사용하지 않는다. 위기 경로가 우선하며 부적절한 선택 참조 때문에 안전 응답을 막지 않는다.
5. pending 재조회는 질문, committed는 message_id로 이력에 합쳐 중복 렌더링을 막는다. superseded는 미응답 진입 종료다. 사용자 입력은 오류 때 보존한다.

네트워크/모델 호출 중 DB 연결·락을 오래 잡지 않는다. 사용자 advisory lock·lease·context revision·privacy fence와 발행 때 소유권/소비 여부를 다시 확인한다. 이미 정상 수락한 생성이 끝나는 동안 deadline이 지났다는 이유만으로 결과를 버리지 않는다.

| 오류/상태 | 처리 |
|---|---|
| `TOPIC_OFFER_UNAVAILABLE` 409 | offer 재선택, 입력 보존 |
| `TOPIC_ENTRY_UNAVAILABLE` 409/410 | 만료/소비/상태를 재조회하고 진입 복구 |
| 5xx·응답 유실 | 같은 key·body·entry·locale로 복구. entry 없는 일반 대화 자동 재전송 금지 |
| `CHAT_TURN_*` | 기존 대화 멱등/락/복구 계약 유지 |

새 catalog를 반영해도 기존 entry의 고정 질문·첫 답변을 바꾸지 않는다. 사용자가 다른 이야기를 하면 승인 질문으로 억지 복귀시키지 않는다.

## 문구·catalog 유지 절차

1. 세 언어 canvas와 질문을 원어로 검수한다. 자동 번역을 승인으로 대신하지 않는다. 정의 파일의 템플릿과 실제 compile 결과를 함께 확인한다.
2. 기존 의미를 바꾼 질문은 새 topic_id, 같은 의미의 문구 수정은 새 revision으로 추가한다. 이미 게시된 ID/버전·철회 이력은 지우지 않는다. 질문이 철회/누락되면 대체가 없을 때 카드를 숨기고 기존 상태를 파괴하지 않는다.
3. 기본 sequence는 append-only다. 순서 변경은 별도 검토와 `--allow-topic-reorder`가 필요하며 ID 삭제를 허용하는 옵션이 아니다. 인기 지표로 자동 질문을 바꾸지 않는다.
4. 이전 배포 catalog와 아래 검사를 수행한다. `--assets`는 원격 HTTP를 읽으며 모델/DB 쓰기를 하지 않는다. 서버 계약 시험과 실기기 글자 크기·접근성·CTA·느린 네트워크·구버전 검증은 별개다.

```bash
uv run python scripts/validate_banners.py
uv run python scripts/validate_banners.py --previous-topics '<이전 배포 catalog.json>'
uv run python scripts/validate_banners.py --assets --environment prod
uv run python -m scripts.openapi_contract --check
```

5. 최종 이미지 manifest/topic revision과 보호된 `/health/banners` 값을 확인한다. 승인된 컨테이너 실행 경로에서는 `scripts/check_running_banners.py`로 대조한다. 토큰·사용자별 응답을 로그에 노출하지 않는다.
6. DB 계약이 바뀌면 [DB 절차](../db/README.md)를 따른다. 일반 문구 교체에 과거 topic 전환 SQL을 재실행하지 않는다. 롤백은 호환 이미지와 catalog로 하고 기존 offer/entry/snapshot 이력은 남긴다.
