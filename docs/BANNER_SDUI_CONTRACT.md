# 홈 배너 SDUI 공동 규약

적용: `schema_version=1`, `banner_canvas_v1`, `home_blind_v1`. 현재 지원 동작과 호환성 경계를 정의한다.

공동 규약의 원본은 moly-backend의 이 파일이며 becappy-mobile의 동명 문서는 동일 사본이다. 작성·배포 방법과 레포 내부 연결은 각 레포의 [BANNER_SDUI.md](BANNER_SDUI.md)를 따른다.

## 1. 범위와 고정 경계

**배너 크기의 진실은 becappy-mobile의 현재 목업 보라색 카드다. 배경과 내부 배치만 서버에서 바꾼다.**

| 구분 | 계약 |
|---|---|
| 고정 카드 | `RoomTheme.theme1Blind.bannerRect = Rect.fromLTWH(52, 120.5, 287.7, 158.457)` 기준. 선언은 `becappy-mobile/lib/ui/core/room/room_theme.dart` |
| 크기의 의미 | 기준 크기 **287.7 × 158.457**과 비율 고정. 실제 기기에서는 현재 홈의 무대 배율을 그대로 적용 |
| 서버 소유 | 고정 카드 안의 배경·색·테두리·텍스트/버튼 등 지원 요소의 위치·크기·내용·순서·action |
| 앱 소유 | 카드 바깥 위치·크기·홈 배율·블라인드/줄·접힘 모션·페이지 넘김과 카드 사이 여백·인디케이터·렌더러·화면 이동 |
| 금지 | 서버가 카드 자체의 width/height/aspect_ratio/scale을 지정하거나 콘텐츠에 맞춰 카드를 늘리는 동작 |
| 운영 | Git의 배너 파일을 서버 이미지에 포함. dev 배포·개발 TestFlight 검수, 운영 승격은 별도. 공통 정의에 사용자별 날짜·루틴 수를 채움 |
| 이미지 | 첫 버전부터 서버가 지정한 원격 배경/내부 이미지 지원. 주소·배치·크기·맞춤 방식을 정의 파일에 명시 |
| 날짜 일치 | 배너와 루틴 조회/완료/취소/통계가 같은 요청 시간대와 서버 날짜를 사용 |
| 별도 범위 | 관리자 화면, 사용자 세그먼트 선정, 외부 URL action, 운세 화면 실제 API 연동 |

페이지 사이 여백은 클라 `AppBlindBannerTokens.cardGap`의 기준 12에 홈 배율을 적용한다. 정착한 카드 크기·위치는 그대로이고 서버 요소 좌표에는 여백을 포함하지 않는다.

배경은 고정 카드 영역을 채우며 내부 요소는 그 경계 안에서만 배치한다. 긴 문구 때문에 외곽 크기를 바꾸지 않는다.
새 캠페인·문구·배경·배치는 지원 요소 안에서 서버 배포로 변경한다. 앱 업데이트는 새 요소/동작 구현을 추가할 때 필요하다.
새 이미지는 기존 Storage 버킷에 준비하고 JSON에서 URL/메타데이터를 참조한다. 이미지 bytes를 Git에 넣는 운영은 사용하지 않는다.

## 2. 고정 canvas와 요소

| registry | v1 식별자 |
|---|---|
| 컴포넌트 | `banner_canvas_v1` |
| 배치 규칙 | `home_blind_v1`: 고정 카드 크기·폰트 매핑·배율·터치 제약 |
| 요소 | `text_v1`, `button_v1`, `image_v1`, `shape_v1`, `action_region_v1` |
| 배경 | `solid_v1`, `linear_gradient_v1`, `image_background_v1` |
| action | `open_fortune`, `open_shop`, `open_conversation`, `open_routines`, `open_topic_conversation_v1` |

- 내부 `frame={x,y,width,height}`는 **카드 전체 기준** 0..1 좌표다. x/y≥0, width/height>0, x+width/y+height≤1, 모두 유한수다.
  이 frame은 내부 요소에만 존재한다. canvas 최상위에는 크기/위치 필드를 두지 않는다.
- font_size/radius/border/padding은 기준 카드의 design unit이다. renderer는 기준 공간에서 그리고 기존 host가 한 번 확대한다.
  TextScaler도 현재 앱 상한1.24 내에서 한 번 적용한다. 이중 배율·추가 자동 글자 축소는 금지한다.
