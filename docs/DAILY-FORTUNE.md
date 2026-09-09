# 오늘의 운세

> 기준일: 2026-09-09
>
> 문서 상태: 독립 점수·3개 언어 문단 재설계 구현 및 개발 DB 검증 완료. PR·서버 배포 전
>
> 공개 계약: `openapi/paths/fortune.yaml`, `openapi/components/fortune.yaml`
>
> 계산·문구 원본: `app/services/fortune_scores.py`, `app/resources/fortune/copy.v2.json`, `copy.v2.en.json`,
> `copy.v2.ja.json`

이 문서는 오늘의 운세 기능의 제품 규칙, 계산 방식, 문구, DB와 API를 한곳에 정리한 기준 문서다. 별도의
버전별로 기준 문서를 새로 만들지 않는다. 본문은 실행·검수 계약, 부록 A·B의 연결 문서는 전체 본문 전문이다.
클라이언트에 아직 반영하지 않은 UI 제안은 부록 D·E에서 구분한다.

파일은 다음 역할로 관리한다. 문구 버전은 자산의 `copy_version`에 기록하고 문서 폴더명에는 붙이지 않는다.

| 위치 | 역할 | 수정 방법 |
| --- | --- | --- |
| `docs/DAILY-FORTUNE.md` | 제품·API·DB·배포 및 검증 기준 | 기준 변경 시 이 문서에 반영 |
| `app/resources/fortune/` | 실행용 세 언어 문구·계산 규칙·manifest | 문구 원본을 수정하고 hash 갱신 |
| `docs/fortune-content/briefs.json` | 집필·현지화 기준 | 편집 기준이 바뀔 때 수정 |
| `docs/fortune-content/overall-*.md`, `category-*.md` | 전체 문구의 세 언어 대조표 14개 | 서버 자산 수정 후 생성기로 갱신 |
| `docs/fortune-content/review-notes.md` | 언어별 검수 기록 | 검수 결과를 한 파일에 기록 |
| `scripts/build_fortune_copy_docs.py` | 전문 생성 및 CI 일치 검사 | `--check`로 정본과 문서 일치 확인 |

집필용 중간 원고·변환 스크립트·일회성 로그는 실행 자산이나 정본 문서로 사용하지 않는다.

