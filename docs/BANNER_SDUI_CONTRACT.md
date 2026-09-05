# 홈 배너 SDUI 공동 규약

상태: **구현 중 / 미배포** · 최신화: 2026-09-05 · API schema v1 제안

현재 합의한 동작과 구현할 계약을 정의한다. 실제 서비스에 적용된 API라는 뜻은 아니다.
레포별 책임은 [BANNER_SDUI.md](BANNER_SDUI.md), 문서 갱신 방법은 이 문서의 「문서 유지 규칙」을 따른다.

## 1. 범위와 고정 경계

**배너 크기의 진실은 becappy-mobile의 현재 목업 보라색 카드다. 배경과 내부 배치만 서버에서 바꾼다.**

| 구분 | 계약 |
|---|---|
| 고정 카드 | `RoomTheme.theme1Blind.bannerRect = Rect.fromLTWH(52, 120.5, 287.7, 158.457)` 기준. 선언은 `becappy-mobile/lib/ui/core/room/room_theme.dart` |
| 크기의 의미 | 기준 크기 **287.7 × 158.457**과 비율 고정. 실제 기기에서는 현재 홈의 무대 배율을 그대로 적용 |
| 서버 소유 | 고정 카드 안의 배경·색·테두리·텍스트/버튼 등 지원 요소의 위치·크기·내용·순서·action |
| 앱 소유 | 카드 바깥 위치·크기·홈 배율·블라인드/줄·접힘 모션·페이지 넘김·인디케이터·렌더러·화면 이동 |
| 금지 | 서버가 카드 자체의 width/height/aspect_ratio/scale을 지정하거나 콘텐츠에 맞춰 카드를 늘리는 동작 |
| 운영 | Git의 배너 파일을 서버 이미지에 포함. dev 배포·개발 TestFlight 검수 후 main 배포. 공통 정의에 사용자별 날짜·루틴 수를 채움 |
| 이미지 | 첫 버전부터 서버가 지정한 원격 배경/내부 이미지 지원. 주소·배치·크기·맞춤 방식을 정의 파일에 명시 |
| 날짜 일치 | 배너와 루틴 조회/완료/취소/통계가 같은 요청 시간대와 서버 날짜를 사용 |
| 별도 범위 | 관리자 화면, 사용자 세그먼트 선정, 외부 URL action, 운세 화면 실제 API 연동 |

배경은 고정 카드 영역을 채우며 내부 요소는 그 경계 안에서만 배치한다. 긴 문구 때문에 외곽 크기를 바꾸지 않는다.
새 캠페인·문구·배경·배치는 지원 요소 안에서 서버 배포로 변경한다. 앱 업데이트는 새 요소/동작 구현을 추가할 때 필요하다.
새 이미지는 기존 Storage 버킷에 준비하고 JSON에서 URL/메타데이터를 참조한다. 이미지 bytes를 Git에 넣는 운영은 사용하지 않는다.

## 2. 고정 canvas와 요소

| registry | v1 식별자 |
|---|---|
| 컴포넌트 | `banner_canvas_v1` |
| 배치 규칙 | `home_blind_v1`: 고정 카드 크기·폰트 매핑·배율·터치 제약 |
| 요소 | `text_v1`, `button_v1`, `image_v1` |
| 배경 | `solid_v1`, `linear_gradient_v1`, `image_background_v1` |
| action | `open_fortune`, `open_shop`, `open_conversation`, `open_routines` |

- 내부 `frame={x,y,width,height}`는 **카드 전체 기준** 0..1 좌표다. x/y≥0, width/height>0, x+width/y+height≤1, 모두 유한수다.
  이 frame은 내부 요소에만 존재한다. canvas 최상위에는 크기/위치 필드를 두지 않는다.
- font_size/radius/border/padding은 기준 카드의 design unit이다. renderer는 기준 공간에서 그리고 기존 host가 한 번 확대한다.
  TextScaler도 현재 앱 상한1.24 내에서 한 번 적용한다. 이중 배율·추가 자동 글자 축소는 금지한다.
