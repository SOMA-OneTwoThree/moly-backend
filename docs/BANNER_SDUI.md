# 홈 배너 SDUI — 서버 적용

상태: **구현 중 / 미배포** · 최신화: 2026-09-05

공개 동작·필드·고정 카드·문서 갱신 절차는 [공동 규약](BANNER_SDUI_CONTRACT.md)이 소유한다.
이 문서에는 파일 로딩·binding 실행·서버 연결·배포/검증 방법만 둔다. API·로더·검증 도구와 배포 전후 gate를 구현했다. 실제 배포와 앱 통합 검수는 별도다.

초기 배경·장식의 bucket 공개 URL 확정 전까지 번들 manifest는 `enabled=false`, `banners=[]`다.
이 상태는 빈 배너 응답만 검증한다. 실제 이미지 검증·A→B 변경 검수에는 URL과 캠페인 파일 반영이 필요하다.

## 1. 서버 책임과 입력 모델

서버는 **클라 목업의 고정 카드 안**에 배경과 요소를 정의한다. 카드 외곽 크기는 becappy-mobile 소스가 원본이다.
운영 manifest/응답 canvas에 root width/height/aspect_ratio/scale을 허용하지 않는다. 내부 element.frame만 배치를 지정한다.
카드 크기를 바꿔야 통과하는 디자인은 배포하지 않고 같은 고정 영역 안에서 수정한다.

| 위치 | 필드 |
|---|---|
| manifest root | manifest_format_version=1, wire_schema_version=1, placement=home_blind, enabled, banners(노출 순서) |
| banner | id, component, layout_profile, enabled, starts_at/ends_at(nullable UTC), platforms, min_app_version/max_app_version_exclusive(nullable) |
| banner 데이터 | bindings(alias→source/format), nullable when, canvases_by_locale |
| locale canvas | 공개 canvas와 동일한 디자인 필드. text는 운영용 template/count_cases 선언 |

파일 입력은 최대50카드/256KiB다. CI/시작 시 파일 전체를 strict 검증하고, 조회 시 envelope와 개별 캠페인 검증을 분리한다.
고유 id·알려진 필드/요소·고정 profile·frame·문구/개수 상한·일정/버전 순서·영어 canvas·binding 참조를 검사한다.
사용한 component/profile/element/background/action에서 capability를 자동 수집한다. 작성자 수기 목록은 신뢰하지 않는다.
공통 캠페인 선정과 사용자 데이터 계산은 분리한다. 후속 세그먼트 기능은 서버 selector 경계에서 확장한다.

### text와 조건 문법

- 정적 문구도 `{kind: "template", value: "..."}`로 선언한다. `{alias}`만 치환, literal brace는 `{{`/`}}`다.
- count_cases는 등록한 비음수 정수 binding의 zero/one/other 중 정확히0/1/그 외를 선택한다.
- 날짜 format은 month_day/full_date만 허용하고 [i18n](../app/services/i18n.py)의 locale 기준을 따른다.
- when은 `{binding, operator: eq|gt, value}` 단일 조건이다. 루틴 의존 배너에는 remaining>0을 필수로 적용한다.
- 미등록/누락 alias·속성 접근·format expression·함수/eval·중복 JSON key·NaN/Infinity는 거부한다.
- 계산 후에도 공개 문구 길이/스키마를 검사한다. 새 binding은 서버에 등록하고 기존 source의 의미를 바꾸지 않는다.

배포 workflow는 선택한 이미지의 `validate_banners.py --assets`를 먼저 실행한다. SSM 배포 후 같은 컨테이너의
`check_running_banners.py`가 내부 `/health/banners` 응답 revision과 파일 hash를 대조한다. 기존 `HEALTH_TOKEN`을
프로세스 내부에서만 사용하며 토큰은 출력하지 않는다. 도구가 없는 SDUI 이전 이미지는 기존 롤백 경로를 유지한다.

## 2. 사용자 데이터와 읽기 경계

인증 user_id, 검증한 X-App-Timezone(생략만 profiles.timezone), 앱 요청 locale, 요청에서 한 번 읽은 서버 UTC clock으로 계산한다.
user_id·fixture·시간 override를 공개 API 입력으로 받지 않는다.
[기존 routine service](../app/services/routine.py)의 전체 목록을 그대로 count하지 않고 아래 조건을 적용한다.

1. 본인 user_id, deleted_at IS NULL.
2. 사용자 현지 오늘의 ISO weekday가 days_of_week에 포함.
3. 같은 본인/routine_id/현지 오늘 completion이 없음. NOT EXISTS/anti-join COUNT로 한 번 조회한다.