- [검수 기준·적용 계획](#4-문구와-표시-기준--2026-09-09)
- [종합 운세 전문](#부록-a-종합-운세-전문--10점수-구간--20묶음--3언어) · [분야별 전문](#부록-b-분야별-운세-전문--40경로--20변형--3언어)
- [행운색](#부록-c-행운색-전문--12색--3언어) · [기존 UI·배너](#부록-d-기존-ui와-서버-운세-배너-전문) · [누락 상태 UI](#부록-e-누락-상태접근성-ui-제안-전문)

## 1. 사용자가 받는 것

사용자는 최초 진입 때 생년월일과 성별을 입력한다. 출생 시각과 출생지는 받지 않는다. 오늘 결과에는 다음
내용이 함께 온다.

| 영역 | 내용 |
|---|---|
| 종합 | 0~100점, 오늘의 총평 1문장 |
| 오늘의 흐름 | 별도 일상 판단을 풀어 쓴 약 5문장(전송은 3구간) |
| 행동 | 오늘 해볼 것 1개, 오늘 조심할 것 1개 |
| 분야 | 애정·금전·일/학업·활력 점수와 분야별 약 5문장(전송은 2구간) |
| 행운색 | 사전 정의된 12색 중 1개 |

총평과 흐름은 돈·연애 같은 한 분야를 대표하지 않는다. 하루 전체의 결론을 총평으로 먼저 말하고, 흐름은
같은 결론을 더 쉽게 설명한다. 분야별 운세는 종합과 별도로 계산한다.

사용자 화면에는 행성, 각, 오브, 트랜짓, 내부 의미 코드와 계산 근거 카드를 노출하지 않는다.

종합 점수·총평, 해볼 것·조심할 것, 행운색은 광고 없이 제공한다. “자세히 보기”의 오늘의 흐름과
네 분야 점수·문구는 무료·체험(런칭 무료 기간 포함) 사용자의 광고 SSV 검증 후 제공한다.
월간·연간 구독자만 상세도 광고 없이 제공한다. 다른 체험 혜택은 유지한다.

## 2. 고정된 제품 규칙

- 필수 입력은 `birth_date`, `gender` 두 개다.
- 생년월일은 1900-01-01 이상이고 요청 시점의 사용자 현지 날짜 기준 만 14세 이상이어야 한다.
- 저장된 입력은 조회·수정·삭제할 수 있다.
- 같은 값을 다시 저장하면 revision이나 당일 결과가 바뀌지 않는다.
- 생년월일 또는 성별이 실제로 바뀌면 revision을 올리고 다음 공개에서 새 결과를 계산한다.
- 프로필을 수정해도 이미 광고나 구독으로 얻은 오늘의 공개 권한은 유지한다.
- 이름은 운세 입력이 아니며 화면 표시용이다.
- 성별은 제품 필수 프로필로 저장하지만 현재 계산에 임의 가중치를 주지 않는다.
- LLM, 런타임 난수, 사용자 UUID, 요청 시각으로 결과나 문구를 바꾸지 않는다.
- 점수·유형은 기존 날짜·프로필·계산 버전으로 결정한다. 표현은 해당 경로의 과거 선택 위치도 사용한다.
- 같은 당일 snapshot은 그대로 반환한다. 서로 다른 사용자가 같은 점수를 받아도 이전 경로 사용 이력에 따라 문구는 다를 수 있다.
- 처음 공개한 결과와 한국어·미국 영어·일본어 문구를 한 행에 저장해 그날 동안 그대로 반환한다.
- 배포 중 문구 파일이 바뀌어도 이미 공개한 오늘 결과는 바꾸지 않는다.
- 사용자별 전문 계산 로그를 별도 테이블에 계속 쌓지 않는다.

## 3. 독립 점수가 정해지는 순서

현재 규칙은 `fortune-independent.v1`이다. 점수는 천체 신호 강도나 사건의 발생 확률이 아닌 재현 가능한 콘텐츠용 운세 점수다. 기존 천체 모듈/규칙은 이전 계약 검증을 위해 남아 있지만 신규 결과의 점수를 계산하지 않는다. 결과 metadata의 ephemeris는 `not-used.independent-v1`이다.

사용자 현지 날짜를 기존 시간대 정책으로 정한 후, 규칙 버전·ISO 생일·현지 날짜·축 이름을 SHA-256에 넣는다. 총점·애정·금전·일/학업·활력마다 도메인이 달라 다른 값을 얻는다. 총점은 분야 평균이 아니며 분야 계산에 총점을 더하지 않는다. 점수 간 간격을 만들기 위한 재추첨/정렬도 없다.

해시를 정수 역누적분포로 0~100에 대응한다. 점수대별 확률은 0~9부터 90~100까지 1/2/4/7/12/18/22/18/11/5%다. 신호가 없어서 50점을 반환하는 기본값은 없다. 같은 생일·현지 날짜는 같은 점수이고 성별·언어·광고·열람 이력·문구 편집은 점수에 영향을 주지 않는다.

내부 semantic schema는 4, 외부 응답 schema는 기존 3을 유지한다. 내부 overall 표현 경로는 `overall.dXX.general`, 분야는 `category.<axis>.dXX.general`이다. 이전 semantic 3의 당일 snapshot도 그대로 읽는다. 행운색은 총점 구간에 대응하는 기존 12색 중 하나이며 다른 네 분야 점수는 변경하지 않는다.

검증은 `scripts/check_fortune_score_distribution.py`로 재현한다. 합성 생일 200개를 2026/2028/2031 각 날짜에 적용해 Pearson/Spearman, 50점 비율, 분야 분포, 총점 조건부 분야 평균, 분야 간 편차를 확인한다. 실제 사용자의 출생일은 수집하지 않는다. 구 점수 회귀 테스트 통과를 새 점수 경험의 증거로 사용하지 않는다.

### 3.1 문구 선택과 기존 이력

언어별 총평 10구간 ×20묶음, 4분야 ×10구간 ×20개를 유지한다. 총평·상세·해볼 것·조심할 것은 같은 variant를 사용하며 분야는 각자의 점수대에서 선택한다. 선택 cursor는 점수 생성에 사용하지 않는다.

선택 상태 `fortune-selection.v3`은 50개 경로의 bounded cursor를 유지한다. 같은 날짜에는 같은 위치, 다음 날짜 해당 경로를 사용할 때 다음 문구로 이동한다. 기존 v1/v2 cursor는 형식·날짜·선택 ID 일치를 검증한 후 새 문구 의미에 맞춰 전체 경로를 초기화한다. 알 수 없는 버전이나 깨진 상태는 조용히 초기화하지 않는다. 이미 생성된 당일 결과를 덮어쓰지는 않는다.

## 4. 문구와 표시 기준 — 2026-09-09

문구 버전은 `fortune-copy.v3-independent.1`이다. 문장 끝 마침표를 사용하고 본문은 한 문단으로 읽힌다. 한국어/영어는 구간 사이에 공백 하나, 일본어는 추가 공백 없이 연결한다. wire 배열 길이인 flow=3/text=2는 문장 수가 아니라 전송 구간 수다. 각 본문은 약 5문장이며 기기별 고정 5줄을 강제하거나 글을 잘라내지 않는다.

총평은 하루의 태도·선택·우선순위를 다루며 모든 분야가 좋거나 나쁘다고 대신 단정하지 않는다. 분야 문구는 해당 분야 점수만 따른다. 문단 안에는 서로 다른 관련 관찰·행동을 담을 수 있으며 하나의 사례를 다섯 문장으로 늘이는 규칙은 없다.

한국어는 친근한 반말, 명확한 판단과 구체적인 제안을 사용한다. ~겠어, 흐름이야 및 반복적인 추상적 격려를 피한다. 일본어는 주어·조사·종결의 자연스러움, 영어는 문장 리듬·번역투·상투적인 조언을 별도로 검토한다. 상대의 마음·수익·의학적 결과·사용자 직업을 사실처럼 단정하지 않는다.

기존 각 ID의 의미를 출발점으로 문장을 교정하고 같은 분야/점수대의 보조 관찰을 함께 편집했다. 모든 문장이 독립적으로 신규 집필됐거나 원어민이 전량 검수했다고 주장하지 않는다. 전체 문단 중복, 문장 수·부호, 반복 표현·의미 충돌을 점검하며 자동 검사는 사람이 읽을 후보를 찾는 보조 수단이다.

### 4.1 문구와 검수 기록

전체 전문은 부록 A/B의 생성 문서에서 확인한다. 생성기는 서버 자산을 한 문단으로 합쳐 3개 언어를 대조한다. 과거 검수 기록은 과거 버전의 증거이며 최신 검증 완료를 뜻하지 않는다.

- [한국어 검수](fortune-content/ko-independent-review.md)
- [일본어 검수](fortune-content/ja-independent-review.md)
- [영어 검수](fortune-content/en-independent-review.md)
- [구조·언어 작업 계획](FORTUNE-REDESIGN-PLAN.md)

### 4.2 앱 연결

`FortuneResultPanel`은 결과 locale로 구간 연결 방식을 정한다. 기존 앱의 배열 역직렬화는 유지되지만 이전 앱은 구간 사이 빈 줄을 표시할 수 있다. 새 단락 표시를 위해 앱 변경도 함께 반영한다. 광고 잠금/권한, 프로필 변경, 날짜 전환, 운세 대화 연결의 기존 계약은 유지한다.

## 5. API 상태 흐름

### 5.1 엔드포인트

| 메서드·경로 | 용도 |
|---|---|
| `GET /fortune-profile` | 저장된 생년월일·성별·revision 조회 |
| `PUT /fortune-profile` | 최초 저장 또는 수정 |
| `DELETE /fortune-profile` | 운세 프로필과 하위 당일 데이터 삭제 |
| `GET /daily-fortune/status` | 홈 진입 상태, 이미 공개된 결과 조회 |
| `POST /daily-fortune/reveal` | 오늘 기본 결과 계산·공개 및 상세 권한 확인 |
| `POST /daily-fortune/ad-sessions` | 무료·체험 사용자의 자세히 보기용 AdMob SSV 세션 발급 |
| `GET /webhooks/ad-ssv` | Google 서명 검증 후 당일 상세 즉시 해제 |

`X-App-Locale`은 `ko/en/ja`와 해당 언어의 지역 태그(`ko-KR/en-US/ja-JP` 등)를 받는다. 지역 태그는
대소문자와 관계없이 기본 언어로 정규화한다. `jp`와 지원하지 않는 언어는 422로 거절한다. 헤더가 없으면
계정 언어, `ko` 순서로 선택하며 응답의 `locale`에는 실제 반환한 기본 언어 코드가 들어간다. 과거
한국어-only snapshot에서 요청 언어가 없을 때만 `ko`로 안전하게 폴백한다.

### 5.2 상태 머신

```text
profile_required ── PUT profile ──> unseen
unseen ── POST reveal ──> revealed       (월간·연간 구독)
unseen ── POST reveal ──> locked         (무료·체험: 기본 결과 공개, 상세 잠금)
locked ── ad session + verified SSV ──> revealed
                                 └──> unseen + unlocked_today  (광고 중 프로필 변경 시)
revealed ── 같은 날 재조회 ──> 같은 snapshot
revealed ── 프로필 수정 ──> unseen       (unlock 권한은 유지)
```

`state`와 `access`는 서로 다른 값이다.

- `state`: `profile_required | unseen | locked | revealed`
- `access`: `included | ad_required | unlocked_today`
- `available=false`: 기능 플래그 또는 승인 상태 문제이므로 운세 탭을 비활성화한다.
- 기능이 꺼져 있으면 프로필 GET·PUT·DELETE도 `FEATURE_UNAVAILABLE`로 DB 접근 전에 종료한다.
- `locked`는 상세 잠금이다. `result`는 `FortuneBasicResult`로 종합 점수·총평·행동·행운색만
  포함하고 `versions`도 반환한다. `overall.flow`와 `categories`는 빈 값 대신 필드 자체를 생략한다.
- `revealed`는 상세까지 공개된 상태이며 `FortuneResult` 전체를 반환한다.
- 정책 변경 전에 체험 혜택으로 얻은 당일 해금도 유지하고 다음 현지 날짜부터 광고를 요구한다.
- `status`는 읽기 전용이다. 현재 snapshot이 없으면 `unseen`을 반환하고 앱이 `reveal`을 호출한다.
  snapshot이 있으면 광고 전에도 기본 결과를 다시 받을 수 있다.
- 광고 SSV가 성공하면 서버가 DB의 당일 행을 직접 해제한다. 이후 status를 다시 조회한다.
- 광고를 보는 사이 프로필이 바뀌어 `unseen + unlocked_today`가 오면 광고를 다시 요구하지 말고
  `POST /daily-fortune/reveal`을 호출해 새 결과를 만든다.
- 프로필 PUT의 `unlock_preserved`는 **이번 실제 변경에서 오늘 권한을 보존했는지**를 뜻한다.
  같은 값 PUT에서는 false이며 현재 access 판정 대신 status를 다시 조회한다.

운세 결과를 대화에 붙이는 기능은 `FORTUNE_CHAT_ENABLED`로 별도 제어한다. 상세가 해금된 당일 결과를
`context_ref={type:daily_fortune, local_date, locale}`로 참조하며, 답변 저장 직전에 날짜·프로필 revision·결과
fingerprint를 다시 검사한다. 서버가 붙이는 운세 표제도 `ko/en/ja`에 맞춰 바뀐다. 운세에서 파생된 대화는 장기
기억·일기·관계 데이터의 근거로 사용하지 않는다.

모바일 연동 시 `locked.result`도 기본 결과 화면에 표시하고 상세 버튼에서 광고를 시작한다.
기존 앱의 전체 잠금 분기와 필수 상세 필드 파싱은 계약 동기화 및 수정이 필요하다. 광고 완료 SDK
콜백만으로 상세를 펼치지 않으며, SSV 후 `status`의 `revealed`와 전체 결과를 확인한다.
`locked + included`는 구독 전환 등으로 광고가 불필요해진 상태이므로 `reveal`로 상세를 공개한다.
외부 응답 및 row 버전은 `3`, 내부 semantic은 `4`이며, 기존 당일 snapshot과 해금은 유지하므로 DB migration은 없다.

### 5.3 공개 응답 예시

2002-12-13생, 2026-09-09, Asia/Seoul에 **이전 선택 이력 없이 처음 생성한** 결과다.
이번 브랜치의 계산·선택·공개 변환 코드에서 생성했다. 생년월일·날짜가 같더라도 이전 경로별 선택 이력이
있으면 점수는 같고 문구 ID는 달라질 수 있다. 성별과 표시 이름은 현재 계산에 사용하지 않는다.

```json
{
  "state": "revealed",
  "access": "included",
  "local_date": "2026-09-09",
  "result": {
    "schema_version": 3,
    "locale": "ko",
    "overall": {
      "score": 42,
      "headline": "오늘은 짧은 쉼 뒤에 일상이 다시 반갑게 느껴져.",
      "do": "반복되는 일 사이에 쉬는 시간을 넣어봐.",
      "pause": "흥미가 줄었다고 바로 싫어진 것으로 여기지 마.",
      "flow": [
        "늘 하던 일도 계속 이어지면 조금 지루해질 수 있어.",
        "잠깐 멀어졌다 돌아오면 처음 좋아했던 이유가 떠오를 수 있어. 의욕을 만들기보다 쉴 간격을 주는 편이 잘 맞아.",
        "오늘 한 가지는 취향대로 정해두는 게 좋아. 호의에 같은 크기의 답을 기대하지 마."
      ]
    },
    "lucky_color": {
      "key": "green",
      "name": "초록",
      "hex": "#43A047"
    },
    "categories": {
      "love": {
        "score": 69,
        "text": [
          "바라는 배려를 말로 전하기에 괜찮은 때야. 알아주길 기다리기보다 어떤 점이 좋을지 제안해봐.",
          "소소한 취향이 서로를 알아가는 좋은 계기가 돼. 좋아하는 것 하나를 소개하고 상대의 추천도 들어보는 편이 나아. 다른 취향을 발견하면 새로운 것을 배운다는 마음으로 들어봐."
        ]
      },
      "money": {
        "score": 82,
        "text": [
          "미리 비교한 조건을 바탕으로 마음에 드는 물건을 고르기 좋은 날이야. 마지막으로 총액을 확인하고 마음에 든 것을 골라봐.",
          "큰돈을 쓰지 않아도 취향에 맞는 것을 고르면 만족하기 좋은 날이야. 비교 때문에 금액을 늘리기보다 가장 좋아하는 것을 고르는 편이 나아. 자주 이용할 경험이나 편리함에 예산을 두어봐."
        ]
      },
      "work": {
        "score": 48,
        "text": [
          "조금씩 고친 내용에서 변화가 드러날 수 있어. 처음 상태와 비교하며 나아진 부분을 찾아봐.",
          "잘 이해하는 방식을 알아보기 좋아. 읽기와 말하기 중 무엇이 더 잘 남는지 살펴보는 게 좋아. 무엇이 궁금한지 먼저 한 문장으로 적어봐."
        ]
      },
      "energy": {
        "score": 60,
        "text": [
          "해보고 싶던 활동을 시작하기 좋은 날이야. 준비를 크게 하기보다 짧게 맛보는 시간을 가져봐.",
          "가벼운 운동을 꾸준히 이어가기 좋은 날이야. 무리해서 양을 늘리기보다 오늘 편한 정도를 기억해두면 도움이 돼. 늘 하던 활동에 쉬운 동작이나 새 경로를 더해봐."
        ]
      }
    }
  },
  "versions": {
    "ephemeris": "not-used.independent-v1",
    "rules": "fortune-independent.v1",
    "copy": "fortune-copy.v3-independent.1"
  }
}
```

내부 `reading_code`, `expression_route`, `copy_selection`은 DB snapshot과 검증에만 쓰며 API로 내보내지 않는다.

## 6. DB 설계

문구 원본은 DB 조합 테이블이 아니라 버전·hash가 고정된 JSON 자산이다. DB에는 사용자 입력과 실제 공개한 당일
snapshot만 저장한다.

### `fortune_profiles`

| 컬럼 | 의미 |
|---|---|
| `user_id` PK/FK | 사용자당 운세 프로필 1개 |
| `gender` | `man | woman | undisclosed` |
| `birth_date` | 생년월일 |
| `revision` | 실제 입력 변경 때만 증가 |
| `created_at`, `updated_at` | 생성·수정 시각 |

### `daily_fortunes`

사용자당 한 행만 두고 날짜가 바뀌거나 프로필이 수정되면 같은 행을 새 snapshot으로 교체한다. 무기한 일별 이력을
쌓지 않는다.

| 컬럼 | 의미 |
|---|---|
| `user_id` PK/FK | 사용자당 현재 snapshot 1개 |
| `fortune_date`, `timezone_snapshot` | 어느 현지 날짜의 결과인지 판정 |
| `profile_revision` | 계산에 사용한 프로필 revision |
| `result_schema_version` | 현재 `3` |
| `semantic_result` | 점수·내부 의미 코드·표현 경로와 비공개 `copy_selection` 선택 상태 |
| `copy_by_locale` | 실제 공개한 `ko/en/ja` 문구 snapshot |
| `unlock_state/source/at` | 오늘 상세 공개 권한 |
| `revealed_at` | 상세 결과가 공개된 시각; 기본만 공개되었거나 권한만 있으면 NULL 가능 |
| `ephemeris/rule/copy_version` | 결과 재현용 버전 |

freshness는 `fortune_date + timezone_snapshot + profile_revision + schema_version`으로 판단한다. 공개 권한은 같은
`fortune_date`의 `unlock_state`로 별도 판단하므로 프로필 수정과 광고 콜백이 겹쳐도 권한을 잃지 않는다.

### `fortune_ad_sessions`

- `client_request_id`는 사용자별 unique라 재시도에 같은 세션을 반환한다.
- Google `ssv_transaction_id`는 전체 unique라 중복 보상을 막는다.
- 생성 후 최대 30분, 당일 자정 이전까지만 유효하다.
- 서명된 user ID, placement allowlist, reward item·amount, 소유자와 날짜를 모두 대조한다.
- 검증 성공 트랜잭션 안에서 광고 세션과 `daily_fortunes` unlock을 함께 커밋한다.
- 만료 세션 정리 인덱스는 `(expires_at, session_id)`다.
- 만료 후 7일이 지난 세션은 worker가 `FOR UPDATE SKIP LOCKED`로 한 번에 최대 500건씩 삭제한다.

세 테이블은 모두 RLS ON, `anon/authenticated` 권한 없음, 계정 삭제 시 FK CASCADE다.

## 7. 버전·배포·검증

### 독립 점수·문단의 배포 계약

- 신규 테이블/컬럼이나 기존 결과 일괄 backfill은 없다. 기존 JSON snapshot과 버전 컬럼을 사용한다.
- 기존 유효 당일 semantic 3/문구/광고 해제는 보존한다. 다음 날짜 또는 기존 프로필 무효화로 새 결과를 만들 때 semantic 4와 새 문구/선택 v3를 저장한다.
- 같은 날짜 프로필 변경으로 재계산해도 광고/구독 공개 권한을 유지한다. 과거 운세 대화의 snapshot을 새 결과로 덮어쓰지 않는다.
- 새 선택 v3를 구 writer는 해석하지 못한다. 운영 승격 시 혼합 writer가 동시에 요청을 받지 않도록 운세·운세대화를 양쪽 인스턴스에서 중지하고 새 이미지로 전환한 뒤 활성화하는 절차가 필요하다. 현재 infra는 `FORTUNE_ENABLED`/`FORTUNE_CHAT_ENABLED`를 true로 명시하므로 이 절차를 준비하지 않은 일반 rolling 배포로 운영에 승격하지 않는다.
- 새 결과가 기록된 뒤 구 writer로 무조건 롤백하지 않는다. semantic 3/4와 선택 v1/v2/v3를 이해하는 호환 수정 버전으로 복구한다. DB 결과나 광고 해제를 삭제해 회귀하지 않는다.
- 코드·3개 언어 JSON·manifest는 함께 배포한다. 개발에서 실제 DB 검증을 마친 뒤 최종 PR을 만들며 사용자가 이번에는 PR 금지를 지시했다.
- 이번 작업은 개발 검증 단계이며 운영 서버/DB/푸시를 변경하지 않는다. 전체 구조는 `db.verify`의 읽기 전용 검사로 확인한다.

### 7.0 이번 변경 검증 결과 (2026-09-09)

| 검사 | 결과 |
| --- | --- |
| 독립 점수 분포 | 2026/2028/2031 총 219,200개, 모든 합격 기준 통과 |
| 축 간 Pearson/Spearman 최대 절댓값 | 연도별 0.0071 / 0.0090 / 0.0050 |
| 분야 점수 차이 10 이하 | 2.09% / 1.95% / 2.05% |
| 분야 점수 차이 25 이상 | 80.55% / 80.58% / 80.49% |
| 서버 전체 테스트 | 2,205 통과, 환경 의존 114 skip |
| 개발 DB 통합 | 별도 8 통과, 임시 계정·데이터 제거 확인 |
| 클라이언트 | analyze 통과, 전체 4,005 테스트 통과 |
| 생성 문서 | 14개 전문 문서 자산 일치 검사 |

개발 DB 통합 검증에는 동시 요청의 단일 선택, 언어 전환, 날짜 변경, 실패 트랜잭션 복구, 개인정보 삭제 및 기존 schema 3 당일 결과 보존 후 다음 날 실제 schema 4 생성이 포함된다. 운영 DB는 변경하지 않았다. 테스트 실행 위치와 서버 배포를 구분한다. 개발 DB에 연결해 구현 코드를 검증했으며 실행 중인 개발 API에 새 코드를 배포한 상태는 아니다.

UI의 결과 본문은 구간 개수와 무관하게 한 문단으로 렌더링한다. 320/393 폭·글자 배율 1.3에서 세 언어 본문을 검사했다. 기존 운세 UI 키는 합산 점수를 주장하지 않으므로 이번에 변경하지 않았다. 실제 기기에서의 최종 가독성 확인은 남아 있다. 한국어 본문은 116~175자로 초기 편집 목표보다 짧지만 약 5문장을 유지했으며, 고정 5줄을 보장하지 않는다. 세 언어 통독 범위와 문장 소재 재사용은 각 편집 검수 기록에 공개했다.

이전 계획의 생일별·날짜별 소집단 상관 리포트는 별도 집계하지 않았다. 위 통계는 연도별 전체 표본 및 총점 구간별 분야 평균 검사 결과다. 단위 테스트·합성 표본 통과를 실제 사용자의 선호도 검증으로 해석하지 않는다.

### 7.1 AdMob 운영 계약

| 플랫폼 | 광고 단위 | 서버 allowlist 값 |
|---|---|---|
| iOS | `ca-app-pub-5805427935121417/3157498952` | `3157498952` |
| Android | `ca-app-pub-5805427935121417/2146352961` | `2146352961` |

- 두 광고 단위의 형식은 보상형, 보상 항목은 `fortune_unlock`, 수량은 `1`이다.
- SSV URL은 `https://voice.moly.asia/webhooks/ad-ssv`이며 두 광고 단위 모두 AdMob 콘솔의 URL 확인을 마쳤다.
- 운영 Parameter Store의 `/moly/prod/fortune-ad-unit-ids`에는 위 allowlist 값 두 개를 쉼표로 구분해 둔다.
- 앱은 광고 요청 전에 `POST /daily-fortune/ad-sessions`가 돌려준 `admob_user_id`와 `custom_data`를 그대로
  Google Mobile Ads SDK의 SSV 옵션에 넣는다. 임의 UUID나 고정 테스트 값을 실제 광고에 재사용하지 않는다.
- 서버는 Google 서명뿐 아니라 사용자·세션 소유자·광고 단위·보상 항목·수량·만료·당일 날짜를 모두 대조한다.
- SSV 서명은 Google `RewardedAdsVerifier`와 동일하게 signed query를 percent-decode 한 번 한
  UTF-8 바이트로 검사한다. `fortune%3A<UUID>`를 raw 바이트로 검사하면 정상 콜백도 422가 된다.
  파라미터도 같은 표현에서 읽으며, 추가 decode나 `+` 치환은 하지 않는다.
- AdMob 콘솔의 서명된 URL 확인 요청처럼 `transaction_id`가 없는 요청은 200 no-op으로 끝내고 운세를 열지 않는다.
  서명이 없거나 서명 뒤에 임의 필드를 붙인 요청은 422로 거절한다.

필수 검증:

1. 규칙·카탈로그·ephemeris 단위 테스트
2. 0/50/100점, 양수·음수 대칭, 10점 경계 테스트
3. 총평 10개 문구 경로·분야 40개 경로·12색 완전성 및 hash 테스트, 내부 계산 80경로의 총평 풀 매핑 검증
4. 언어별 새 총평 200묶음의 제목·완성 해석 중복, 같은 점수 내 해석 복제, 마침표·퇴역 말투 검사
5. 프로필 동일 PUT/변경 PUT과 unlock 보존 테스트
6. 광고 SSV 즉시 unlock·중복 transaction·만료·소유자 불일치 테스트
7. 광고 전 기본 공개·상세 누출 방지와 해금 후 전체 응답 Pydantic/OpenAPI 계약 테스트
8. 스키마 변경 때 CI 임시 DB에서 schema·seed 재생성 및 SQL 검증, 개발 DB 구조 확인, 배포 시 DB preflight와 인증 포함 HTTP smoke test
9. 출시 전 문구 전수 사람 검수, 점수 분포·경로 도달률 장기 시뮬레이션

오늘의 운세와 운세 대화 연결은 운영에서 활성화돼 있다. 운영 배포는 DB preflight, 두 인스턴스 롤링,
버전 일치와 synthetic health를 모두 통과해야 한다. 새 앱 빌드가 광고 연동을 시작할 때에는 iOS·Android에서
각각 실제 광고 1회를 사용해 `세션 발급 → SSV → 당일 unlock → status 재조회`를 출시 전 최종 확인한다.


## 부록 A. 종합 운세 전문 — 10점수 구간 × 20묶음 × 3언어

유형 분류 없이 하루 전체를 해석하는 새 원고다. 각 파일에 세 언어의 20묶음 전체를 담았다.
점수 100은 90–100점 구간을 사용한다. 제목·풀이·행동·주의를 같은 ID로 읽는다.

| 점수 구간 | 전체 전문 | 언어별 묶음 / 표현 |
| --- | --- | --- |
| 0–9 | [총평 0–9점](fortune-content/overall-d00.md) | 20 / 120 |
| 10–19 | [총평 10–19점](fortune-content/overall-d10.md) | 20 / 120 |
| 20–29 | [총평 20–29점](fortune-content/overall-d20.md) | 20 / 120 |
| 30–39 | [총평 30–39점](fortune-content/overall-d30.md) | 20 / 120 |
| 40–49 | [총평 40–49점](fortune-content/overall-d40.md) | 20 / 120 |
| 50–59 | [총평 50–59점](fortune-content/overall-d50.md) | 20 / 120 |
| 60–69 | [총평 60–69점](fortune-content/overall-d60.md) | 20 / 120 |
| 70–79 | [총평 70–79점](fortune-content/overall-d70.md) | 20 / 120 |
| 80–89 | [총평 80–89점](fortune-content/overall-d80.md) | 20 / 120 |
| 90–100 | [총평 90–100점](fortune-content/overall-d90.md) | 20 / 120 |

## 부록 B. 분야별 운세 전문 — 40경로 × 20변형 × 3언어

각 분야는 점수 구간별 20개의 완성된 약 5문장 문단(전송 2구간)을 갖는다. 종합과 분야 사이의 문장은 조립하지 않는다.

| 분야 | 전체 전문 | 언어별 묶음 / 표현 |
|---|---|---|
| 애정 | [love](fortune-content/category-love.md) | 200 / 400 |
| 금전 | [money](fortune-content/category-money.md) | 200 / 400 |
| 일·학업 | [work](fortune-content/category-work.md) | 200 / 400 |
| 활력 | [energy](fortune-content/category-energy.md) | 200 / 400 |

본문 수정 후 `uv run python scripts/build_fortune_copy_docs.py`로 전문을 다시 생성한다.
CI의 `--check`는 실행 자산과 문서가 다르면 실패한다. 생성 파일을 단독 수정하지 않는다.

## 부록 C. 행운색 전문 — 12색 × 3언어

**2026-09-08 합의: 코랄을 회색으로 교체한다.** 한국어 `회색`, 미국 영어 `Gray`, 일본어 `グレー`,
색상값은 `#9E9E9E`다. 색 이름과 화면의 색칩을 함께 바꾸며, 색상 수는 12개로 유지한다.

`coral`은 기존 선택표·저장 결과와 연결되는 내부 식별자이므로 유지한다. 사용자에게는 이 키가 아닌
현지화된 `name`을 표시하고 색칩은 `hex`로 그린다. 클라이언트에서 키를 보고 코랄 이름이나 색상을
하드코딩하지 않는다. 새 카탈로그 반영 후 생성되는 결과부터 회색을 사용하며, 이미 공개된 당일
snapshot의 색 이름·HEX는 기존 보존 정책에 따라 유지한다. 이번 변경은 문서에만 반영한 상태다.

| 키 | HEX | 한국어 | English (US) | 日本語 |
| --- | --- | --- | --- | --- |
| red | #E53935 | 빨강 | Red | 赤 |
| coral | #9E9E9E | 회색 | Gray | グレー |
| orange | #FB8C00 | 주황 | Orange | オレンジ |
| yellow | #FDD835 | 노랑 | Yellow | 黄色 |
| green | #43A047 | 초록 | Green | 緑 |
| sky | #4FC3F7 | 하늘색 | Sky blue | 水色 |
| blue | #1E88E5 | 파랑 | Blue | 青 |
| navy | #3949AB | 남색 | Navy | ネイビー |
| purple | #8E44AD | 보라 | Purple | 紫 |
| pink | #EC6F91 | 분홍 | Pink | ピンク |
| white | #FFFFFF | 흰색 | White | 白 |
| beige | #D8C3A5 | 베이지 | Beige | ベージュ |

## 부록 D. 기존 UI와 서버 운세 배너 전문

### D.1 기존 ARB 33키

원본은 `becappy-mobile/lib/l10n/app_{ko,en,ja}.arb`. `{date}`·`{score}`는 보존한다.
`fortuneResultGreeting`과 `fortuneSample*`의 문구는 프리뷰용이다. 서비스 결과는 4.6의 API 필드로 교체한다. 총평 프리뷰는 세 언어 모두 `overall.d70.general/v01`로 맞춰두었으며, 분야 프리뷰는 `category.love.d70.general/v01`이다. 한 샘플 분야 본문을 모든 탭에 재사용하지 않는다.

| 키 | 용도 | 한국어 | English (US) | 日本語 |
| --- | --- | --- | --- | --- |
| `homeBannerSampleFortuneLabel` | 클라이언트 배너 프리뷰 | 오늘의 운세 | Today's fortune | 今日の運勢 |
| `homeBannerSampleFortuneHeadline` | 클라이언트 배너 프리뷰 | {date},<br>오늘은 어떤 하루일까? | What might {date} bring?<br>Let's take a look. | {date}の運勢は？<br>キャピーと見てみよう |
| `homeBannerSampleFortuneCta` | 클라이언트 배너 프리뷰 | 운세 보기 | See my fortune | 運勢を見る |
| `fortuneTitle` | 고정 UI | 오늘의 운세 | Today's fortune | 今日の運勢 |
| `fortuneBack` | 고정 UI | 뒤로 | Back | 戻る |
| `fortuneClose` | 고정 UI | 닫기 | Close | 閉じる |
| `fortuneGreeting` | 고정 UI | 어서 와! 오늘의 운세를 봐줄게.<br>먼저 두 가지만 알려줘. | Curious about your day?<br>I'll ask you two quick questions. | 今日の運勢を見に来たんだね。<br>占う前に、二つ教えてね。 |
| `fortuneGenderQuestion` | 고정 UI | 성별을 선택해 줘. | First, select your gender. | 性別を選んでね。 |
| `fortuneGenderMale` | 고정 UI | 남성 | Male | 男性 |
| `fortuneGenderFemale` | 고정 UI | 여성 | Female | 女性 |
| `fortuneBirthdayQuestion` | 고정 UI | 생년월일을 알려줘. | What's your date of birth? | 生年月日を教えてね。 |
| `fortuneBirthdayDone` | 고정 UI | 완료 | Continue | 決定 |
| `fortuneIdlePrompt` | 고정 UI | 오늘은 어떤 하루일까?<br>수정구슬로 살펴볼게. | Ready for today's reading?<br>Let's look into the crystal ball. | 今日はどんな一日になるかな？<br>水晶玉で見てみよう。 |
| `fortuneReadCta` | 고정 UI | 오늘의 운세 보기 | See my fortune | 今日の運勢を見る |
| `fortuneReadingCaption` | 고정 UI | 오늘의 운세를 살펴보고 있어. | Reading your fortune… | 占っています… |
| `fortuneResultGreeting` | 프리뷰 → API 결과 | 망설이던 일에 한 걸음 다가가기 좋은 날이야 | Today is a good day to move toward something you've hesitated over | ためらっていたことへ、一歩近づくのに良い日 |
| `fortuneResultDate` | 고정 UI | {date} | {date} | {date} |
| `fortuneScore` | 고정 UI | {score} | {score} | {score} |
| `fortuneLuckScoreLabel` | 고정 UI | 운세 점수 | Fortune score | 運勢スコア |
| `fortuneLuckColorLabel` | 고정 UI | 행운색 | Lucky color | ラッキーカラー |
| `fortuneSampleLuckColor` | 프리뷰 → API 결과 | 노랑 | Yellow | 黄色 |
| `fortuneTryLabel` | 고정 UI | 해볼 것 | Try this | やってみること |
| `fortuneSampleTryBody` | 프리뷰 → API 결과 | 해보고 싶던 일에 첫발을 내디뎌봐 | Take a first step toward something you've wanted to try | 試したかったことへ、最初の一歩を踏み出してみて |
| `fortuneAvoidLabel` | 고정 UI | 조심할 것 | Watch out for | 控えたいこと |
| `fortuneSampleAvoidBody` | 프리뷰 → API 결과 | 준비가 완벽해야만 시작할 수 있다고 생각하지 마 | Don't make perfect preparation a condition for beginning | 準備が完璧でなければ始められない、と考えないで |
| `fortuneDetailTitle` | 고정 UI | 자세한 운세 | Your full reading | 詳しい運勢 |
| `fortuneDetailUnlockCta` | 고정 UI | 광고 보고 자세한 운세 보기 | Watch an ad for the full reading | 広告を見て詳しい運勢を読む |
| `fortuneSampleDetailBody` | 프리뷰 → API 결과 | 처음 해보는 부담보다 한번 시도하고 싶은 마음이 앞설 수 있어<br>완벽한 때를 기다리기보다 지금 가능한 시도가 기회를 열어줘<br>작게 시작해도 오늘은 그다음이 보일 수 있어 | The sense that you can try may grow stronger than the pressure of that first move<br>An attempt you can make now opens more room than waiting for perfect timing<br>Even a small beginning can help you see what comes next | 初めての緊張より、一度やってみたい気持ちが大きくなるかも<br>完璧なときを待つより、今できる挑戦が機会につながりそう<br>小さく始めても、今日はその先が見えてくるかも |
| `fortuneCategoryLove` | 고정 UI | 애정 | Love | 恋愛運 |
| `fortuneCategoryMoney` | 고정 UI | 금전 | Money | 金運 |
| `fortuneCategoryStudy` | 고정 UI | 일·학업 | Work & study | 仕事・学業 |
| `fortuneCategoryHealth` | 고정 UI | 활력 | Energy | 活力 |
| `fortuneSampleCategoryBody` | 프리뷰 → API 결과 | 꾸미지 않은 말에 마음이 더 가까워질 수 있어<br>잘 보이려 애쓰기보다 함께 있어 즐거운 순간을 표현해봐 | Unpolished, honest words can bring you closer<br>Express the moments you enjoy together instead of trying hard to impress | 飾らない言葉で、心の距離が近づきそう<br>よく見せようとするより、一緒にいて楽しい気持ちを表してみて |

### D.2 공유 접근성 문구

`homeBannerBlindCord`는 홈 배너 공통 키로 운세 카드에서도 사용된다. 다른 종류의 카드에서도 맞는 문구로 유지한다.

| 키 | 한국어 | English (US) | 日本語 |
| --- | --- | --- | --- |
| `homeBannerBlindCord` | 줄을 당겨 다음 카드 보기 | Pull the cord to see the next card | ひもを引いて次のカードを見る |

### D.3 실제 서버 배너

출처: `app/resources/banners/home_blind.json`의 `banners[id=composed-image-test].canvases_by_locale.{locale}.elements`. 운세로 이동하는 배너만 검수한다. 날짜 바인딩은 `{day}`이며 ARB의 `{date}`와 혼용하지 않는다. `primary-action`의 `open_fortune` 동작은 그대로다.

| 요소·필드 | 한국어 | English (US) | 日本語 |
| --- | --- | --- | --- |
| `heading.text.value` | 오늘의 운세 | Today's fortune | 今日の運勢 |
| `date.text.value` | {day} | {day} | {day} |
| `message.text.value` | 오늘은 어떤 하루가 될까? | What might today bring? | 今日はどんな一日になるかな？ |
| `action-label.text.value` | 운세 보기 | See my fortune | 運勢を見る |
| `primary-action.accessibility_label` | 오늘의 운세 보기 | See today's fortune | 今日の運勢を見る |

## 부록 E. 누락 상태·접근성 UI 제안 전문

**아직 구현되지 않은 제안 키다.** 서버 메시지를 복사해서 표시하는 대신 오류 코드와 현재 화면 상태로 매핑한다. 미래 문구가 존재한다는 이유만으로 해당 기능이 구현됐다고 해석하지 않는다.

| 제안 키 | 한국어 | English (US) | 日本語 |
| --- | --- | --- | --- |
| `fortuneOverallLabel` | 종합 운세 | Overall outlook | 総合運 |
| `fortuneFlowLabel` | 오늘의 흐름 | How today may unfold | 今日の流れ |
| `fortuneProfileTitle` | 운세 정보 | Reading profile | 占いの基本情報 |
| `fortuneProfileEdit` | 정보 수정 | Edit profile | 情報を変更 |
| `fortuneProfileSave` | 저장 | Save | 保存 |
| `fortuneProfileSaved` | 저장했어. 바뀐 정보로 오늘의 운세를 다시 볼 수 있어. | Saved. You can get a new reading with your updated details. | 保存したよ。変更した情報で、今日の運勢をもう一度占えるよ。 |
| `fortuneProfileDelete` | 운세 정보 삭제 | Delete reading profile | 占いの情報を削除 |
| `fortuneProfileDeleteConfirm` | 생년월일과 성별, 저장된 운세가 삭제돼. 계속할까? | This deletes your birth date, gender, and saved reading. Continue? | 生年月日・性別と保存された運勢が削除されるよ。続ける？ |
| `fortuneProfileDeleteAction` | 삭제 | Delete | 削除 |
| `fortuneCancel` | 취소 | Cancel | キャンセル |
| `fortuneProfileRequired` | 생년월일과 성별을 입력하면 운세를 볼 수 있어. | Enter your date of birth and gender to get your reading. | 生年月日と性別を入力すると、運勢を見られるよ。 |
| `fortuneBirthDateInvalid` | 생년월일을 다시 확인해 줘. 1900년생부터 입력할 수 있고, 만 14세 이상이어야 해. | Check your birth date. You must be at least 14 and born in 1900 or later. | 生年月日を確認してね。1900年以降に生まれた満14歳以上の人が利用できるよ。 |
| `fortuneSaveFailed` | 정보를 저장하지 못했어. 다시 시도해 줘. | Your details couldn’t be saved. Try again. | 情報を保存できなかったよ。もう一度試してね。 |
| `fortuneDeleteFailed` | 정보를 삭제하지 못했어. 다시 시도해 줘. | Your details couldn’t be deleted. Try again. | 情報を削除できなかったよ。もう一度試してね。 |
| `fortuneLoadFailed` | 운세를 불러오지 못했어. 다시 시도해 줘. | Your reading couldn’t be loaded. Try again. | 運勢を読み込めなかったよ。もう一度試してね。 |
| `fortuneRetry` | 다시 시도 | Try again | 再試行 |
| `fortuneUnavailable` | 지금은 운세를 볼 수 없어. 잠시 후 다시 와 줘. | Readings are unavailable right now. Please try again later. | 今は占いを利用できないよ。しばらくしてからまた来てね。 |
| `fortuneAdPreparing` | 광고를 준비하고 있어. | Loading ad… | 広告を読み込み中… |
| `fortuneAdUnavailable` | 지금 볼 수 있는 광고가 없어. 잠시 후 다시 시도해 줘. | No ad is available right now. Try again later. | 今は視聴できる広告がないよ。しばらくしてから試してね。 |
| `fortuneAdIncomplete` | 광고를 끝까지 보면 자세한 운세를 볼 수 있어. | Watch the full ad to see your full reading. | 広告を最後まで見ると、詳しい運勢を見られるよ。 |
| `fortuneAdVerifying` | 광고 시청을 확인하고 있어. 잠시만 기다려 줘. | Confirming that you finished the ad… | 広告の視聴を確認しているよ。少し待ってね。 |
| `fortuneAdPending` | 시청 확인이 늦어지고 있어. 광고를 다시 보지 말고 시청 상태를 확인해 줘. | Ad verification is taking longer than usual. Check again before watching another ad. | 視聴の確認に時間がかかっているよ。広告をもう一度見る前に、視聴状況を確認してね。 |
| `fortuneCheckUnlock` | 시청 상태 확인 | Check ad status | 視聴状況を再確認 |
| `fortuneDetailOpenCta` | 자세한 운세 보기 | See the full reading | 詳しい運勢を見る |
| `fortuneDateChanged` | 날짜가 바뀌었어. 오늘의 운세를 새로 볼까? | It’s a new day. Ready for today’s reading? | 日付が変わったよ。新しい運勢を見る？ |
| `fortuneContextStale` | 운세가 바뀌었어. 결과를 다시 확인한 뒤 이야기해 줘. | Your reading has changed. Open the updated result before chatting about it. | 運勢が更新されたよ。新しい結果を確認してから話しかけてね。 |
| `fortuneChatCta` | 캐피와 운세 이야기하기 | Talk about your reading with Cappy | キャピーと運勢の話をする |
| `fortuneChatUnavailable` | 지금은 운세에 관해 이야기할 수 없어. 잠시 후 다시 시도해 줘. | Chat about your reading is unavailable right now. Please try again later. | 今は運勢について話せないよ。しばらくしてからまた試してね。 |
| `fortuneScoreSemantics` | {label}, 100점 만점에 {score}점 | {label}, {score} out of 100 | {label}、100点中{score}点 |
| `fortuneColorSemantics` | 행운색, {color} | Lucky color, {color} | ラッキーカラー、{color} |
| `fortuneCategorySelectedSemantics` | {category}, 선택됨 | {category}, selected | {category}、選択中 |
| `fortuneScoreHelp` | 점수는 오늘 일이 풀리는 정도를 표현한 운세 지표야. 실제 확률을 뜻하지는 않아. | The score describes how smoothly the day may go. It isn’t a probability. | スコアは物事の進みやすさを表す占いの目安で、実際の確率ではないよ。 |
| `fortuneGenderUndisclosed` | 응답하지 않음 | Prefer not to say | 回答しない |

### E.1 상태와 문구 연결

| 조건 | 표시·행동 |
| --- | --- |
| `PROFILE_REQUIRED` 또는 최초 입력 | 입력 화면과 `fortuneProfileRequired`. 결과를 임의 생성하지 않음. |
| 성별 응답하지 않음 | `fortuneGenderUndisclosed` → 서버 `undisclosed`. male/female 표시를 그대로 API에 보내지 않고 man/woman으로 매핑. |
| `INVALID_BIRTH_DATE` | 입력 유지 + `fortuneBirthDateInvalid`. 상세 생년월일을 로그·문구에 다시 노출하지 않음. |
| 프로필 PUT 성공 | 기존 값이 실제로 변경된 경우에만 `fortuneProfileSaved`. 최초 저장은 운세 보기 단계로 이동; 동일 값 저장은 새 운세를 약속하지 않음. |
| 저장·삭제·조회 실패 | 각 동작별 Failed 문구 + `fortuneRetry`. 삭제 실패를 삭제 완료처럼 표시하지 않음. |
| 운세 `FEATURE_UNAVAILABLE` | `fortuneUnavailable`; 운세 대화의 동일 코드는 `fortuneChatUnavailable`로 화면 맥락에 맞춰 구분. |
| `access=ad_required` | `fortuneDetailUnlockCta`. 실제 광고를 끝까지 보고 SSV 확인이 필요함. |
| `access=included / unlocked_today`, `AD_NOT_REQUIRED` | 서버 상태를 확인하고 `fortuneDetailOpenCta`. 광고를 다시 요구하지 않음. |
| 광고 로딩/광고 없음/중도 종료 | Preparing / Unavailable / Incomplete를 구분. 모든 오류를 사용자 중도 종료로 취급하지 않음. |
| SDK 완료, 서버 상세 잠김 | `fortuneAdVerifying`. 지연 시 `fortuneAdPending` + `fortuneCheckUnlock`, 광고 재시청을 자동 요구하지 않음. |
| SSV 검증 후 `revealed` | 실제 상세 결과 표시. 검증 대기 문구 제거. |
| 사용자 현지 날짜 변경 | `fortuneDateChanged` 뒤 새 status/reveal. 기기 날짜만으로 서버 결과의 날짜를 바꾸지 않음. |
| `FORTUNE_CONTEXT_STALE` | `fortuneContextStale` 뒤 최신 결과로 돌아감. 이전 결과로 운세 대화를 이어가지 않음. |
| 운세 대화 CTA | 상세 공개 및 기능 사용 가능 조건에서만 제공. 누르면 지원되는 context_ref를 보냄. |
| 접근성 | `label/category/color`는 해당 로케일의 실제 표시명, score는 숫자. int/String ARB 메타데이터 정의. 시각적 색·선택 강조만으로 상태를 전달하지 않음. |
| 점수 도움말 | 사용자가 점수 의미를 확인하는 위치에 사용. 점수를 확률이나 객관적 예측력으로 설명하지 않음. |

### E.2 날짜·줄바꿈·용어

날짜는 API가 정한 현지 날짜를 로케일 포맷으로 표시한다. 예: 한국어 `9월 8일`, 영어 `Sep 8`, 일본어 `9月8日`. 입력 휠의 연월일 순서는 플랫폼 로케일을 따른다. `{date}`·`{day}`를 문자열 조각으로 이어 붙이지 않는다. 이 문서의 줄바꿈은 문장 구분용이며 화면에서 강제 두 줄을 보장하지 않는다.

브랜드는 기존 앱 본체의 `캐피 / Cappy / キャピー`로 맞춘다. 일본어 운세 샘플에만 있던 `カピ`는 사용하지 않는다. 종합 점수 표제는 `운세 점수 / Fortune score / 運勢スコア`, 색은 `행운색 / Lucky color / ラッキーカラー`로 통일한다. LLM에 전달하는 내부 운세 컨텍스트도 이 분야 용어를 사용하되, 서버 검증 헤더 등 내부 문구를 UI로 노출하지 않는다.
