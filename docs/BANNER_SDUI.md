# 배너 SDUI — 서버 운영

배너의 디자인·문구·노출 조건은 서버 파일이, 이미지 파일은 Supabase Storage가 소유한다. 앱은 공통 규약에 따라 그리며 등록된 화면으로 이동한다. 필드·제약·호환성의 원본은 [공동 규약](BANNER_SDUI_CONTRACT.md)이다.

`open_topic_conversation_v1`은 배너에 보인 질문으로 대화를 준비한다. [주제 대화](BANNER_TOPICS_DESIGN.md)에 선택·첫 답변·복구·콘텐츠 운영 규칙을 둔다. `open_conversation`은 일반 대화 진입이며 답변하지 않고 나간 주제 질문을 복원하지 않는다. 캐피의 일기 목록·감정 기록·루틴·타이머 설정·음악 선택 이동도 지원하며 action 이름은 공동 규약을 따른다. 새 action을 지원하지 않는 앱에는 해당 카드를 제공하지 않는다.

## 배너를 바꾸는 순서

1. 이 레포의 [app/resources/banners/home_blind.json](../app/resources/banners/home_blind.json)을 편집한다. 새 카드는 기존 카드 구조를 복사해 고유 `id`를 부여한다. `banners` 배열이 노출 순서다. 한국어 문구를 추가·수정할 때 영어·일본어 canvas도 함께 작성한다. 스키마에서 영어 `en` canvas는 필수다.
2. 새 이미지가 있으면 개발 Supabase의 공개 `banner-assets` bucket에 먼저 업로드한다. JSON에 공개 URL과 파일 메타데이터를 반영한다. 이미지 준비 절차는 아래를 따른다.
3. 레포 루트에서 파일과 실제 원격 이미지를 검증한다. 위치·글자 크기를 바꿨다면 클라이언트의 실제 폰트와 `measureBannerCard`로 각 언어·화면 크기·최대 글자 확대도 확인한다. 서버 스키마 통과만으로 표시를 보장하지 않는다.

   ```sh
   uv run python scripts/validate_banners.py --assets --environment dev
   ```

4. 변경을 서버 `dev` 브랜치에 반영한다. [Deploy dev](../.github/workflows/deploy-dev.yml)가 서버 이미지를 빌드하고 배포한다. **기능 브랜치 push나 JSON 저장만으로 실행 중 서버가 바뀌지는 않는다.**
5. 배포 성공과 `https://dev.moly.asia/health`의 커밋 SHA를 확인한다. 배포 과정의 `check_running_banners.py`는 실행 중 서버의 배너·주제 catalog revision과 배포 이미지 속 두 파일 hash도 대조한다.
6. **dev TestFlight에서 홈 재진입 또는 앱 복귀**로 결과를 확인한다. 지원하는 문구·스타일·이미지·배치 변경은 앱 재빌드 없이 적용된다. 홈에 계속 머물러 있을 때 실시간 push로 바뀌지는 않는다.

검증·배포 Actions가 실행되지 않거나 실패하면 새 정의가 반영됐다고 판단하지 않는다. 서버 배포가 끝난 시점과 앱이 다시 조회하는 시점을 구분한다.

## 어디를 수정하는가

아래 canvas 경로는 `banners[].canvases_by_locale.<언어>` 기준이다. 각 언어의 문구와 배치를 함께 확인한다.

| 바꿀 내용 | JSON 위치 |
|---|---|
| 카드 배경 | `background`: 단색·그라데이션·원격 이미지 |
| 배경 위 반투명 덮개 | 이미지 배경의 `background.overlay`: `color`와 `opacity`(0..1). 생략/null이면 효과 없음. 지원 앱 필요 |
| 카드 외곽선·모서리 | canvas의 `border.width`(0이면 선 없음), `radius` |
| 문구 | `elements[].text.value` (`text_v1`, `button_v1`의 template) |
| 글자 크기·색·정렬 | `elements[].style` |
| 요소의 위치·크기 | `elements[].frame`의 `x`, `y`, `width`, `height` |
| 버튼용 이미지·장식 | `image_v1`의 `source`, `frame`, `fit`, `alignment` |
| 색상 면·테두리·모서리 | `shape_v1`의 `background`, `border`, `radius` |
| 클릭 범위·이동 | `action_region_v1`의 `frame`, `content_ids`, `action.type` |
| 기존 단색 버튼 | `button_v1`의 `background_color`, `radius`, `border`, `padding_*`, `action` |
| 노출·기간·플랫폼 | 카드의 `enabled`, `starts_at`, `ends_at`, `platforms`, 앱 버전 범위 |
| 노출 순서·전체 중단 | 최상위 `banners` 순서, 최상위 `enabled` |

