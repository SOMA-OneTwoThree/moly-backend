# 오늘의 운세

기준: `fortune-copy.v8-localized.3`. 한국어·영어·일본어를 지원하며 실행 중 LLM 호출 없이 검수된 문구를 선택한다. API의 기준은 [경로](../openapi/paths/fortune.yaml)와 [응답 스키마](../openapi/components/fortune.yaml)다.

## 공개 범위와 API

| 항목 | 계약 |
| --- | --- |
| 입력 | `birth_date`, `gender`. 생년월일은 1900-01-01 이상, 사용자 현지 날짜 기준 만 14세 이상 |
| 기본 결과 | 종합 점수·총평·`overall.flow`·해볼 것·조심할 것·행운색. 광고 전에도 공개 |
| 상세 결과 | `categories`의 네 분야 점수와 문구. 구독·스토어 체험·가입 체험 또는 검증된 당일 광고 해금으로 공개 |
| 언어 | `X-App-Locale` → 계정 언어 → 한국어. 지역 태그는 ko/en/ja로 정규화 |
| 응답 문단 | `overall.flow` 2~3개, 분야별 `text` 1~3개. 현재 원고의 총운은 2개이며 과거 저장본의 3개도 읽음 |

- `/fortune-profile`: GET/PUT/DELETE. 동일 입력을 다시 저장하면 revision과 당일 결과가 바뀌지 않는다.
- `/daily-fortune/status`: 현재 상태와 저장된 결과 조회. 상세가 잠기면 `categories`를 제외한다.
- `/daily-fortune/reveal`: 사용자별 잠금 아래 오늘 결과 생성 또는 기존 저장본 반환.
- `/daily-fortune/ad-sessions`: 무료 사용자의 상세 해금용 광고 세션. 앱의 광고 완료 통보만으로 해금하지 않고 기존 SSV 검증을 따른다.

`free`·`launch`를 상세 무료 권한으로 간주하지 않는다. 권한 판단의 원본은 [운세 서비스](../app/services/fortune.py)와 기존 entitlement 서비스다.

## 계산과 저장

- 점수는 `fortune-experience.v1` 정책을 사용한다. 일반은 주로 70~100점이며 약 1%의 결과에서 한 항목만 60~69점이다. 최초 이용은 각 항목 90~100점이다.
- 생년월일·현지 날짜·최초/일반 모드·알고리즘과 점성술 신호로 결정한다. 성별·언어·사용자 UUID는 현재 점수의 가중치가 아니다.
- 카드는 점수와 독립적으로 추첨한다. 일반은 78장 중 서로 다른 5장과 정·역방향, 최초 이용은 승인된 6세트 중 하나다.
- 프로필·최초 이용일·당일 결과·광고 세션은 기존 DB 모델을 사용한다. 문구 변경만으로 DDL·백필·선택 상태 초기화를 하지 않는다.
- 결과에는 세 언어 문구를 함께 저장한다. 문구 자산만 배포해도 이미 발급된 유효한 당일 결과와 해금은 보존한다.
- 실제 프로필 revision 변경은 재계산 대상이다. 이전 점수/추첨 정책의 저장본도 현재 정책으로 전환하지만 당일 공개 권한과 최초 이용 이력을 보존한다.
- 내부 카드·방향·해석 근거는 응답에 내보내지 않는다.

계산: [점수](../app/services/fortune_scores.py), [추첨](../app/services/fortune_tarot.py). 생성·저장·권한: [서비스](../app/services/fortune.py).

## 원고와 생성 자산