- 한 카드 최대12요소/버튼2개. 배열 순서는 뒤→앞 그리기, 읽을 요소의 고유 semantics_order(0..11)는 읽기 순서다.
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
| image_background_v1 | type, source, fit=cover, alignment={x,y}(각0..1), base_color 필수. 고정 canvas 전체를 채우고 radius로 clip |
| image_v1 | id, type, frame, source, fit(contain/cover), alignment, accessibility_label, semantics_order 필수. frame 안에 clip |
| alignment | 0은 왼쪽/위, 0.5는 중앙, 1은 오른쪽/아래. contain은 여백 정렬, cover는 잘릴 영역의 정렬 |
| 접근성 | 장식은 accessibility_label/semantics_order 모두 null. 의미 있는 이미지는 해당 locale의 설명1..120자와 고유 읽기 순서 필수 |

이미지는 비율을 유지한다. 투명 배경의 base_color는 최하단 색이며 로딩 실패를 덮는 대체 디자인으로 사용하지 않는다.
배경 그림에 필수 문구를 구워 넣지 않는다. 문구·이동 버튼은 실제 text/button 요소로 표현하고 이미지 위의 대비도 실제 crop별로 검수한다.
GIF/APNG/움직이는 WebP·SVG·data/file URL·임시 서명 URL·redirect는 v1에 허용하지 않는다. URL에 인증정보/사용자 식별자를 넣지 않는다.
이미지 주소로 앱 Bearer 토큰/cookie를 보내지 않는다. 원본 URL을 서버 사용자 요청마다 다운로드하거나 proxy하지 않는다.
같은 URL을 덮어쓰지 않는다. 새 이미지에는 새 URL/sha256을 사용하고 dev/prod는 검수한 같은 bytes를 참조한다.
이는 기존 Storage의 [CDN 갱신 지연을 피하는 업로드 지침](https://supabase.com/docs/guides/storage/uploads/standard-uploads#overwriting-files)과도 일치한다.
각 카드의 이미지 검증/디코딩까지 끝나야 카드와 CTA를 표시한다. 실패/시간 초과는 해당 카드 전체 제외, 정상 카드는 유지한다.
갱신 중에는 기존 유효 카드만 유지할 수 있다. 늦은 이미지 완료에도 context/generation/만료를 재검사하고 제외한 카드를 되살리지 않는다.
사용자별 feed의 no-store와 달리 공통 이미지 bytes는 (허용 origin, sha256) 기준으로 캐시할 수 있다. 캐시도 bytes/hash를 검증한다.

## 3. 조회 API와 응답

`GET /banners`, operationId `listBanners`. 기존 Bearer 인증, `Cache-Control: private, no-store`.
운영용 쓰기는 이 API에 넣지 않는다. 사용자별 JSON에 공유/CDN/영속 disk cache·ETag를 사용하지 않는다.

| 입력 | 규칙 |
|---|---|
| placement | 필수 home_blind |
| schema_version | 필수 양의 정수, 지원값1 |
| platform | 필수 android/ios |
| app_version | 필수1..64자. major.minor.patch 비교, 시험용 suffix/metadata 분리. 해석 불가면 버전 제한 없는 카드만 허용 |
| capabilities | 필수 반복 query 배열, 최대32개, 각 값 `[a-z][a-z0-9_]{0,63}`, 중복 제거 |
| X-App-Locale | 최대64자 BCP47 앱 표시 언어. 미설정·미지원→en |
| X-App-Timezone | IANA 시간대1..64자. 지원 앱은 기기의 현재 식별자를 전송. 생략은 profiles.timezone, 잘못된 값은422 APP_TIMEZONE_INVALID |

정상 응답 예시:

```json
{
  "schema_version": 1,
  "placement": "home_blind",
  "revision": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "served_at": "2026-09-05T03:00:00Z",
  "items": [
    {
      "id": "today-routines",
      "data_dependencies": ["user.local_date", "routines.remaining_today"],
      "component": "banner_canvas_v1",
      "layout_profile": "home_blind_v1",
      "locale": "ko",
      "valid_until": "2026-09-05T15:00:00Z",
      "canvas": {
        "background": {"type": "solid_v1", "color": "#3A2A2C"},
        "radius": 16,
        "border": {"width": 1.5, "color": "#2A1D15"},
        "elements": [
          {
            "id": "label", "type": "text_v1", "semantics_order": 0, "vertical_align": "center",
            "frame": {"x": 0.06, "y": 0.08, "width": 0.88, "height": 0.15},
            "text": "9월 5일의 루틴",
            "style": {"font": "body", "font_size": 13, "weight": 600, "color": "#E0AC63", "align": "center", "max_lines": 1, "line_height": 1.2}
          },
          {
            "id": "headline", "type": "text_v1", "semantics_order": 1, "vertical_align": "center",
            "frame": {"x": 0.06, "y": 0.25, "width": 0.88, "height": 0.35},
            "text": "아직 3개가 기다리고 있어요",
            "style": {"font": "body", "font_size": 16, "weight": 600, "color": "#F7EEE1", "align": "center", "max_lines": 2, "line_height": 1.3}
          },
          {
            "id": "cta", "type": "button_v1", "semantics_order": 2, "vertical_align": "center",
            "frame": {"x": 0.25, "y": 0.66, "width": 0.5, "height": 0.25},
            "text": "확인하기",
            "style": {"font": "body", "font_size": 17, "weight": 600, "color": "#1B1614", "align": "center", "max_lines": 1, "line_height": 1.2},
            "background_color": "#E0AC63", "radius": 18,
            "border": {"width": 1.5, "color": "#2A1D15"}, "padding_horizontal": 12, "padding_vertical": 4,
            "action": {"type": "open_routines"}
          }
        ]
      }
    }
  ]
}
```

예시는 필드 구조 설명용이며 시각 검수된 게시본은 아니다. 예시 숫자를 runtime fallback으로 사용하지 않는다.
data_dependencies는 카드가 사용하는 source의 중복 없는 목록(user.local_date / routines.remaining_today, 정적 카드는 빈 배열)이다.
앱은 이 값으로 저장 중 루틴 의존 카드를 무효화한다. 서버가 binding에서 자동 도출하며 카드 ID나 문구로 추측하지 않는다.
위 필드는 모두 필수이고 valid_until만 nullable이다. 필수 필드 누락을 Flutter 기본값으로 채우지 않는다.
카드/요소 id는 `[a-z0-9][a-z0-9_-]{0,63}`, 각각 목록/카드 안에서 고유하다.
revision은 **배포된 정의 파일의 원본 UTF-8 bytes SHA256**이며 `[a-f0-9]{64}`다. 코드 버전/배포 순번은 아니다.
같은 revision에도 사용자·언어·시각에 따라 결과가 달라지므로 새 응답을 적용한다. 순차 배포/복구로 이전 hash가 와도 적용한다.
비활성/조건 불일치는 로딩한 파일의 revision과 items=[]다. 누락/손상된 파일은503이며 정상 빈 목록이 아니다. 응답 상한은5카드/128KiB다.

## 4. 데이터·언어·노출·action

| 데이터 | 계산·유효 기한 |
|---|---|
| user.local_date | 검증한 X-App-Timezone(생략만 profiles.timezone) + 요청의 단일 서버 UTC clock. 현지 달력일, 다음 현지 자정까지 |
| routines.remaining_today | 본인·삭제되지 않음·오늘 ISO 요일 예정·현지 오늘 미완료. **0개면 의존 루틴 배너 숨김**. 다음 현지 자정까지 |

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
| open_conversation | 기존 대화 진입, chatEnabled 등 접근 제한 유지 |
| open_fortune | 기존 운세 화면으로 이동/복귀. 운세 실제 API 연동 완료를 뜻하지 않음 |

네 action 모두 매개변수 없음. raw 경로/함수명/스크립트를 실행하지 않는다. 구매·보상·unlock을 직접 수행하지 않는다.
새 의미/매개변수는 별도 action 계약이 필요하다. 버튼 없는 안내형 카드도 허용한다.

## 5. 갱신·실패·호환성

조회 계기: 첫 홈 진입, foreground, 대화/타이머 종료, 상점/루틴 popup 닫힘, 운세 화면 복귀, locale 변경.
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
만료 timer·resume·클릭 직전에 검사해 제거/CTA 차단한다. background 경과를 clock이 보장하지 못하면 이전 snapshot을 폐기한다.
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

## 6. 배포와 완료 기준

백엔드 흐름: feature 브랜치에서 파일 작성 → 자동 검증 → dev 머지/배포 → 개발 서버를 바라보는 TestFlight 확인 → main 머지/배포.
서버 이미지가 코드와 정의 파일을 함께 포함하고, 프로세스 시작 때 검증한 파일을 메모리에 로딩한다. 별도 DB 게시 단계는 없다.
새 원격 이미지는 정의 배포 전에 불변 주소로 준비/검증한다. JSON 배포가 외부 이미지 bytes까지 자동으로 변경하는 것은 아니다.
검수한 파일 hash·서버/클라 SHA·TestFlight 빌드와 결과를 PR/CI에서 연결한다. 파일/관련 구현이 바뀌면 다시 검수한다.
운영 순차 배포 중에는 이전/새 정의가 잠시 섞일 수 있다. 반영 시점은 앱의 다음 조회이며 전체 이용자 동시 전환은 보장하지 않는다.
배너만 복구할 때는 과거 파일을 복원해 검증/재배포한다. 서버 이미지 rollback은 코드와 파일을 함께 되돌린다.
같은 파일은 같은 revision이고, 일정과 사용자 날짜/count는 현재 시점으로 재계산한다. 상세 배포/검증은 서버 적용 문서가 소유한다.

구현 완료에는 다음 검증이 필요하다. 실행 로그/결과는 PR·CI에 남기며 이 절에는 통과 기준만 유지한다.

- 개발 서버 연결 **동일 TestFlight 빌드**에서 서버 파일 A→B 배포의 배경·문구·내부 frame/색 변경; **카드 외곽 크기/비율 불변**.
- 사용자별 count, 오늘 요일·삭제·완료/취소·0개 숨김·현지 자정/DST·시간대 변경/다중 기기·저장 중 popup 복귀.
- 네 action 이동/back/제한, 기존 블라인드·줄·모션·테마 조건 유지.
- 5→1→0장, 실패/중단/rollback, 손상 카드 격리, 구버전·계정/언어 전환·늦은 응답·만료.
- 실제 stage 폭×높이·지원 ko/en/ja·기본/최대 글자 배율에서 overflow·hit 영역·접근성 검사.
- 이미지 변경/투명도/crop·깨진 캐시·404/timeout·hash 불일치·대용량/움직이는 파일·전환 중 늦은 완료·실기기 메모리 검사.
- 코드 생성/계약 sync, 이미지 내 파일 포함·검수 파일 hash 일치·누락/손상·순차 배포/복구 검사.

## 7. 문서 유지 규칙

| 정보 | 유일한 원본/관리 위치 |
|---|---|
| 고정 카드/홈 geometry | becappy-mobile의 RoomBlind/RoomStageGeometry 소스. 계약에 필요한 고정 경계만 이 문서에 명시 |
| 공동 동작·경계·호환성 | **moly-backend/docs/BANNER_SDUI_CONTRACT.md**. 클라의 동명 파일은 동일한 배포 사본 |
| 레포 내부 구조/운영 방법 | 각 레포 BANNER_SDUI.md. 공동 규약을 재서술하지 않고 참조 |
| 운영 배너 정의 | 서버의 app/resources/banners/home_blind.json. 문서 예시/DB를 운영 원본으로 사용하지 않음 |
| 구현된 HTTP/DB 상세 | 서버 분할 OpenAPI, models/schema/migrations. 클라는 생성 bundle/SDK를 동기화 |
| 변경 이유·조사·실행 결과 | Git/PR·CI. 이 규약에 시간순 이력이나 검토 보고서를 누적하지 않음 |

1. 변경 시 **해당 절을 수정하고 기존 설명을 교체**한다. 독립적인 새 책임이 있을 때만 절을 추가한다.
2. 공동 동작/필드 변경은 원본과 클라 사본을 함께 갱신한다. UI 경계 변경은 클라이언트 진실과 먼저 대조한다.
3. 바뀐 규칙의 검증 기준과 영향을 받는 레포 적용 문서만 함께 수정한다. 중복·폐기된 규칙/예시는 제거한다.
4. 한 개념은 한 곳에서 정의한다. 공식 schema/fixture가 구현되면 여기에 중복한 필드 설명/긴 JSON을 원본 링크로 교체한다.
5. 질문 이력·폐기안·조사 과정·완료 로그·미승인 확장 상세는 넣지 않는다. 미결정은 범위 절의 짧은 항목으로만 유지하고 결정 즉시 교체한다.
6. 구현 계획은 레포의 정식 specs 절차, 배너 변경/배포 이력은 Git/PR·CI에 둔다. 문서에 체크리스트/테이블 사본을 계속 추가하지 않는다.
7. 변경 완료 전 상태/최신화 날짜, JSON·링크, 두 사본의 동일성, schema/fixture sync와 관련 검증 기준을 확인한다.

현재 이 규칙은 문서 갱신 절차다. 자동 동기화/CI 검사가 구현된 것으로 간주하지 않는다.