좌표는 **고정 카드 전체 기준 0..1 비율**이다. 예를 들어 `x: 0.42`는 카드 왼쪽에서 42% 지점이다. 카드 바깥 크기는 변경할 수 없다.

버튼의 실제 터치 영역은 최소 48px로 확장된다. 본문 frame이 이 영역과 겹치면 앱이 카드 전체를 숨긴다. 문장 위치와 클릭 영역을 함께 검증한다. 현재 세 배너의 본문은 짧은 선의 아래 경계와 버튼 배경의 위 경계 사이에 중앙 정렬한다. 운세는 날짜·질문 두 줄 전체를 하나의 묶음으로 중앙 정렬한다. 버튼 그림과 최소 클릭 영역은 서로 다를 수 있다.

이미지 버튼은 `image_v1` + 필요 시 `text_v1`/`shape_v1` + `action_region_v1`로 구성한다. 클릭 영역의 `content_ids`에 시각 요소의 ID를 연결하고, 영역 안에 해당 요소의 전체 frame이 들어오도록 편집한다. 시각 요소와 클릭 영역을 각각 옮겨야 하며 자동으로 따라 움직이는 부모·자식 좌표계는 없다.

이미지 버튼의 응답 예시는 [composed_feed.json](../tests/fixtures/banners/composed_feed.json)을 참고한다. 현재 배너 파일은 운세·대화 주제·상점 순서로 배경, 제목, 본문과 `shape_v1` + `text_v1` + `action_region_v1` 버튼을 정의한다. 운세 날짜는 `date` 요소의 `{day}`로 요청 시간대의 오늘을 표시하고, 질문은 `message` 요소로 분리해 굵기를 각각 조절한다. 버튼은 각각 기존 운세 화면, 주제 준비를 거친 대화 화면, 상점 화면으로 이동한다. 정확한 필수 필드는 [서버 스키마](../app/schemas/banners.py)를 따른다.

## 이미지 준비

- 개발 프로젝트: `wywzjslvxwttxkecbyis`, 공개 bucket: `banner-assets`.
- `source`에는 `url`, `sha256`, `byte_length`, `pixel_width`, `pixel_height`, `media_type`을 기록한다. 크기는 화면 표시 크기가 아니라 **업로드한 원본 파일** 기준이다.
- 정지 **PNG·JPG(JPEG)·WebP**를 지원하며 **WebP를 권장**한다. 업로드 파일은 **512KiB(524,288바이트) 이하**여야 한다. 한 변 2048px, 총 1,048,576픽셀 이하이며 카드당 배경 포함 이미지 요소는 최대 2개다.
- 새 파일명/경로로 업로드한다. 기존 URL을 덮어쓰지 않고 과거 배너가 참조하는 파일도 보관한다. 임시 서명 URL·SVG·움직이는 이미지는 지원하지 않는다.

로컬 이미지의 메타데이터는 다음처럼 확인할 수 있다. `image.png`를 업로드할 파일로 바꾸고, 출력값에 Storage의 공개 `url`을 추가한다.

```sh
uv run python - <<'PY_IMAGE'
from pathlib import Path
from hashlib import sha256
from PIL import Image
import json
p = Path("image.png")
b = p.read_bytes()
with Image.open(p) as im:
    print(json.dumps({"sha256": sha256(b).hexdigest(), "byte_length": len(b),
                      "pixel_width": im.width, "pixel_height": im.height,
                      "media_type": Image.MIME[im.format]}))
PY_IMAGE
```

## 사용자 데이터·언어

`bindings`에서 허용된 데이터만 사용하며 서버가 계산을 마친 문자열을 응답한다. 임의 코드나 수식을 실행하지 않는다.

| source | 의미 |
|---|---|
| `user.local_date` | 요청한 기기 시간대의 오늘. format은 `month_day` 또는 `full_date` |
| `topic.question` | 현재 offer의 질문 원문. format null, text에는 `{question}` 단독 사용 |
| `routines.remaining_today` | 본인·오늘 요일 예정·미삭제·현지 오늘 미완료 루틴 수. format은 null. 0개면 의존 배너 숨김 |