routine_completions.activity_date는 이 기능에서 현지00:00 reward_date다. 채팅의04:00 날짜를 사용하지 않는다.
[AppDay](../app/core/app_day.py)가 한 번 캡처한 clock과 IANA 시간대에서 오늘과 다음 달력일 자정을 계산한다.
현재 클라는 온보딩 때만 시간대를 저장하므로 자동 동기화가 있다고 가정하지 않는다. 이번 범위에서 요청 시간대 규칙을
[routine API](../app/api/routine.py)와 service의 목록/_today/완료/취소/통계에 함께 적용하고 날짜 응답 헤더를 OpenAPI에 명시한다.
헤더가 없으면 기존 저장 시간대를 사용한다. 다른 기기의 profile/과거 기록을 변경하지 않으며 소유권·요일·완료 유일성 규칙은 유지한다.
기존 `is_valid_iana_timezone`으로 명시 헤더를 검증하고, 손상된 저장 값에만 safe_zone의 기존 fallback을 쓴다.
날짜/served-at/day-ends-at 응답 헤더는 실제 계산에 사용한 같은 clock/시간대에서 만들며 날짜를 사용하는204 응답에도 포함한다.

```mermaid
flowchart LR
    A[인증/번들 정의] --> F[기간/OS/버전/언어/capability]
    F --> B[필요 binding 일괄 조회]
    B --> C[조건/번역 계산]
    C --> V[카드 검증/최대5장 응답]
```

- 배너 정의는 메모리에서 읽고, 필요한 profile1회/count1회가 기본 DB 읽기 상한이다. 카드별 반복 조회·내부 HTTP 호출은 하지 않는다.
- 정적 카드만 있으면 필요 없는 profile/count를 읽지 않는다. 사용자별 응답/데이터를 공유 캐시하지 않는다.
- binding 조회 실패를0이나 샘플 값으로 바꾸지 않는다. 복구 가능한 SQL 오류는 savepoint rollback 후 의존 카드만 제외한다.
  [기존 pg 처리](../app/core/pg.py)와 같은 transaction 복구 원칙을 적용한다. catch만 하고 깨진 transaction에서 계속 실행하지 않는다.
- 번들 정의 로딩·공통 context·필요한 DB 연결 유실·전체 request budget 실패는503 BANNERS_UNAVAILABLE이다.
- 전체 budget은 pool 대기/SQL/compiler를 포함한 한 경계로 집행한다. 초기2초는 개발 환경에서 검증할 실험값이며 SLA가 아니다.
  transaction/savepoint 제어문은 데이터 조회 횟수와 별도로 측정한다. client 공통10초 timeout은 유지한다.

## 3. 번들 파일과 로딩

원본은 `app/resources/banners/home_blind.json` 한 파일로 둔다. 글자 크기·텍스트/버튼 frame·색·배경·action을 여기서 편집한다.
[Dockerfile](../Dockerfile)의 `COPY app ./app`에 포함시켜 코드와 같은 이미지로 배포한다. 배너 전용 DB 테이블·쓰기 API·게시 명령은 만들지 않는다.

- 서버 프로세스 시작 때 고정 경로의 UTF-8 bytes를 크기 제한 안에서 읽고 전체 검증한 immutable snapshot을 메모리에 보관한다.
  요청마다 파일을 다시 읽거나 요청 입력으로 파일 경로를 선택하지 않는다. 운영 컨테이너 파일 수정/외부 mount/hot reload는 사용하지 않는다.
- 응답 revision은 **그 원본 bytes의 SHA256 소문자64자리**다. 재직렬화한 JSON이나 사용자별 응답을 hash하지 않는다.
  파일과 revision을 한 snapshot으로 묶고 각 요청이 하나만 사용한다. 코드 버전은 별도 서버 SHA로 추적한다.
- 미노출은 유효한 enabled=false,banners=[] 파일로 표현한다. 누락/잘못된 파일을 정상 빈 배너로 바꾸지 않는다.
  로딩 실패 시 배너만 unavailable로 두고 `/banners`는503을 반환한다. 다른 API는 기존 동작을 유지한다.
- 이미지 빌드 검증은 실제 패키징된 파일을 runtime과 같은 loader로 읽어 hash/스키마를 확인한다. 실패하면 배포를 진행하지 않는다.
  [검증기](../scripts/validate_banners.py)를 이미지 빌드와 배포 workflow에 연결한다. 실제 배포 실행 검증은 별도다.
- 메모리에는 공통 정의만 보관한다. 사용자별 날짜/count는 요청 시 해당 환경의 기존 DB에서 읽고 공유 정의를 변경하지 않는다.

### 이미지 원본