- 한 카드 최대12요소/동작2개(button_v1과 action_region_v1 합계). 배열 순서는 뒤→앞 그리기, 읽을 요소의 고유 semantics_order(0..11)는 읽기 순서다.
- 중첩·상대 참조·자동 높이·스크롤·원격 애니메이션·실행 코드는 지원하지 않는다.
- 글자 overflow, 필수 요소 잘림, 의미 있는 요소끼리 겹침은 카드 제외 사유다. 장식 이미지/배경 위 텍스트는 가독성을 유지할 때 허용한다.
  임의 ellipsis/축소로 통과시키지 않는다. 장식 이미지는 필수 정보나 버튼을 가리거나 터치를 가로채지 않는다.
- 버튼 hit rect는 중심 기준 최소48×48 logical px다. 카드/화면/clip 밖이나 다른 버튼·줄 조작 영역과 충돌하면 제외한다.
  검사는 완전히 펼친 카드의 실제 무대 폭×높이·배율에서 수행한다. 모션 중 일시 clip은 배치 실패가 아니다.

| 스타일/필드 | 규칙 |
|---|---|
| 색 | 불투명 sRGB `#RRGGBB` |
| font | body/display. 둘 다 item.locale ko/en→Pretendard, ja→PretendardJP. 원격 폰트 금지 |
| font_size / weight | 12..28 / 400·500·600·700 |
| align / vertical_align | start·center·end / top·center·bottom. LTR, letter spacing=0 |
| line_height / max_lines | 필수1.0..2.0 / text1..3, button1 |
| radius / border.width | 0..24 / 0..3. radius는 해당 box 짧은 변의 절반 이하 |
| button padding | horizontal0..24, vertical0..12, 모두 필수 |
| text 길이 | 계산 후 text120 code point, button20. 빈 문자열·버튼 줄바꿈 금지 |
| solid_v1 | 필수 type, color |
| linear_gradient_v1 | 필수 type, colors(정확히 두 색), direction. stops=[0,1] |

gradient direction은 horizontal(left→right), vertical(top→bottom), diagonal_down(topLeft→bottomRight),
diagonal_up(bottomLeft→topRight)다. 축 방향은 양 끝 중앙을 기준으로 한다.
일반 텍스트의 [대비 기준](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)은4.5:1 이상이다.
실제 글자 크기·보조 기술의 읽기/focus도 검수한다. 터치 크기·대비 검사만으로 접근성 전체를 보증하지 않는다.

### 원격 이미지

| 필드/대상 | 규칙 |
|---|---|
| source | url, sha256, byte_length, pixel_width, pixel_height, media_type 모두 필수 |
| url | 최대2048자 HTTPS 공개 불변 주소. 앱/검증기의 배너 asset origin 허용 목록 안에서만 사용 |
| 파일 | 정지 PNG/JPEG/WebP. MIME/실제 형식/sha256(소문자64자리)/bytes/원본 해상도가 선언과 일치해야 함 |
| 상한 | 파일당512KiB, 한 변2048px 및 총1,048,576픽셀 이하. 카드당 배경 포함2개, 내부 이미지는12요소 상한에도 포함 |
| image_background_v1 | type, source, fit=cover, alignment={x,y}(각0..1), base_color 필수. 고정 canvas 전체를 채우고 radius로 clip. 선택적 overlay는 아래 규칙 적용 |
| image_v1 | id, type, frame, source, fit(contain/cover), alignment, accessibility_label, semantics_order 필수. frame 안에 clip |
| alignment | 0은 왼쪽/위, 0.5는 중앙, 1은 오른쪽/아래. contain은 여백 정렬, cover는 잘릴 영역의 정렬 |
| 접근성 | 장식은 accessibility_label/semantics_order 모두 null. 의미 있는 이미지는 해당 locale의 설명1..120자와 고유 읽기 순서 필수 |

