# 오늘의 운세

> 기준일: 2026-09-08
>
> 문서 상태: 하루 총평 교열·네 분야 전면 재집필·세 언어 검수 및 서버·개발 DB 검증 완료. 개발 브랜치 PR 검토용, 배포 전
>
> 공개 계약: `openapi/paths/fortune.yaml`, `openapi/components/fortune.yaml`
>
> 계산·문구 원본: `app/resources/fortune/rules.v2.json`, `copy.v2.json`, `copy.v2.en.json`,
> `copy.v2.ja.json`

이 문서는 오늘의 운세 기능의 제품 규칙, 계산 방식, 문구, DB와 API를 한곳에 정리한 기준 문서다. 별도의
버전별로 기준 문서를 새로 만들지 않는다. 본문은 실행·검수 계약, 부록 A·B의 연결 문서는 전체 본문 전문이다.
클라이언트에 아직 반영하지 않은 UI 제안은 4.6절과 부록 D·E에서 구분한다.

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

- [검수 기준·적용 계획](#4-운세-문구-재설계--2026-09-08)
- [종합 운세 전문](#부록-a-종합-운세-전문--10점수-구간--20묶음--3언어) · [분야별 전문](#부록-b-분야별-운세-전문--40경로--20변형--3언어)
- [행운색](#부록-c-행운색-전문--12색--3언어) · [기존 UI·배너](#부록-d-기존-ui와-서버-운세-배너-전문) · [누락 상태 UI](#부록-e-누락-상태접근성-ui-제안-전문)

## 1. 사용자가 받는 것

사용자는 최초 진입 때 생년월일과 성별을 입력한다. 출생 시각과 출생지는 받지 않는다. 오늘 결과에는 다음
내용이 함께 온다.

| 영역 | 내용 |
|---|---|
| 종합 | 0~100점, 오늘의 총평 1문장 |
| 오늘의 흐름 | 총평이 나온 이유를 일상 언어로 풀어 쓴 3문장 |
| 행동 | 오늘 해볼 것 1개, 오늘 조심할 것 1개 |
| 분야 | 애정·금전·일/학업·활력 점수와 분야별 2문장 |
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
- LLM, 난수, 사용자 UUID, 요청 시각으로 결과나 문구를 바꾸지 않는다.
- 점수·유형은 기존 날짜·프로필·계산 버전으로 결정한다. 표현은 해당 경로의 과거 선택 위치도 사용한다.
- 같은 당일 snapshot은 그대로 반환한다. 서로 다른 사용자가 같은 점수를 받아도 이전 경로 사용 이력에 따라 문구는 다를 수 있다.
- 처음 공개한 결과와 한국어·미국 영어·일본어 문구를 한 행에 저장해 그날 동안 그대로 반환한다.
- 배포 중 문구 파일이 바뀌어도 이미 공개한 오늘 결과는 바꾸지 않는다.
- 사용자별 전문 계산 로그를 별도 테이블에 계속 쌓지 않는다.

## 3. 결과가 정해지는 순서

```text
생년월일 + 오늘 현지 날짜
        ↓
두 날짜에서 비교 가능한 하루 기준값 계산
        ↓
의미가 있는 후보를 찾고 같은 종류의 중복 제거
        ↓
강한 후보 최대 3개 선택
        ↓
종합 점수 + 하루 유형 + 보조 맥락 확정
        ↓
종합 점수 구간의 하루 총평 20묶음 중 1개 선택 + 분야별 독립 선택
        ↓
총평·흐름·행동·분야·행운색을 한 snapshot으로 저장
```

### 3.1 하루 기준값

출생 시각을 받지 않으므로 임의로 정오를 출생 시각이라고 가정하지 않는다. 출생일은 UTC 기준
00·06·12·18시 네 값을 원형 평균해 하루 기준값으로 만든다. 오늘은 사용자 시간대의 같은 네 시각을 사용한다.
경계가 359°와 1°인 값도 180°로 잘못 평균하지 않는다.

현재 계산 라이브러리와 기준은 다음과 같이 버전으로 고정한다.

- 계산 라이브러리: Astronomy Engine 2.1.19
- 좌표 기준: 지구 중심 apparent, true ecliptic-of-date
- 출생일 비교 요소: 변동이 너무 큰 1개 요소를 제외한 6개
- 오늘 비교 요소: 7개
- 계산 버전: `fortune-rules.v2.1`
- 결과 스키마: `3`

### 3.2 후보와 강도

오늘 7개와 출생일 6개를 비교해 최대 42개 후보를 검사한다. 각 후보는 방향 `q`와 정확도 `k`를 가진다.

- `q`: 좋은 방향 `+1`, 주의 방향 `-1`, 중간 방향 `±0.6667` 또는 `0`
- `k`: 기준점에서 정확하면 `1`, 허용 범위 끝이면 `0`
- 최종 후보값 `x = q × k`, 범위는 `-1...+1`

종합의 허용 범위는 빠르게 변하는 오늘 요소 하나만 5도, 나머지는 2도다. 분야별은 후보가 5개뿐이라
중립값만 반복되지 않도록 같은 근거를 7도·4도로 적용한다. 경계와 고정소수점 반올림 규칙은
`rules.v2.json`과 `fortune_rules.py`가 단일 원본이다.

같은 오늘 요소에서 여러 후보가 잡히면 가장 강한 하나만 남긴다. 그 뒤 절댓값이 큰 순서로 최대 세 개를
선택한다. 강도가 같으면 규칙 파일의 고정 우선순위로 결정한다.

### 3.3 종합 점수

```text
S = 선택된 후보값의 합, 빈 자리는 0
종합 점수 = 50 + round_half_away(S × 50 / 3)
```

점수는 행복 확률이나 좋은 사건의 개수가 아니다. **오늘 계획한 일을 원하는 방향으로 풀어가기 얼마나
수월한지**를 나타낸다.

| 후보 구성 | 점수 |
|---|---:|
| 후보 없음 | 50 |
| 정확한 좋은 후보 1개 | 67 |
| 정확한 좋은 후보 3개 | 100 |
| 정확한 주의 후보 1개 | 33 |
| 정확한 주의 후보 3개 | 0 |

양수와 음수는 50을 기준으로 완전히 대칭이다. 점수는 10점 단위 구간으로 문구에 연결한다. 100점은 90점대
구간을 쓴다.

| 점수 | 문구 결론 |
|---:|---|
| 0~9 | 중요한 선택과 새 시작을 미루는 편이 좋다 |
| 10~19 | 일을 늘리지 말고 현재 상황부터 챙긴다 |
| 20~29 | 작은 실수와 오해를 확인한다 |
| 30~39 | 생각대로 풀리지 않을 수 있어 서두르지 않는다 |
| 40~49 | 약한 주의가 우세해 속도보다 확인이 중요하다 |
| 50~59 | 크게 치우치지 않아 선택과 마무리가 중요하다 |
| 60~69 | 평소보다 수월해 계획을 이어가기 좋다 |
| 70~79 | 노력한 만큼 성과를 기대할 수 있다 |
| 80~89 | 좋은 조건이 분명해 기회를 잡기 좋다 |
| 90~100 | 여러 좋은 조건이 겹쳐 적극적으로 움직이기 좋다 |

2,288개 날짜 조합의 개발 표본에서 1~96점, 고유 점수 95개가 나왔다. 5·25·50·75·95 백분위는 각각
18·39·51·62·78점이었다. 균형 유형은 35.9%, 분야별 정확히 50점인 비율은 활력 24.6%, 애정 34.1%,
금전 40.2%, 일/학업 41.7%였다. 이 수치는 계산 분포 확인용이며 사용자에게 노출하지 않는다.

### 3.4 총평·풀이·행동을 함께 고르는 규칙

총평은 특정 작업의 지침이 아니라 **오늘 어떤 하루인지, 무엇을 느낄 수 있는지, 어떤 태도가 도움이 되는지**를 해석한다.
종합 점수의 10개 구간마다 20개의 완성 묶음을 두고, 제목·풀이 3문장·해볼 것·조심할 것을 함께 고른다.

기존 시작·전진·집중·조율·변화·정리·회복·균형별 총평 분류와 1,600묶음은 폐기했다. 내부 유형은 점수의
의미 코드와 행운색 계산에 남지만 총평 소재나 문구 선택 경로를 결정하지 않는다. 같은 점수 구간이면
내부 유형이 달라도 같은 `overall.dXX.general` 풀과 선택 위치를 사용한다.

계산 결과의 `reading_code`와 `expression_route`는 기존 형식을 유지한다. 카탈로그와 선택 계층에서만
점수 구간별 문구 경로로 변환한다. `clear/mixed`도 내부 계산 맥락이며 총평 분류를 추가하지 않는다.

### 3.5 분야별 운세

애정·금전·일/학업·활력은 각각 관련 후보 5개만 사용해 종합과 독립적으로 점수를 계산한다. 종합 점수를 네
분야 평균으로 만들지 않고, 종합 점수를 분야에 복사하지도 않는다.

`분야 4개 × 점수 구간 10개 = 40개` 경로를 유지하고, 각 경로에 20개의 두 문장 묶음을 둔다.
대표 세부 주제의 내부 의미 코드는 유지한다. 표현의 세부 상황은 해당 분야·점수의 판정 안에서 달라진다.

### 3.6 행운색

행운색은 유형 8개와 점수 구간 10개의 고정 표에서 고른다. 난수가 아니며 색상 수는 12개다.
이번 서버 자산에서 코랄을 회색으로 교체했다. 표시는 `회색 / Gray / グレー`, 색상값은 `#9E9E9E`다.
기존 계산 표와 저장 데이터의 호환을 위해 내부 키 `coral`은 유지한다. 이미 생성된 유효한 당일 결과는
기존 표시를 보존하며 새 결과를 만들 때부터 회색을 사용한다. 아래는 새 자산의 색상 목록이다.

`빨강, 회색, 주황, 노랑, 초록, 하늘색, 파랑, 남색, 보라, 분홍, 흰색, 베이지`

### 3.7 20개 문구의 선택·저장 계약

각 종합 점수 구간에는 `v01`~`v20`의 완성 묶음이 있다. 총평·흐름 3문장·해볼 것·조심할 것을 같은 ID로
고르며 문장끼리 섞지 않는다. 분야도 두 문장을 한 묶음으로 고른다. 한국어·영어·일본어는 동일 ID의
의미 명세를 공유하므로 언어를 바꿔도 조언의 상황과 방향은 유지된다.

선택 상태는 기존 `daily_fortunes.semantic_result.copy_selection`에 둔다. 종합 10경로와 분야 40경로,
최대 50개 경로의 마지막 날짜와 위치만 남긴다. 새 테이블·컬럼·사용자별 문구 이력 행은 만들지 않는다.
점수 계산이나 공개 API 스키마에는 이 상태를 노출하지 않는다.

| 상황 | 선택 결과 |
|---|---|
| 해당 경로를 처음 사용 | 경로별 고정 순서의 첫 문구 |
| 해당 경로의 마지막 사용일보다 늦은 현지 날짜 | 다음 위치로 이동; 20개 뒤 순환 |
| 같은 날 재요청·언어 변경 | 저장한 snapshot 유지 |
| 같은 날 프로필 수정으로 다시 계산 | 이미 사용한 경로는 같은 위치; 처음 나온 경로만 시작 |
| 다른 경로를 거쳐 원래 경로로 돌아옴 | 원래 경로의 위치에서 이어감 |
| 시간대 변경 등으로 날짜가 같거나 과거로 이동 | 해당 경로의 위치와 마지막 날짜를 유지 |
| 상세가 아직 잠겨 있음 | 전체 묶음을 함께 정하고 저장; 상세를 열 때 다시 선택하지 않음 |
| 프로필 삭제 후 재등록 | snapshot이 함께 삭제되므로 선택 상태도 처음부터 시작 |

경로별 순서는 `SHA-256(순열 버전 | 경로 | 문구 ID)`의 바이트 순서로 고정한다.
새 총평 순열에는 `fortune-selection.v2`, 기존 순서를 보존하는 분야 순열에는 `fortune-selection.v1`을 사용한다. 사용자 UUID·생년월일·
난수로 순서를 섞지 않으며, 문구 수정 버전이 바뀌어도 ID와 선택 버전을 유지하면 순서는 유지된다.
날짜를 단순히 20으로 나누는 방법은 드물게 나오는 경로에서 같은 문구를 반복할 수 있어 사용하지 않는다.
매번 무작위로 고르는 방법도 연속 중복을 막지 못한다. 경로별 위치를 저장하면 **그 경로에서 20번 위치가
배정되는 동안 20개를 한 번씩** 사용한다. 사용자가 실제로 읽은 횟수나 달력상의 20일을 뜻하지 않는다.

기존 사용자 잠금 안에서 계산·문구·선택 상태를 한 트랜잭션으로 저장한다. 동시 요청은 같은 snapshot을
받고 저장 실패 때 위치만 먼저 소모하지 않는다. 예전 snapshot에 상태 키가 없으면 다음 재계산에서
초기화한다. 상태 키가 있는데 형식·버전·선택 ID가 잘못되면 조용히 초기화하지 않고 오류로 처리한다.
새 상태 버전은 `fortune-selection.v2`다. 기존 `v1` 상태는 전체 형식·날짜·위치·선택 ID를 검증한 뒤
총평의 옛 80경로만 제거하고 분야 40경로의 위치·마지막 날짜는 보존한다. 총평은 완전히 다른 원고이므로
다음 재계산에서 새 점수 구간의 첫 문구로 시작한다. 당일 유효 snapshot은 읽기만 하며 강제로 전환하지 않는다.
알 수 없는 버전이나 손상된 상태는 변환으로 덮어버리지 않는다.

## 4. 운세 문구 재설계 — 2026-09-08

### 4.1 이번 재설계의 범위

사용자가 승인한 총평 예시와 분야별 5개 예시를 기준으로 세 언어의 총평과 네 분야를 새로 작성했다.
이번 추가 작업에서는 분야별 800묶음 전부를 교체하고, 총평 세부 문장의 추상 표현과 직역을 교열했다.
서버 문구 버전은 `fortune-copy.v2-field-readings.1`, 선택 상태 버전은 `fortune-selection.v2`다.
점수 계산과 공개 응답 스키마 3은 유지한다. 모든 본문에 문장 끝 마침표를 쓰지 않는다.
클라이언트 ARB·화면 연결과 실제 배포는 후속 작업이다.

| 문구 단위 | 언어별 묶음 수 | 언어별 표현 수 | 세 언어 표현 수 |
| --- | ---: | ---: | ---: |
| 새 총평: 10점수 구간 × 20묶음 | 200 | 1,200 | 3,600 |
| 분야: 4분야 × 10점수 구간 × 20묶음 | 800 | 1,600 | 4,800 |
| 본문 합계 | 1,000 | 2,800 | 8,400 |

총평 묶음의 여섯 요소는 제목·풀이 3문장·해볼 것·조심할 것이다. 같은 ID의 세 언어는 하루 판정과 상황,
조언의 뜻을 공유하되 문장 구조를 직역하지 않는다. 색 12개와 고정 UI는 20개씩 늘리지 않는다.
자산의 `content_status=approved_for_production`은 기존 카탈로그 로딩 계약이며 PR·배포에 대한 사용자 허락을 대신하지 않는다.

### 4.2 총평의 통과 기준

| 질문 | 통과 기준 |
| --- | --- |
| 오늘의 운세로 읽히는가 | 제목에 하루의 분위기·기회·주의가 담기고 풀이가 그 의미를 이어감 |
| 내 하루와 연결되는가 | 기대·말·선택·타이밍·기다림·작은 즐거움처럼 일상에 대입할 수 있음 |
| 특정 일을 하는 사람만 해당하지 않는가 | 파일·최종본·자료 분류·작업 절차 같은 전제를 요구하지 않음 |
| 점수 차이가 내용에 드러나는가 | 낮은 구간의 신중함, 중간의 일상적 만족, 높은 구간의 기회와 호응을 구별함 |
| 조언만 나열하지 않는가 | 해야 할 일 목록이 아니라 하루의 해석 뒤에 그에 맞는 태도를 권함 |
| 한 묶음이 이어지는가 | 제목·풀이·행동·주의가 같은 상황을 말하며 같은 결론을 반복하지 않음 |
| 언어 간 의미가 같은가 | 점수·ID별 판정 강도, 상황, 조언과 주의의 원인이 같음 |

총평에서 사람들과 나누는 말, 일상적인 피곤함이나 쉼을 다룰 수 있다. 총평과 분야를 기계적으로
구분하려고 이런 자연어를 금지하지 않는다. 다만 종합 점수로 별도 애정·금전·건강 결과를 단정하거나
정해지지 않은 인물·사건·시간을 사실처럼 예고하지 않는다. 낮은 점수는 불행 예고, 높은 점수는 성공 확률이 아니다.

### 4.3 점수 구간과 내용의 차이

점수 산식과 경계는 3.3을 유지한다. 편집 기준은 [공통 명세](fortune-content/briefs.json)에 둔다.
같은 20개 과제를 점수마다 강약만 바꿔 반복하지 않고, 각 구간 안에서 하루 해석 20개를 작성한다.
ID의 의미 일치는 같은 점수 구간의 한국어·영어·일본어 사이에서 보장한다. 다른 점수의 v01이
반드시 같은 소재일 필요는 없다.

| 점수 구간 | 하루 해석의 중심 |
| --- | --- |
| 0–19 | 기대·기분의 어긋남과 작은 부담을 살피고 자신을 몰아붙이지 않는 하루 |
| 20–39 | 성급히 판단하지 않고 지켜보면 나아질 여지가 있는 하루 |
| 40–59 | 익숙한 일상 속 작은 변화·선택·즐거움에서 만족을 찾는 하루 |
| 60–79 | 작은 기회와 주변의 호응을 받아들이며 한 걸음 다가가기 좋은 하루 |
| 80–100 | 자신의 장점을 드러내고 기회를 적극적으로 받아들이기 좋은 하루 |

분야는 각각 자기 점수대로 해석한다. 정식 표시명은 `애정 / 금전 / 일·학업 / 활력`,
`Love / Money / Work & study / Energy`, `恋愛運 / 金運 / 仕事・学業 / 活力`다.

### 4.4 세 언어의 말투와 반복 관리

- 한국어 총평과 분야에서 `~겠어`, `흐름이야`, `가능성이 보여`, `기운이 모여`를 사용하지 않는다. 승인 예시처럼 친근한 운세 판정과 권유로 이어간다.
  `~는 날이야`, `~을 눈여겨봐`, `~하지 않아도 괜찮아` 등을 문맥에 맞춰 쓰며 한 어미로 채우지 않는다.
- 영어는 자연스러운 일일 운세 문체를 사용한다. 기술 설명·성과 코칭·추상적인 명사구와 기계적인 `may/could` 접두사를 피한다.
- 일본어는 하루의 운세를 짧게 해석하고 부드럽게 권한다. 한국어 문장 구조를 그대로 옮기거나 `〜そう`만 바꿔 늘리지 않는다.
- 총평과 분야별 본문의 문장 끝 `.`, `。`는 제거한다. 문장 구분은 기존 배열 요소를 사용한다.

짧은 행동 문구나 일상 어휘가 겹친다는 이유만으로 부자연스러운 동의어로 바꾸지 않는다.
총평 제목 200개와 제목+풀이의 완성 해석 200개는 언어별 중복 없이 구성한다. 같은 점수 구간의
완성 해석 간 `SequenceMatcher >= 0.85`는 복제 여부를 검사하는 보조 기준이다. 예전의 모든 짧은 필드
유사도 0.72 금지와 상투어 최대 1회 규칙은 폐기했다. 이 기준이 운세다운 자연어보다 문자열 차이를
우선하게 만들었기 때문이다. 제목의 의미 차이·점수 적합성·독자 관련성은 별도로 읽고 검수한다.

분야별 첫 문장은 해당 분야의 해석, 둘째는 그 해석에 맞는 제안·기회다. 총평처럼 매번 `~는 날이야`로
끝내거나 금지형 조언만 반복하지 않는다. 동일 분야·점수·ID의 세 언어 의미는 맞추지만 점수가 다른 ID에
고정 소재를 강요하지 않는다. 이전의 20개 고정 주제 명세와 문서 제목은 제거했다.

| 분야 | 해석의 중심 | 피할 표현 |
| --- | --- | --- |
| 애정 | 관심, 대화, 감정의 거리, 자연스러운 매력 | 연인 전제, 상대의 마음 확정, 호감 보장 |
| 금전 | 필요한 소비, 가격과 가치, 쓰고 아끼는 만족 | 수익 예고, 충동적 지출 권장, 금융 매뉴얼 |
| 일·학업 | 집중, 이해, 익힘, 노력과 성취 | 직장·학생 신분 전제, 실습·검증·수행 절차 나열 |
| 활력 | 체감 기운, 활동의 즐거움, 자기 속도와 휴식 | 신체 진단, 회복 보장, 활동량 경쟁 |

카탈로그는 분야의 두 문장을 합친 전체 묶음 중복과 같은 묶음 안의 동일 문장 두 개를 거부한다.
짧은 제안이 다른 해석과 함께 자연스럽게 다시 쓰이는 것은 허용한다. 문자열 유사도만 낮추려는
수식어 덧붙이기보다 점수 적합성·구체성·해석과 제안의 연결을 직접 읽어 확인한다.

### 4.5 전체 문구 읽는 법

- 부록 A는 새 총평 10점수 구간의 전문이다. 각 구간 20묶음의 세 언어를 나란히 볼 수 있다.
- 부록 B는 이번에 전면 교체한 분야별 40경로의 20묶음 전문이다. 각 묶음의 분야 해석과 제안·기회를 나란히 읽는다.
- 부록 C는 `회색 / Gray / グレー`를 포함한 12색이다.
- 부록 D는 고정 UI·배너 검수안이다. `Sample`은 프리뷰이며 실제 결과는 API를 사용한다.
- 부록 E는 아직 구현하지 않은 상태·접근성 UI 제안이다.

### 4.6 클라이언트 연결 및 적용 계획

현재 클라이언트 `f82b5a6`의 운세 화면은 API를 연결하지 않은 더미다. 대기 타이머 후 고정 결과를 보여주며,
상세 버튼은 실제 광고 검증 없이 상태만 바꾼다. 모든 분야 탭이 같은 `fortuneSampleCategoryBody`를 사용한다.
따라서 번역 파일 수정만으로 실제 운세에 맞는 문장이 표시되지는 않는다.

| 표시 위치 | 최종 연결 | 이번 검수에서 확인한 후속 작업 |
|---|---|---|
| 결과 말풍선 `fortuneResultGreeting` | `result.overall.headline` | 고정 운세 대사 제거 |
| 종합 점수·날짜 | `result.overall.score`, 응답 최상위 `local_date` | 고정 82점과 기기 `DateTime.now()` 대신 서버 결과 사용 |
| 오늘의 흐름 | `result.overall.flow` 3문장 | 상세 권한 확인 후 표시 |
| 해볼 것·조심할 것 | `result.overall.do`, `result.overall.pause` | Sample 본문 대신 동일 경로 결과 사용 |
| 분야 탭·본문 | `result.categories.{love,money,work,energy}.{score,text}` | 점수와 본문을 선택한 분야에 함께 바인딩 |
| 행운색 | `result.lucky_color.{name,hex}` | 샘플 색 이름과 고정 색 분리 |
| 성별 선택 | `man / woman / undisclosed` | 기존 클라 male/female을 API 값에 매핑하고 ‘응답하지 않음’ 선택지 추가; 서버 enum 변경 불필요 |
| 입력 완료 | 프로필 PUT 성공 | 실패했는데 결과 단계로 진행하지 않음 |
| 생년월일 선택 | 서버와 같은 1900년·만14세 조건 | 현재 오늘까지 선택 가능한 휠 상한 수정 필요 |
| 상세 열기 | `access`와 SSV 이후 서버 상태 | 구독 포함/이미 해제는 광고 버튼 없이 열기 |
| 광고 확인 중 | 상태 재조회 | 콜백 수신만으로 성공 문구 표시 금지 |
| 오류 안내 | API 오류 코드 → ARB | 한국어 서버 `message`를 세 언어 화면에 그대로 노출하지 않음 |
| 운세 대화 | 확인된 결과 컨텍스트 | 내부 `_render` 분야명도 일·학업/활력으로 통일, 새 운세를 즉석 창작하지 않음 |

서버의 20변형 선택·저장, 분야 본문을 포함한 운세 대화 컨텍스트, 운세 배너 문구는 이번 구현 범위다.
클라이언트는 다음 순서로 연결한다.

1. 기존 33키의 고정 UI와 신규 상태 키를 ARB에 반영한다. placeholder 이름·개수·메타데이터를 세 언어에서 맞춘다.
2. 결과 말풍선·점수·흐름·행동·분야·색을 API 응답에 연결하고 샘플 값을 실제 결과에 사용하지 않는다.
3. 프로필 저장·상세 잠금·광고 확인·오류 상태를 연결한다. `study/health` 키를 유지할지 `work/energy`로 바꿀지는
   호출부와 생성 코드를 함께 수정할 때 정한다. 서버 키는 이미 `work/energy`다.
4. 날짜 형식과 접근성 읽기를 검증한다. 문서의 `<br>`는 표에서 줄을 나누기 위한 표시이며 API 문자열에 넣지 않는다.
5. 세 언어·작은 화면·큰 글자·잠금/해제/실패 상태를 실제 화면에서 확인하고 Flutter 분석·위젯 테스트를 실행한다.

서버 검증 결과만으로 클라이언트 연결이나 줄 넘침·광고 SDK 검증이 끝났다고 판단하지 않는다.

### 4.7 검수 과정과 결과

한국어 담당이 승인된 예시를 바탕으로 점수 구간별 20묶음을 새로 작성하고, 영어·일본어 담당이
동일 ID의 하루 판정·상황·조언을 대조해 현지화한다. 총괄은 저·중·고점과 세 언어를 교차 검수한다.
기존 8유형별 원고는 재사용하지 않는다. 언어별 검수 기록은 아래와 같다.

[한국어·영어·일본어 통합 검수 기록](fortune-content/review-notes.md)

최종 검증 결과(2026-09-08, 이번 분야 교체 후 다시 실행):

| 검사 | 결과 |
| --- | --- |
| 총평 재검수 | 언어별 200묶음 / 1,200표현 전부 재검수; 제한 교열 KO 41·EN 62·JA 58필드 |
| 분야 신규 원고 | 언어별 800묶음 / 1,600문장, 합계 2,400묶음 / 4,800문장 모두 교체 |
| 원고와 서버 자산 | 최종 분야 120개 점수별 원고 및 총평 교열본과 실행 JSON의 모든 묶음 일치; 계산 규칙·색 자산 변경 없음 |
| 문구 검사 | 본문 8,400표현 수량 확인; 문장 끝 마침표·한국어 금지 말투·제목 및 완성 묶음 중복 0; 분야 같은 경로 내 전체 묶음 유사도 ≥0.85 후보 0 |
| 서버 회귀 | `uv run pytest -q --ignore=tests/integration` — 최신 dev 반영 후 2,179개 통과 |
| 서비스/개발 DB 통합 | `MOLY_FORTUNE_TEST_ENV=dev uv run pytest -q tests/integration/test_fortune_variants.py` — 7개 통과 |
| 정적 검사 | `uv run ruff check app worker scripts tests db` 통과 |
| 문서 일치 | `uv run python scripts/build_fortune_copy_docs.py --check` — 총평 10개·분야 4개 전문 일치 |

총괄은 저점·중간·고점의 묶음을 세 언어로 대조하고, 한국어의 추상적인 제목과 제안 발생 단정,
영어의 딱딱한 표현, 일본어 직역 결합을 지적해 수정했다. 한국어 담당은 총괄이 쓴 금전·활력을 독립 검수하고
영어 36묶음을 추가로 대조했다. 각 언어 담당은 원고 전체의 의미·말투를 자기검수했다.
짧은 일상 표현이 겹친다는 이유로 의미를 바꾸거나 검사 통과용 수식어를 붙이지 않았다.

별도 에이전트가 카탈로그·버전 전환 통합 테스트·문서 생성기·배포 계약을 읽어 검토했고, 진행을 막는 결함을 발견하지 못했다.
지적된 API 예시는 이번 실행 코드의 실제 공개 결과로 다시 생성했으며 v1 writer 롤백 제한은 7절에 반영했다.

개발 DB 검증은 새 브랜치의 서비스 코드를 **기존 개발 Supabase DB**에 연결해 수행했다. 동일 경로 반복을 위해
점수 계산을 고정하고 기능 활성화·접근 판정을 대체했으며, 실제 SQL·잠금·커밋·실패 시 롤백·삭제는 실행했다.
동시 요청·언어 변경·날짜 진행/역행·당일 프로필 변경·저장 실패 재시도·기존 당일 문구 보존·프로필 삭제와
v1→v2 전환 시 분야별 순서 보존을 확인했다. 이번에는 직전 총평 버전→새 분야 문구 버전의 전환에서도
세 언어의 당일 옛 문구 보존, 다음 날짜의 새 문구 적용, v2 선택 위치 진행을 별도로 확인했다.
임시 테스트 데이터는 종료 시 삭제하고 잔여가 없음을 검사했다.
HTTP·광고 SDK·실제 구독 판정·천체 계산 정확성의 통합 검증으로 확대 해석하지 않는다.

이 기록은 에이전트 집필·교차검수 및 서버 테스트 결과다. 외부 사람 원어민의 전수 검수나 실제 기기 표시 검증을
수행했다는 뜻은 아니다. 구현·검수를 마친 뒤 사용자의 승인을 받아 개발 브랜치 대상 PR 하나로 제출한다. 실제 배포 검증은 머지 이후 별도다.

### 4.8 현지 표현 참고 자료

앞선 2026-09-08 검수에서 확인한 장르 참고 자료다. 이번 재집필은 사용자가 승인한 총평 예시와 분야별 예시를 우선한다.
아래 자료는 각 언어권의 운세 문체·분류 관습을 확인하기 위한 것이며 예측 정확성의
근거가 아니다. 문장을 복사하거나 특정 날짜의 사건 예고를 옮기지 않고 독립 작성했다.

- 한국어는 짧은 판정과 일상 행동을 잇는 구성을 참고했다.
  [세계일보 띠별 운세](https://www.segye.com/newsView/20161231000164),
  [부산일보 일일 운세](https://v.daum.net/v/20260830134546257).
  단정적인 사건 예고·연령별 인물 전제는 가져오지 않았다.
- 일본어는 총합·분야 이름과 짧은 권유의 사용례를 참고했다.
  [ニフティ 今日の運勢](https://uranai.nifty.com/f12seiza/uo/),
  [LINE占い 공식 도움말](https://help2.line.me/linefortune_chat/web/categoryId/20009649/3/pc?lang=ja).
  `仕事・学業 / 活力`은 이 앱의 실제 축에 맞춰 조정했다.
- 미국 영어는 직접적인 일상어 판정에서 실천 가능한 행동으로 이어지는 문장 리듬을 참고했다.
  [Astrology.com Daily Readings](https://www.astrology.com/horoscope/daily/today.html),
  [Horoscope.com Daily Readings](https://www.horoscope.com/us/horoscopes/general/index-horoscope-general-daily.aspx).
  별자리·행성의 새 근거를 덧붙이거나 결과를 보장하는 표현은 가져오지 않았다.

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
결과 계산·저장 스키마는 계속 `3`이며, 기존 당일 snapshot과 해금은 유지하므로 DB migration은 없다.

### 5.3 공개 응답 예시

2002-12-13생, 2026-08-27, Asia/Seoul에 **이전 선택 이력 없이 처음 생성한** 결과다.
이번 브랜치의 계산·선택·공개 변환 코드에서 생성했다. 생년월일·날짜가 같더라도 이전 경로별 선택 이력이
있으면 점수는 같고 문구 ID는 달라질 수 있다. 성별과 표시 이름은 현재 계산에 사용하지 않는다.

```json
{
  "state": "revealed",
  "access": "included",
  "local_date": "2026-08-27",
  "result": {
    "schema_version": 3,
    "locale": "ko",
    "overall": {
      "score": 47,
      "headline": "오늘은 네 취향을 다시 알아가는 재미가 있어",
      "do": "오늘 한 가지는 네 취향대로 정해봐",
      "pause": "선택할 때마다 다른 사람의 평가를 먼저 찾지 마",
      "flow": [
        "남들이 좋다는 것보다 네가 끌리는 쪽에 만족이 가까워",
        "평범한 선택에도 네 마음을 반영하면 하루가 더 선명해져",
        "작은 것부터 좋아하는 방향을 골라봐"
      ]
    },
    "lucky_color": {
      "key": "green",
      "name": "초록",
      "hex": "#43A047"
    },
    "categories": {
      "love": {
        "score": 59,
        "text": [
          "함께할 일을 제안하기에 부담이 적은 때야",
          "누구나 편하게 참여할 수 있는 짧은 약속부터 꺼내봐"
        ]
      },
      "money": {
        "score": 58,
        "text": [
          "오늘은 작은 절약을 부담 없이 이어가기 좋아",
          "만족은 비슷하고 비용은 덜 드는 방법 하나를 찾아봐"
        ]
      },
      "work": {
        "score": 51,
        "text": [
          "네 생각을 말로 꺼내며 더 분명하게 다듬을 수 있어",
          "완벽한 답이 아니어도 근거와 함께 이야기해봐"
        ]
      },
      "energy": {
        "score": 65,
        "text": [
          "혼자서도 가볍게 나설 마음이 생길 수 있어",
          "누군가의 일정을 기다리기보다 가까운 곳부터 다녀와봐"
        ]
      }
    }
  },
  "versions": {
    "ephemeris": "astronomy-engine-2.1.19-geocentric-apparent-v1",
    "rules": "fortune-rules.v2.1",
    "copy": "fortune-copy.v2-field-readings.1"
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

### 이번 총평·분야 문구 재설계의 배포 계약

- 코드와 세 언어 JSON·manifest를 함께 배포한다. 카탈로그는 `fortune-copy.v2-field-readings.1`, 선택 상태는 `fortune-selection.v2`다.
- DB 테이블·컬럼·제약·인덱스·RLS 변경은 없다. 운영 마이그레이션 SQL이나 일괄 데이터 재작성은 필요하지 않다.
- 이미 생성된 유효 당일 snapshot은 옛 문구와 말투·마침표까지 보존한다. 다음 날짜 또는 기존 freshness 조건에 따른 재계산에서 새 총평·분야 문구를 적용한다.
- v1 선택 상태가 있으면 전체를 검증한 뒤 새 상태로 전환한다. 폐기된 유형별 총평의 위치는 버리고 새 점수별 풀을 시작하며, 분야별 위치·순열·마지막 날짜는 유지한다.
- 이미 `fortune-selection.v2` 상태인 사용자는 이번 문구 교체만으로 순서나 위치를 초기화하지 않는다. 같은 ID의 새 문구를 다음 재생성부터 사용하며 별도 배포 시점이나 DB 상태를 추가하지 않는다.
- 새 상태는 최대 50경로다. 상태가 없던 기존 사용자는 초기화하고, 손상됐거나 알 수 없는 버전은 조용히 초기화하지 않는다.
- **선택 v1을 쓰던 구 코드로 무작정 롤백하지 않는다.** 구 reader는 유효한 새 snapshot의 공개 형식을 읽을 수 있지만 구 writer는 다음 재계산 때 v2 상태를 거부한다. 이 버전 경계에서는 구 writer와 새 writer의 동시 가동을 피한다.
- 직전 `fortune-copy.v2-day-overview.1`은 선택 상태가 이미 v2여서 이번 문구 교체와 별도 상태 변환이 없다. 해당 버전으로 원고를 되돌릴 때도 코드·자산·manifest를 함께 되돌리고 당일 snapshot은 보존한다. 이 경우와 위의 선택 v1 코드 복귀를 구분한다.
- 문제가 생기면 기능 플래그로 운세 기능을 일시 중지한 뒤 v2 상태를 이해하는 수정 이미지를 배포하는 것을 우선한다. v1 코드로 되돌려야 한다면 별도 호환 변환을 검토해야 하며, 이번 변경에는 데이터 삭제나 다운그레이드 SQL을 포함하지 않는다.
- PR은 최종 검수 후 사용자의 허락을 받아 `dev` 대상으로 하나만 생성한다. 이번 제출에는 머지·배포를 포함하지 않는다.

### 기존 기능의 배포 계약

- `FORTUNE_ENABLED`, `FORTUNE_CHAT_ENABLED`의 애플리케이션 기본값은 fail-closed를 위해 `false`다.
  개발·운영에서는 infra의 `deploy.sh`가 두 값을 명시적으로 `true`로 주입한다. 기능을 다시 끄려면 코드 기본값에
  기대지 말고 infra 설정을 변경한 뒤 두 인스턴스를 재배포한다.
- 현재 `fortune-rules.v2.1`은 결정 규칙·점수 분포·3개 언어 카탈로그·API 계약 검증을 마쳐
  `approved_for_production=true`다. 승인되지 않은 후속 규칙은 운영에서 계속 fail-closed다.
- 규칙이나 문구를 바꾸면 asset version과 manifest SHA-256을 함께 바꾼다.
- DB 정의는 `db/schema.sql`에 합친다. 기존 DB에는 검토한 차이 SQL을 수동 적용한다.
  파일 checksum 원장을 배포 조건으로 삼거나 날짜별 마이그레이션을 추가하지 않는다.
- 스키마 변경 시 구·신 이미지가 공존할 수 있는 DB 변경을 먼저 적용하고 검증한 뒤 코드를 배포한다.
  기능을 새로 켜는 경우에는 플래그 OFF 코드 배포 → infra 설정 변경 → 같은 이미지 SHA 재배포 →
  기능 smoke 순서를 확인한다. infra 머지만으로 실행 중인 환경변수가 바뀌지는 않는다.
- 새 이미지의 DB preflight는 전체 구조 계약을 읽기 전용으로 검증한다. 구 이미지 롤백은 infra의
  운세·건초 광고 구조 검증을 사용한다. 테이블·RLS·권한·광고 만료·메시지 제약·인덱스를 확인하며
  어느 경로도 DB를 자동 변경하지 않는다.

- 프로필 API는 플래그가 꺼지면 운세 테이블 접근 전에 종료한다. worker 정리는 `to_regclass`로 테이블 존재를
  확인하므로 migration 전에는 건너뛰고, 테이블이 생긴 뒤에는 기능을 중지해도 7일 보존 정책을 계속 지킨다.

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

각 분야는 점수 구간별 20개의 완성된 두 문장 묶음을 갖는다. 종합과 분야 사이의 문장은 조립하지 않는다.

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