문구는 `{"kind":"template","value":"오늘은 {day}"}`처럼 작성한다. `{day}`는 등록한 binding alias이며 literal 중괄호는 `{{`/`}}`로 쓴다. `count_cases`는 0/1/그 외 문구를 선택한다. `when`은 binding의 `eq` 또는 `gt` 조건이며 루틴 배너에는 `remaining > 0` 조건을 둔다.

`ko`, `en`, `ja` 등 언어별 canvas를 저장한다. 요청 언어가 없으면 영어 canvas 전체를 사용한다. 서버가 사용된 capability를 자동 수집하므로 지원하지 않는 앱에는 해당 카드를 보내지 않는다.

배너 문구 작업은 한국어를 기준으로 **한국어·영어·일본어를 함께 완료**한다. 사용자가 한국어만 제공해도 별도 요청 없이 제목·본문·버튼·접근성 이름의 영어·일본어 문구를 작성한다. 직역보다 각 언어에서 자연스러운 짧은 앱 문구를 우선하되 의미·말투·동작과 `{day}` 같은 binding은 유지한다. 각 언어의 글꼴·줄바꿈·확대 글자·버튼 영역을 검수한다. 이는 작성 규칙이며 실행 중 자동번역 기능이 아니다.

## 런타임과 유지보수

- [BannerCatalog](../app/services/banner_catalog.py)는 서버 시작 시 번들 JSON을 strict 검증하고 메모리에 로딩한다. 파일 원본 bytes의 SHA256이 `revision`이다. 사용자별 날짜·루틴 수는 조회 시 계산한다.
- 시각 정의는 파일 기반이며 별도 게시 명령이 없다. 사용자별 주제 위치·일일 준비 횟수와 대화 준비/완료 상태만 `user_topic_states`, `chat_topic_entries`에 저장한다. 하루 두 주제까지 준비하고 날짜가 바뀌면 현재 배너에서 한 개 전진한다. 질문 문구의 편집 원본은 `app/resources/conversation_topics/catalog.json`이다. [Dockerfile](../Dockerfile)이 정의 파일을 서버 코드와 함께 포함한다. 컨테이너 파일 수동 수정이나 hot reload는 운영 경로가 아니다.
- [배너 API](../app/api/banners.py)는 기존 Bearer 인증과 `private, no-store` 응답을 사용한다. 정의 로딩 실패는 503, 비노출은 정상 빈 목록이다.
- [AppDay](../app/core/app_day.py)와 요청 `X-App-Timezone`을 배너·루틴이 공유한다. 사용자 profile의 시간대나 과거 완료 기록을 덮어쓰지 않는다.
- [서비스](../app/services/banners.py)는 필요한 binding을 묶어 조회한다. 실패한 데이터를 0이나 샘플 문구로 대체하지 않는다.
- 배너 전체 중단은 최상위 `enabled: false`, `banners: []`로 검증·재배포한다. 복구는 과거 JSON을 복원해 검증·재배포한다. 중단·복구도 다음 앱 조회에 반영된다.
- 운영 승격은 별도다. 같은 이미지 bytes를 운영 `banner-assets`에 준비하고 URL을 운영 주소로 바꾼 뒤 `--environment prod`로 재검증한다. 환경 URL이 바뀌므로 정의 revision도 달라진다. 개발 DB를 운영 DB에 복사하지 않는다.

코드/규약 변경은 관련 테스트와 `scripts/openapi_contract.py --check`를 수행한다. HTTP 변경 시 분할 OpenAPI→bundle→클라 SDK 순서로 동기화한다. 새 요소/action은 공동 규약·서버·클라 지원을 함께 추가해야 한다. 감정 기록 버튼은 `action: {"type": "open_mood"}`로 작성한다. `open_mood` 지원 클라이언트 빌드가 필요하며, 서버 배포만으로 구버전 앱에 새 이동 기능이 생기지는 않는다.

이 문서에는 현재 작성·배포 절차만 유지한다. 변경 이유·빌드 번호·CI 결과·QA 이력은 PR/검토 기록에 둔다. 공동 규칙 변경은 서버 원본과 클라 사본을 함께 갱신한다.