이미지는 비율을 유지한다. 투명 배경의 base_color는 최하단 색이며 로딩 실패를 덮는 대체 디자인으로 사용하지 않는다.
배경 `overlay`는 생략/null이면 효과 없음, 객체이면 `{color: "#RRGGBB", opacity: 0..1}`이다. 색·불투명도는 필수이고 불투명도는 유한수다. 이미지 위·모든 내부 요소 아래에 단색으로 합성하며 글자·버튼의 불투명도, 터치·읽기·디코딩 수명에 영향을 주지 않는다. 이미지 실패를 덮개로 대체하지 않는다. 객체가 있으면 opacity=0이어도 `image_background_overlay_v1` capability를 요구하며 미지원 앱에는 해당 카드를 제외한다. 이미지 수/요소 수를 추가로 사용하지 않는다. 덮개 색·농도 변경은 기존 이미지 bytes/디코딩을 재사용한다.
필수 안내 문구는 text_v1로 제공한다. 이미지 버튼은 image_v1과 action_region_v1으로 구성하고 클릭 영역의 접근성 이름을 제공한다. 이미지 위 문구의 대비는 실제 crop별로 검수한다.
GIF/APNG/움직이는 WebP·SVG·data/file URL·임시 서명 URL·redirect는 v1에 허용하지 않는다. URL에 인증정보/사용자 식별자를 넣지 않는다.
이미지 주소로 앱 Bearer 토큰/cookie를 보내지 않는다. 원본 URL을 서버 사용자 요청마다 다운로드하거나 proxy하지 않는다.
같은 URL을 덮어쓰지 않는다. 새 이미지에는 새 URL/sha256을 사용한다.
이미지 bucket 이름은 dev/prod 모두 `banner-assets`다. dev 프로젝트는 `wywzjslvxwttxkecbyis`,
prod 프로젝트는 `qkgjlgzsharnilxnkytd`다. dev에서 검수한 파일을 prod bucket의 같은 경로에 같은 bytes로 업로드하고,
manifest의 URL만 prod 주소로 교체한다. hash·크기·형식은 유지하고 운영 배포 전 공개 다운로드로 재검증한다.
개발 origin은 dev flavor(로컬 flavor 생략 포함)에서만 허용한다. prod 앱은 운영 origin만 허용하고,
prod 배포 검증기는 개발 origin 참조를 거부한다. 기존 운영 `shop-assets` URL은 호환성을 위해 계속 허용한다.
이는 기존 Storage의 [CDN 갱신 지연을 피하는 업로드 지침](https://supabase.com/docs/guides/storage/uploads/standard-uploads#overwriting-files)과도 일치한다.
각 카드의 이미지 검증/디코딩까지 끝나야 카드와 CTA를 표시한다. 실패/시간 초과는 해당 카드 전체 제외, 정상 카드는 유지한다.
갱신 중에는 기존 유효 카드만 유지할 수 있다. 늦은 이미지 완료에도 context/generation/만료를 재검사하고 제외한 카드를 되살리지 않는다.
사용자별 feed의 no-store와 달리 공통 이미지 bytes는 (허용 origin, sha256) 기준으로 캐시할 수 있다. 캐시도 bytes/hash를 검증한다.

### 서버 시각 구성과 클릭 영역

서버가 이미지·문구·도형의 디자인과 배치를 결정하고, 클라는 이를 그린 뒤 등록된 action을 기존 앱 화면에 연결한다. 화면별 버튼 디자인은 필요하지 않다. 기존 button_v1은 호환용으로 유지한다.

| 요소 | 필수 필드와 의미 |
|---|---|
| shape_v1 | id, frame, background(solid_v1 또는 linear_gradient_v1), radius(0..24), border, semantics_order=null. 장식 도형이며 자체 터치 없음 |
| action_region_v1 | id, frame, content_ids(고유 시각 요소 ID 1..10개), accessibility_label(1..120자), semantics_order(0..11), action. 자체 시각 스타일 없음 |

- content_ids는 같은 canvas의 text_v1/image_v1/shape_v1만 참조한다. 최소 한 개의 이미지 또는 텍스트를 포함하고 한 요소를 여러 동작에 연결하지 않는다. 참조 요소의 전체 frame은 클릭 영역 안에 있어야 한다.
- 이미지와 텍스트를 겹쳐 버튼처럼 구성할 수 있다. 배열 순서대로 뒤에서 앞으로 그리며 장식이 앞의 필수 내용을 덮는 배치는 거부한다. 클릭 영역 자체는 그리기 순서와 무관하게 터치를 처리한다.
- 클릭 영역도 최소48×48 터치·고정 카드/화면/줄 충돌 검사를 적용한다. 다른 동작과의 겹침 및 연결하지 않은 필수 내용 위의 클릭 영역은 거부한다.
- 연결한 시각 요소의 별도 읽기는 제외하고 accessibility_label을 지정한 순서의 버튼으로 한 번 읽는다. 이미지가 준비되지 않거나 카드가 만료되면 클릭도 제공하지 않는다.
- 도형 위 텍스트 대비는 해당 배경으로 검사한다. 이미지 위의 실제 대비는 dev TestFlight에서 검수한다.
- 새 type은 같은 이름의 capability를 요구한다. action_region_v1은 연결한 action capability도 요구한다. 해당 capability를 광고하지 않는 앱에는 새 표현을 보내지 않는다. 새 지원 앱 설치 후에는 이미지·문구·배치·동작 구성을 서버 파일만 바꿔 적용한다.

## 3. 조회 API와 응답

신규 앱은 `POST /banners/resolve`(`resolveBanners`)에 아래 client context를 JSON으로 전송한다. 지원하는 주제 카드가 있을 때 사용자별 현재 제안을 유지/전진시키므로 POST다. 기존 `GET /banners`(`listBanners`)는 같은 context를 query로 받고 신규 주제 카드를 제외하는 읽기 전용 호환 경로다. 둘 다 기존 Bearer 인증, `Cache-Control: private, no-store`.
운영용 쓰기는 이 API에 넣지 않는다. 사용자별 JSON에 공유/CDN/영속 disk cache·ETag를 사용하지 않는다.

| 입력 | 규칙 |
|---|---|
| placement | 필수 home_blind |
| schema_version | 필수 양의 정수, 지원값1 |
| platform | 필수 android/ios |
| app_version | 필수1..64자. major.minor.patch 비교, 시험용 suffix/metadata 분리. 해석 불가면 버전 제한 없는 카드만 허용 |
| capabilities | 필수 배열(GET은 반복 query), 최대32개, 각 값 `[a-z][a-z0-9_]{0,63}`, 중복 제거 |
| X-App-Locale | 최대64자 BCP47 앱 표시 언어. 미설정·미지원→en |
| X-App-Timezone | IANA 시간대1..64자. 지원 앱은 기기의 현재 식별자를 전송. 생략은 profiles.timezone, 잘못된 값은422 APP_TIMEZONE_INVALID |

필수 필드와 타입은 서버 `app/schemas/banners.py`, HTTP 입력/응답은 `openapi/paths/banners.yaml` 및 `openapi/components/banners.yaml`을 따른다. 완성 응답 예시는 양 레포의 `tests/fixtures/banners/composed_feed.json`(클라: `test/fixtures/banners/composed_feed.json`)을 참조한다. 서버 파일의 template 선언과 API 응답의 완성 문자열을 혼동하지 않는다.

data_dependencies는 카드의 런타임 의존성 중복 없는 목록(user.local_date / routines.remaining_today / topic.question, 정적 카드는 빈 배열)이다.
앱은 이 값으로 저장 중 루틴 의존 카드를 무효화한다. 서버가 binding에서 자동 도출하며 카드 ID나 문구로 추측하지 않는다.
응답의 valid_until은 nullable이며 필수 필드 여부는 스키마를 따른다. 필수 필드 누락을 Flutter 기본값으로 채우지 않는다.
카드/요소 id는 `[a-z0-9][a-z0-9_-]{0,63}`, 각각 목록/카드 안에서 고유하다.
revision은 **배포된 정의 파일의 원본 UTF-8 bytes SHA256**이며 `[a-f0-9]{64}`다. 코드 버전/배포 순번은 아니다.
같은 revision에도 사용자·언어·시각에 따라 결과가 달라지므로 새 응답을 적용한다. 순차 배포/복구로 이전 hash가 와도 적용한다.
비활성/조건 불일치는 로딩한 파일의 revision과 items=[]다. 누락/손상된 파일은503이며 정상 빈 목록이 아니다. 응답 상한은5카드/128KiB다.

## 4. 데이터·언어·노출·action

| 데이터 | 계산·유효 기한 |
|---|---|
| user.local_date | 검증한 X-App-Timezone(생략만 profiles.timezone) + 요청의 단일 서버 UTC clock. 현지 달력일, 다음 현지 자정까지 |
| routines.remaining_today | 본인·삭제되지 않음·오늘 ISO 요일 예정·현지 오늘 미완료. **0개면 의존 루틴 배너 숨김**. 다음 현지 자정까지 |
| music.daily_title (서버 작성 전용) | 앱 내장 6곡의 제목 중 현지 날짜 기준으로 고른 곡. 같은 날짜·언어 변경·서버 재시작에 유지. 응답은 완성 문자열이며 의존성은 기존 `user.local_date`, 유효 기한은 다음 현지 자정. `open_music`은 음악 선택 화면만 열며 곡을 자동 선택·재생하지 않음 |
| topic.question | 사용자별 현재 offer의 고정된 locale 질문. 다음 현지 자정까지; 첫 답변 성공 시에도 해당 offer 카드 무효화 |

서버가 binding/조건을 실행하고 완성 문자열만 응답한다. 앱은 날짜/count를 다시 계산하지 않는다.
동일 시간대 규칙을 기존 `/routines` 목록·완료·취소·통계에도 적용한다. 날짜를 사용한 정상 루틴 응답에는
`X-App-Local-Date: YYYY-MM-DD`, `X-App-Served-At`, `X-App-Day-Ends-At`(후자 둘은 UTC RFC3339)를 추가한다.
새 클라의 오늘 요일/표시는 이 서버 날짜를 사용하고, 자정 만료는 아래 배너와 같은 monotonic deadline 식을 사용한다. 기존 응답 body는 유지한다.
기존 앱의 헤더 생략은 저장 시간대 동작을 유지하며, 요청 시간대로 profile이나 과거 완료 기록을 덮어쓰지 않는다.
시간대/현지 날짜 변경 시 루틴과 배너 캐시를 함께 무효화하고 보이는 루틴 화면은 자정에 한 번 재조회한다.
배너의 자정 만료는 제거 정책을 유지한다. 상세 HTTP 원본은 서버 OpenAPI와 루틴 계약에 둔다.
같은 요청에서 데이터를 일괄 조회한다. binding 실패는 의존 카드 제외, 필요한 공통 context 실패는 전체 조회 실패다.
현지 자정은 IANA timezone의 다음 **달력일**을 UTC로 바꾼다. now+24h로 계산하지 않는다.
count는 조회 시점 값이며 타 기기의 즉시 변경을 보장하지 않는다. 날짜 표시를 위해 운세 결과 API를 호출하지 않는다.

언어별 완성 canvas를 보관한다. ko-KR→ko, ja-JP→ja, 미지원→en. 영어 canvas는 정의 파일에 필수다.
번역이 없으면 필드를 섞지 않고 영어 canvas 전체를 선택한다. 폰트도 그 item.locale에 따른다.
노출 순서는 enabled → `starts_at <= now < ends_at` → OS/버전 → locale → capability → binding 조건 → 저장 순서 최대5장이다.
일정은 nullable UTC, 버전 하한 포함/상한 미포함. null은 경계 없음. 사용한 capability는 서버가 자동 수집한다.

| action | 의미 |
|---|---|
| open_shop | 기존 상점 화면 |
| open_routines | 기존 루틴 화면 |
| open_diary | 캐피의 일기 목록 |
| open_mood | 기존 감정 기록 팝업. 달력·첫 진입 안내·작성/수정/삭제 흐름 유지. 자동 기록 없음 |
| open_timer | 기존 타이머 설정. 자동 시작 없음 |
| open_music | 기존 음악 선택. 자동 재생 없음 |
| open_conversation | 기존 대화 진입, chatEnabled 등 접근 제한 유지 |
| open_fortune | 기존 운세 화면으로 이동/복귀. 운세 실제 API 연동 완료를 뜻하지 않음 |
| open_topic_conversation_v1 | `topic_ref`로 클릭한 질문을 준비한 뒤 기존 대화에 연결 |

주제 이외의 여덟 action은 매개변수 없음. 각 action 이름의 capability가 있어야 해당 카드를 제공한다. 신규 주제 action만 `topic_ref`(offer_id UUID, offer_sequence 양의 정수, topic_id, topic_revision SHA256, locale ko/en/ja)를 갖는다. 파일에는 action type만 쓰고 참조는 서버가 응답 시 채운다. `topic.question` binding과 같은 snapshot이어야 하며 질문 text는 alias를 단독으로 사용한다. 지원 capability는 `open_topic_conversation_v1`이다. 준비·첫 답변 규약은 [주제 대화](BANNER_TOPICS_DESIGN.md)와 서버 `openapi/components/topics.yaml`을 따른다. raw 경로/함수명/스크립트를 실행하지 않는다. 구매·보상·unlock을 직접 수행하지 않는다.
새 의미/매개변수는 별도 action 계약이 필요하다. 버튼 없는 안내형 카드도 허용한다.

## 5. 갱신·실패·호환성

조회 계기: 첫 홈 진입, foreground, 대화/타이머 종료, 상점/루틴 popup 닫힘, 운세 화면 복귀, locale 변경.
대화 화면이 활성화된 동안 배너 resolve를 중지하고 홈 복귀 때 재개한다. 첫 주제 답변 성공은 해당 offer의 카드만 무효화하며 다른 offer를 완료 처리하지 않는다.
rebuild·swipe·줄 탭은 조회 계기가 아니다. 동일 진행 요청은 합치되 routine 변경 후에는 새 generation으로 재조회한다.
변경 전 count 카드와 이전 generation 응답은 적용하지 않는다.
루틴의 낙관적 UI 갱신은 서버 저장 완료가 아니다. 저장 중 popup을 닫으면 count 카드를 숨기고,
쓰기 성공/실패가 확정된 뒤 새 요청으로 갱신한다. 실패 후에도 서버 값을 다시 읽으며 낙관적 count를 배너에 넣지 않는다.

| 상태 | 표시 |
|---|---|
| 첫 조회 | 카드/dots 없음 |
| 같은 context 재조회 | 아직 유효한 직전 snapshot 유지 |
| 성공 | 검증된 새 목록 적용 |
| 빈 목록/조회 실패 | 직전 snapshot 폐기, 카드/dots 숨김, 블라인드/홈 유지 |
| context 변경 | 이전 snapshot 즉시 폐기; 세션·환경·locale·시간대/날짜·OS·앱 버전·capability로 구분 |

valid_until은 캠페인 종료와 binding 날짜 경계 중 빠른 값, null은 알려진 기한 없음이다.
만료 deadline은 `requestStartMonotonic + (valid_until - served_at)`으로 보수적으로 계산한다.
요청 시작의 기기 시각에도 같은 유효 기간을 더해 보조 deadline을 만들고, 두 시계 중 하나라도 만료되면 timer·resume·클릭 직전에 제거/CTA 차단한다. 절전 중 단조 시계가 멈춰도 기기 시각으로 만료를 검사한다. 앱 background·운세 이동에서는 진행 요청을 무효화하고 동작을 차단하되 유효한 snapshot·선택 페이지는 유지한다. 동일 context 복귀 갱신은 등장 연출을 반복하지 않는다.
프로세스 재시작 후 재조회하며 홈 체류 중 polling·push 회수는 하지 않는다. 새 예약/운영 중단은 다음 조회에 반영된다.
현재 카드 id 유지, 삭제되면 첫 카드, 0장에는 PageView/dots 없음, 1장은 점 하나. 늦은 응답으로 slat 진입을 재시작하지 않는다.

| 오류/호환성 | 처리 |
|---|---|
| 잘못된 요청 | 기존422 error envelope |
| 미지원 placement/schema | 422 BANNER_PLACEMENT_UNSUPPORTED / BANNER_SCHEMA_UNSUPPORTED |
| 인증 | 기존401 refresh/retry/SessionRejected 흐름 유지 |
| 정의 파일 로딩·공통 context·응답 envelope 손상 | 전체 조회 실패. 서버 장애는503 BANNERS_UNAVAILABLE |
| 카드 손상·미지원 요소/action/profile·배치 실패 | 해당 카드 전체 제외, 정상 카드 유지. 중복 id는 첫 유효 카드만 유지 |
| 이미지 실패/불일치/상한 초과 | 해당 카드 전체 제외. 이미지 없는 다른 카드와 홈은 유지 |
| 추가 메타데이터 | 무시 가능. 화면/버튼 의미를 바꾸는 변경은 새 capability/schema로 분리 |

알 수 없는 capability는 교집합에서 제외한다. 앱은 실제 구현된 기능만 광고한다.
카드별 raw JSON 경계를 생성 SDK로 보존하고 Data adapter가 엄격히 검사한다. UI/Domain에는 raw JSON을 노출하지 않는다.
manifest 문법·wire schema·layout profile의 의미를 별도로 버전 관리한다. 기존 의미를 깨는 변경은 기존 식별자를 재사용하지 않는다.
새 요소/필드가 화면 의미를 바꾸면 해당 capability 또는 v2를 추가한다. 구버전에는 지원 카드만 선택하고 대체 카드가 없으면 숨긴다.
초기 renderer를 실제 운영 앱에 배포한 뒤 파일만으로 디자인을 바꿀 수 있다. 개발 TestFlight 설치만으로 운영 앱 지원이 생기지는 않는다.

## 6. 배포·호환성 운영

서버 feature 브랜치에서 파일 작성·검증 → 서버 dev 반영·배포 → 같은 dev TestFlight의 다음 조회로 확인한다. 테스트 앱의 환경은 Git 브랜치와 무관하게 dev flavor다. 서버 파일 저장·기능 브랜치 push만으로 실행 중 앱 내용이 변경되지는 않는다.

서버 이미지는 코드와 정의 파일을 함께 포함하고 시작 시 메모리에 로딩한다. 별도 DB 게시 단계는 없다. 새 이미지 bytes는 bucket에 먼저 올린다. 배너 중단·복구도 파일 변경과 서버 재배포가 필요하다. 순차 배포 중에는 이전/새 정의가 잠시 섞일 수 있고 전체 이용자의 동시 전환을 보장하지 않는다.

지원 앱이 있어야 서버 변경을 해석할 수 있다. 새 요소/action을 추가하면 공동 규약·서버·클라 지원을 갱신하고 capability로 구버전을 분리한다. 운영 앱 배포, 운영 bucket 준비, 서버 운영 승격은 개발 검수와 별개다. 환경 URL을 바꾸면 파일 revision도 달라지므로 최종 배포 파일을 다시 검증한다.

콘텐츠 검수는 고정 외곽, 실제 문구/이미지 배치·대비·터치·화면 이동, 지원 언어/화면/글자 배율을 포함한다. 사용자 데이터·시간대·실패·만료·구버전 조건도 해당 변경에 맞춰 확인한다. 자동 schema 검사 통과를 실제 기기 시각 검수 완료로 간주하지 않는다. 실행 결과는 이 규약에 누적하지 않는다.

## 7. 문서 유지 규칙

| 정보 | 원본 |
|---|---|
| 고정 카드/홈 geometry | becappy-mobile의 RoomBlind/RoomStageGeometry 소스 |
| 공동 동작·경계·호환성 | moly-backend/docs/BANNER_SDUI_CONTRACT.md; 클라 동명 파일은 동일 사본 |
| 레포 연결·작성/운영 방법 | 각 레포 docs/BANNER_SDUI.md |
| 배너 콘텐츠 | 서버 app/resources/banners/home_blind.json |
| 필드/HTTP 상세·완성 예시 | 서버 schema/OpenAPI와 공유 fixture; 클라는 공식 생성 SDK |
| 변경 이유·빌드/QA/CI 결과 | Git/PR·검토 기록 |

1. 바뀐 절을 제자리에서 수정하고 폐기된 설명을 제거한다. 개발 과정·이전 빌드 번호·질문 이력을 누적하지 않는다.
2. 공동 규칙은 서버 원본과 클라 사본을 동시에 수정하고 byte 동일성을 확인한다.
3. 필드 정의·예시를 여러 문서에 복제하지 않는다. 팀 공유본은 입문용이며 최신 상세는 레포 문서를 따른다.
4. 필드/동작 변경 시 schema·fixture·관련 구현/검증 기준을 함께 갱신한다. 새 독립 책임이 생긴 경우에만 절을 추가한다.
5. 변경 완료 전 링크·예시·명령·양쪽 사본·실제 구현과의 일치를 확인한다. 문서 사본 동기화는 자동 CI 기능으로 가정하지 않는다.