정의 파일은 이미지의 불변 URL/hash/메타데이터를 참조한다. bytes는 기존 Supabase Storage 공개 asset 배포 방식을 재사용한다.
초기 origin 후보는 현재 상품 자산의 `https://qkgjlgzsharnilxnkytd.supabase.co`다. 배너 전용 prefix와 업로드 권한은 실제 저장소 설정을 확인해 확정한다.
새 이미지를 먼저 업로드하고, 공개 읽기/형식/상한/hash/해상도를 검증한 뒤 그 URL을 참조하는 JSON을 dev에 반영한다.
검증기는 인증 헤더 없이 허용 origin만 조회하며 redirect를 따르지 않는다. API 시작/사용자 요청은 외부 자산 다운로드를 기다리지 않는다.
dev/prod는 같은 공개 이미지 bytes를 사용한다. 개인화 정보는 이미지나 URL에 넣지 않는다.
과거 URL을 덮어쓰거나 복구 가능한 정의가 참조하는 파일을 삭제하지 않는다. 배너 중단 파일은 외부 이미지 검증 없이 배포할 수 있어야 한다.

## 4. 검증·배포·되돌리기

백엔드 흐름: feature 브랜치 작업 → 자동 검증 → dev 머지/배포 → 개발 서버를 바라보는 TestFlight 확인 → main 머지/배포.
GitHub Actions는 서버 이미지를 배포하고 서버가 포함된 파일을 로딩한다. 배포 후 별도 DB 게시 단계는 없다.

- PR/CI에서는 API와 같은 validator/compiler 및 합성 데이터로 규약·binding·경계값을 검사한다.
  개발 TestFlight는 실제 API/계정/제품 Flutter renderer로 문구·배치·동작을 최종 확인한다. 별도 preview 화면/게시 proof는 필수 운영 절차가 아니다.
- 현재 CI는 pull_request 계기이고 dev/main push 배포와 별개다. 배너 검증은 배포 workflow 안에서도 실행하여
  같은 배포 대상 이미지의 파일/참조 자산 검증이 성공해야 인스턴스 교체로 진행한다. PR 검증만으로 dev 배포가 차단된다고 가정하지 않는다.
  기존 이미지 재배포도 그 이미지의 정의를 검사한다. 도입 이전 이미지로 복구하면 배너 API가 없을 수 있으며 앱은 조회 실패 정책을 따른다.
- dev에서 검수한 원본 파일 hash가 main에 반영될 파일과 같은지 확인한다. 머지 과정에서 내용이 바뀌거나 관련 서버 동작이 바뀌면 재검수한다.
  환경에 따라 파일을 다시 작성하지 않는다. 개발 DB를 운영 DB에 복사하지 않고 운영 요청은 운영 사용자 데이터를 사용한다.
- 운영의 기존 순차 배포 중에는 이전/새 이미지의 응답이 잠시 섞일 수 있다. 각 프로세스는 자신의 코드와 파일을 함께 사용한다.
  revision은 순번이 아니며 앱은 낮고 높음을 비교하지 않는다. 정상 새 응답은 같은 hash/이전 hash여도 적용한다.
  배포 완료는 기존 health/SHA 확인과 배너 로딩 점검으로 확인한다. 배포 실패 시 일부 인스턴스가 이미 새 버전일 수 있다.
  현재 `/health/ready`는 DB 점검이므로 배너 로딩 성공을 증명하지 않는다. 보호된 진단/배포 점검에 catalog 상태/hash를 연결하되
  배너 장애만으로 전체 API readiness를 내려 서비스에서 제외하지 않는다.
- 배너만 되돌릴 때는 과거 파일을 현재 브랜치에 복원하고 현재 validator로 검증한 뒤 재배포한다.
  서버 전체 rollback은 코드와 배너를 함께 과거 이미지로 되돌리는 기존 배포 절차다. 두 경우 모두 일정과 사용자 데이터는 현재 시점으로 계산한다.
- 전체 중단은 enabled=false,banners=[]로 수정해 재배포한다. 빈 콘텐츠는 규약/패키징 검사로 확인한다.
  앱 반영은 다음 조회 시점이며 홈 체류 중 즉시 회수는 보장하지 않는다. 배너 변경·중단·되돌리기 모두 서버 배포가 필요하다.
- 변경 이력은 Git/PR, 적용 이력은 CI/배포 이미지에 둔다. 복구할 서버 이미지와 참조 asset을 함께 보관한다.

## 5. 구현 연결과 관측