| 위치 | 역할 |
| --- | --- |
| [content/fortune/ko-plain-voice](../content/fortune/ko-plain-voice) | 현재 한국어 원고·작성자/독립 검수·생성된 검토 자료 |
| [content/fortune/ko-rewrite](../content/fortune/ko-rewrite) | EN/JA 번역의 한국어 기준본·카드 의미·검수 증빙. 현재 한국어 교정 후에도 비교 기준으로 보존 |
| [content/fortune/localization](../content/fortune/localization) | 영어·일본어 정본·작성자/독립 검수·교차 검증 |
| [content/fortune/review.json](../content/fortune/review.json), [briefs.json](../content/fortune/briefs.json) | 이전 문구의 검수·기준 기록. 구버전 검수 회귀 보호를 위해 보존 |
| [app/resources/fortune](../app/resources/fortune) | 실행 JSON·규칙·manifest. 검수된 원고에서 생성 |
| [종합 전문](../content/fortune/overall.md), [애정](../content/fortune/category-love.md), [금전](../content/fortune/category-money.md), [일·학업](../content/fortune/category-work.md), [건강](../content/fortune/category-energy.md), [첫 방문](../content/fortune/first-visit.md) | 실행 자산에서 생성한 세 언어 대조표 |

각 언어는 5분야 × 78장 × 정·역방향 = 780개 묶음이다. EN/JA의 검수 기준은 `ko-rewrite`이며, 후속 한국어 교정본과 새로 등가 검수를 마쳤다는 뜻은 아니다. 검수 JSON의 hash는 기록된 원고와 검수의 일치를 확인하며 원어민 검수를 대신하지 않는다.

기존 검수 JSON의 `docs/fortune-content/...` 식별자는 당시 증빙의 고정 ID다. 실제 파일은 `content/fortune`에서 읽고 기록된 hash·승인 원고는 바꾸지 않는다.

### 편집 규칙

카드 의미와 분야를 유지하며 쉬운 일상어로 운과 행동을 직접 말한다. 글자 수를 채우기 위한 상황·이유, 내부 카드 정보, 미완성 문장과 강제 줄바꿈을 추가하지 않는다. 총운은 2문단, 애정은 3문단, 금전·건강은 1~2문단, 일·학업은 2문단을 유지한다. 문구를 바꾸면 해당 원고의 작성자·독립 검수와 hash 증빙을 갱신한 뒤 생성한다. 검수 실패를 기존 문구로 대체해 통과시키지 않는다.

## 빌드와 검증

저장소 루트에서 실행한다. 아래 검사는 외부 API·DB·모델 호출 없이 정본과 생성 결과를 비교한다.

```bash
uv run python content/fortune/ko-plain-voice/build_review.py --check
uv run python content/fortune/localization/validate.py --check
uv run python scripts/build_fortune_localized_copy.py --check
uv run python scripts/build_fortune_copy_docs.py --check
uv run python scripts/check_fortune_tarot_editorial.py --assets-only
uv run pytest -q tests/test_fortune_localized_build.py tests/test_fortune_tarot.py tests/test_fortune_service.py tests/test_fortune_workflows.py
```

원고·검수 수정 후 `build_review.py`, `validate.py`, 두 `build_fortune_*`를 `--check` 없이 실행하면 각각의 생성 파일을 갱신한다. 실행 자산은 원고를 검증한 뒤 한 번에 교체하며 manifest를 마지막에 기록한다. CI의 전문 일치·검수 hash·서비스 계약 검사는 유지한다.

## 호환과 롤백

- 새 문구는 새 결과부터 적용한다. 점수·카드 추첨·무료/구독/광고 권한을 문구 배포에 섞어 바꾸지 않는다.
- v8 저장본이 존재하는 환경은 `flow` 2~3개·분야 1~3개를 읽는 서버로만 롤백한다. 과거의 정확히 3개/2개만 받는 reader는 사용할 수 없다.
- 새 문단 수를 도입할 때는 reader 호환을 먼저 배포한 뒤 writer를 전환한다. 저장된 본문을 읽기 때문에 롤백 시 원고를 재작성하지 않는다.
- 모바일에서 실제 보이는 문단 간격은 클라이언트 구현의 검증 대상이다. 서버 배열 계약 통과만으로 스토어 앱 표시까지 확인했다고 하지 않는다.