| 위치 후보 | 책임 |
|---|---|
| app/api/banners.py, app/main.py | 라우터·기존 인증·입력/공통 오류·private/no-store |
| app/schemas/banners.py | Banner 접두어의 공개 스키마와 운영 manifest 검증 |
| app/services/banners.py | 후보 선택·binding 조합·최종 응답. binding 코드가 커질 때 별도 모듈 분리 |
| app/api/routine.py, app/services/routine.py, app/core/app_day.py | 요청 시간대/단일 clock·날짜 응답 헤더, 배너와 같은 현지 날짜 계산 |
| app/resources/banners/home_blind.json | 서버 이미지에 포함되는 배너 정의 원본 |
| app/services/banner_catalog.py | 시작 시 파일 검증/로딩·revision·immutable snapshot |
| scripts/validate_banners.py, 기존 CI/배포 workflow | runtime과 같은 loader/compiler로 파일·패키징 검증, 배포 점검 |
| openapi/paths/banners.yaml, openapi/components/banners.yaml | 공개 HTTP 기계 판독 원본, 상위 openapi.yaml 참조 |

wire items의 raw 값 경계는 카드별 클라 파싱을 위한 것이다. 서버는 최종 카드를 strict 검증하고 UI에 임의 JSON을 실행시키지 않는다.
생성 SDK·normalizer에서 손상/unknown 카드와 정상 카드의 혼합 파싱을 먼저 확인한다. generation 결과를 수동 수정하지 않는다.
진단에는 trace/placement/revision/banner id/서버 SHA/제외 사유만 중복 제한해 기록한다. 토큰·문구·count·루틴명은 기록하지 않는다.
클라 진단도 기존 수집 설정을 따른다. 노출/클릭 분석을 이번 기능에 추가하지 않는다.

## 6. 검증과 개발 배포

| 수준 | 통과 기준 |
|---|---|
| Unit/contract | binding/조건·언어 fallback·상한·고정 root 필드 거부·capability·실제 HTTP/OpenAPI 일치 |
| DB integration | 사용자/요일/삭제/완료/count·자정/DST·기기/저장 시간대 불일치·구버전 헤더 생략, savepoint 복구/연결 유실 |
| 파일/배포 | 실제 이미지 파일 포함·hash 일치·누락/손상 차단·프로세스 snapshot·순차 배포·재배포/중단/복구 |
| 원격 이미지 | 불변 URL·공개 읽기·실제 bytes/hash/형식/크기·redirect 거부·복구 파일 보존·중단은 자산 장애에 독립 |
| 개발 서버+동일 TestFlight 빌드 | 공동 규약의 디자인 변경/고정 외곽·실데이터·이동·실패/복구 조건 |

구현 시 분할 OpenAPI를 수정한 뒤 bundle을 생성·검증하고, 그 계약을 커밋한 뒤 클라에 sync한다.
클라 sync는 미커밋 bundle을 거부하며 전체 서버 계약을 복사한다. 배너/루틴 외 기존 API의 생성 DTO 호환성도 함께 확인한다.

```bash
uv run python scripts/openapi_contract.py --write
uv run python scripts/openapi_contract.py --check
uv run ruff check app scripts tests
uv run pytest -q --ignore=tests/integration
```

위 suite와 별도로 실제 DB 통합/개발 서버 검증이 필요하다.
[deploy-dev.yml](../.github/workflows/deploy-dev.yml)의 dev 배포 경로로 반영하고 https://dev.moly.asia/health SHA를 확인한다.
feature 브랜치 push만으로 개발 서버가 바뀌지 않는다. 로그인/계정 서버와 Supabase Auth는 공용이며 별도 개발 배포를 전제하지 않는다.
개발 API는 공용 Auth의 issuer/JWKS를 검증하고, SUPABASE_DB_CONNECTION_STRING은 개발 DB를 선택한다. 클라 flavor가 DB를 직접 고르지 않는다.
개발 DB에는 공용 JWT sub에 대응하는 테스트 프로필과 참조 관계가 필요하다. 현재 profiles.id가 auth.users를 참조하므로
프로필 한 행만 임의 생성하거나 운영 DB를 자동 복사하지 않는다. https://dev.moly.asia가 개발 DB에 연결된 구성은 사용자에게 확인했다.
클라에는 개발 API 주소만 설정하고 공용 계정 로그인 후 개발 루틴 조회·변경은 dev TestFlight에서 검수한다.
공용 계정 API가 처리하는 프로필 변경/탈퇴까지 개발 DB로 분리되는 것은 아니다.
개발 TestFlight에서 공동 완료 조건을 확인한 뒤 main으로 반영한다. [운영 배포](../.github/workflows/deploy.yml)는 main push가 계기다.
검증한 파일 hash·서버/클라 SHA·TestFlight 빌드·기기/언어를 PR·CI에 남긴다. 지원 요소의 배너 파일만 변경하면 같은 앱으로 재검수한다.
구현 완료 시 [문서 안내](README.md)의 API_SPEC/ARCHITECTURE에 해당 책임만 최신화하고 이 문서와 상세를 중복하지 않는다.
