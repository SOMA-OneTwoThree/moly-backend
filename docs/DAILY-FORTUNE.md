# 오늘의 운세

> 기준일: 2026-09-08
>
> 문서 상태: 20변형 서버 구현·세 언어 원고 통합·개발 DB 검증 완료. 배포·PR 미실행; 검증 결과는 4.7절
>
> 공개 계약: `openapi/paths/fortune.yaml`, `openapi/components/fortune.yaml`
>
> 계산·문구 원본: `app/resources/fortune/rules.v2.json`, `copy.v2.json`, `copy.v2.en.json`,
> `copy.v2.ja.json`

이 문서는 오늘의 운세 기능의 제품 규칙, 계산 방식, 문구, DB와 API를 한곳에 정리한 기준 문서다. 별도의
버전별로 기준 문서를 새로 만들지 않는다. 본문은 실행·검수 계약, 부록 A·B의 연결 문서는 전체 본문 전문이다.
클라이언트에 아직 반영하지 않은 UI 제안은 4.6절과 부록 D·E에서 구분한다.

- [검수 기준·적용 계획](#4-운세-문구-재설계--2026-09-08)
- [종합 운세 전문](#부록-a-종합-운세-전문--80경로--20변형--3언어) · [분야별 전문](#부록-b-분야별-운세-전문--40경로--20변형--3언어)
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
확정된 경로마다 저장된 선택 위치로 검수 묶음 20개 중 1개 선택
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

### 3.4 총평·흐름·행동을 함께 고르는 규칙

대표 후보를 하루 유형 8개 중 하나로 정한다.

| 내부 유형 | 의미 |
|---|---|
| 시작 | 새 일을 시작하는 흐름 |
| 전진 | 하던 일을 진척하는 흐름 |
| 집중 | 한 가지를 깊게 처리하는 흐름 |
| 조율 | 대화와 협조가 중요한 흐름 |
| 변화 | 방법이나 상황이 달라지는 흐름 |
| 정리 | 누락과 마무리를 챙기는 흐름 |
| 회복 | 속도와 컨디션을 되찾는 흐름 |
| 균형 | 여러 영향이 고르게 섞인 흐름 |

점수 구간 10개와 유형 8개를 합쳐 80개 문구 경로를 유지한다. 각 경로에는 20개 완성 묶음을 둔다.
총평·흐름 3문장·해볼 것·조심할 것을 함께 선택하므로 서로 다른 변형의 문장을 임의 조합하지 않는다.

보조 후보가 반대 방향으로 충분히 섞였는지는 내부 의미 코드의 `clear/mixed`로 보존한다.
이번 20변형은 점수·유형별 일상 상황의 다양화이며, `clear/mixed`를 새 경로나 별도 판정으로 확장하지 않는다.
`좋을 수도, 나쁠 수도 있다` 같은 양비론 문장은 쓰지 않는다.

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

각 종합 경로에는 `v01`~`v20`의 완성 묶음이 있다. 총평·흐름 3문장·해볼 것·조심할 것을 같은 ID로
고르며 문장끼리 섞지 않는다. 분야도 두 문장을 한 묶음으로 고른다. 한국어·영어·일본어는 동일 ID의
의미 명세를 공유하므로 언어를 바꿔도 조언의 상황과 방향은 유지된다.

선택 상태는 기존 `daily_fortunes.semantic_result.copy_selection`에 둔다. 종합 80경로와 분야 40경로,
최대 120개 경로의 마지막 날짜와 위치만 남긴다. 새 테이블·컬럼·사용자별 문구 이력 행은 만들지 않는다.
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

경로별 순서는 `SHA-256(선택 버전 | 경로 | 문구 ID)`의 바이트 순서로 고정한다. 사용자 UUID·생년월일·
난수로 순서를 섞지 않으며, 문구 수정 버전이 바뀌어도 ID와 선택 버전을 유지하면 순서는 유지된다.
날짜를 단순히 20으로 나누는 방법은 드물게 나오는 경로에서 같은 문구를 반복할 수 있어 사용하지 않는다.
매번 무작위로 고르는 방법도 연속 중복을 막지 못한다. 경로별 위치를 저장하면 **그 경로에서 20번 위치가
배정되는 동안 20개를 한 번씩** 사용한다. 사용자가 실제로 읽은 횟수나 달력상의 20일을 뜻하지 않는다.

기존 사용자 잠금 안에서 계산·문구·선택 상태를 한 트랜잭션으로 저장한다. 동시 요청은 같은 snapshot을
받고 저장 실패 때 위치만 먼저 소모하지 않는다. 예전 snapshot에 상태 키가 없으면 다음 재계산에서
초기화한다. 상태 키가 있는데 형식·버전·선택 ID가 잘못되면 조용히 초기화하지 않고 오류로 처리한다.
향후 선택 버전을 바꿀 때는 기존 상태를 어떻게 이어갈지 명시적으로 변환해야 한다.

## 4. 운세 문구 재설계 — 2026-09-08

### 4.1 이번 검수의 범위와 적용 상태

서버 목표 버전은 `fortune-copy.v2-variants.1`이다. 점수·계산 규칙·공개 스키마 3은 유지하고,
기존 경로 아래에 20개 완성 묶음을 추가한다. 클라이언트 ARB·화면 연결의 적용 상태와 서버 자산 적용을 구분한다.
운영·개발 배포와 기존 DB 결과의 일괄 교체는 이번 작업에 포함하지 않는다.

| 출처 | 범위 | 관리 위치 |
|---|---|---|
| 서버 `copy.v2*.json` | 언어별 종합 1,600묶음·분야 800묶음·색 12개 | 실행 자산; 전체 전문은 부록 A~C |
| 공통 의미 명세 | 종합 8유형·분야 4개마다 20개 상황 | `fortune-content-v3/briefs.json` |
| 클라이언트 `app_{ko,en,ja}.arb` | 기존 운세 33키 검수안 | 부록 D; 클라이언트 반영은 후속 |
| 서버 `home_blind.json` | 운세 배너 제목·질문·버튼·접근성·날짜 | 서버 배너에 적용; 부록 D |
| 클라이언트 운세 화면·상태 | 실제 표시 위치·전환·더미 값 | 4.6의 후속 연결 작업 |
| API 상태와 접근성 | 신규 UI 33키 | 부록 E의 미구현 제안 |

| 문구 단위 | 언어별 묶음 수 | 언어별 표현 수 | 세 언어 표현 수 |
|---|---:|---:|---:|
| 종합: 10점수 × 8유형 × 20변형 | 1,600 | 9,600 | 28,800 |
| 분야: 4분야 × 10점수 × 20변형 | 800 | 1,600 | 4,800 |
| **본문 합계** | **2,400** | **11,200** | **33,600** |

종합 한 묶음은 총평 1개·흐름 3개·해볼 것 1개·조심할 것 1개다. 분야 한 묶음은 2문장이다.
색 이름 36개와 고정 UI는 별도이며 20개씩 늘리지 않는다. 단어를 모두 다르게 만들려고 부자연스러운 동의어를
끼우지 않는다. 같은 뜻의 반복, 무의미한 수식어, 어떤 결과에도 붙일 수 있는 조언을 제거한다.

### 4.2 판정 기준

검수 단위는 한 문장, 같은 경로의 묶음, 같은 화면에 함께 나오는 종합+네 분야, 그리고 동일 키의 세 언어다.

| 항목 | 통과 조건 | 탈락 예 |
|---|---|---|
| 점수의 방향 | 저점은 마찰의 종류와 부담을 줄이는 행동, 중간은 유지할 조건, 고점은 시도할 기회를 말함 | 20점에서 대담한 결정을 권하거나 90점에서 모든 시도를 중지 |
| 유형의 의미 | 아래 8개 유형 중 해당 유형을 문장만으로 구분할 수 있음 | 시작·전진·변화에 모두 ‘새로운 도전을 해봐’ |
| 묶음의 논리 | 총평=판정, 설명1=일상에서 나타날 양상, 설명2=해석·대응, 설명3=대응·조건/주의. 행동 두 개가 이 내용에 연결됨 | 총평은 회복인데 설명은 분실물 확인, 행동은 연락 |
| 구체성 | 오늘 조절할 수 있는 대상·행동이 있고 해당 점수·유형에 이유가 있음 | ‘좋은 기운을 믿고 긍정적으로 지내’ |
| 근거의 범위 | 경로가 알려주는 경향만 말함. 새 인물·소식·시간·확정 사건을 만들어 넣지 않음 | ‘저녁에 기다리던 연락’, ‘뜻밖의 돈’, ‘연인의 선물’ |
| 언어 간 일치 | 주의/호조의 강도, 행동의 방향, 분야가 같음. 어순·비유·종결은 현지화 가능 | 한국어는 보류, 영어는 즉시 실행 |
| 분야 독립성 | 총평이 전체를 대표하고, 분야는 자기 점수대로 읽힘 | 종합 고점이라는 이유로 금전 저점도 좋다고 말함 |
| 독자 포괄성 | 연인·재직·재학 여부를 단정하지 않음 | ‘애인에게’, ‘상사에게’, ‘시험에서’만으로 대상 고정 |
| 문체 | 캐피의 친근한 말투. 짧은 판정 뒤 실천 가능한 권유 | 훈계·번역투·보고서체·회사식 코칭 |
| UI 정합성 | 버튼이 실제 동작을 설명하고 동적 결과와 안내 문구를 구분함 | 광고 검증 전 ‘해제 완료’, 누구에게나 같은 총평 |

질병·임신·사고·투자 수익을 예측하거나 점수를 성공 확률로 표현하지 않는다. 이는 운세의 분야와
점수 의미를 정확히 지키기 위한 편집 기준이다. 불필요한 경고문을 모든 화면에 붙이는 방식으로 해결하지 않는다.

### 4.3 점수·유형·분야의 의미 기준

점수는 3.3의 계산과 구간을 그대로 사용한다. 문구 때문에 점수·룰·표현 키를 바꾸지 않는다.

| 구간 | 공통 편집 강도 | 조언의 범위 |
|---|---|---|
| d00: 0~9 | 마찰이 가장 큼 | 부담 큰 결정의 보류, 필요한 부분만 처리 |
| d10: 10~19 | 여력보다 요구가 큼 | 새 부담을 줄이고 현재 범위를 지킴 |
| d20: 20~29 | 오해·누락이 눈에 띔 | 잘못 짚기 쉬운 부분을 특정하여 확인 |
| d30: 30~39 | 진행이 기대보다 늦음 | 순서·속도를 조절하고 재시도 범위를 좁힘 |
| d40: 40~49 | 작은 걸림이 남음 | 하던 것을 유지하되 필요한 확인을 덧붙임 |
| d50: 50~59 | 큰 유불리 없음 | 과장 없이 현재 조건에서 선택·마무리 |
| d60: 60~69 | 평소보다 수월함 | 준비한 범위에서 먼저 움직임 |
| d70: 70~79 | 노력과 반응이 잘 연결됨 | 기존 시도의 다음 단계로 나아감 |
| d80: 80~89 | 유리한 조건이 뚜렷함 | 미뤄 둔 기회에 접근하되 범위를 정함 |
| d90: 90~100 | 여러 조건이 우호적임 | 의미 있는 시도를 적극 권유, 결과 보장 금지 |

| 키 | 의미 중심 | 다른 유형과의 경계 |
|---|---|---|
| start | 처음 착수할 때의 준비와 첫 선택 | 이미 하던 것의 진척은 advance |
| advance | 진행 중인 것의 다음 단계 | 무조건 새로운 것을 시작하는 문구 금지 |
| focus | 주의의 유지와 깊이 | 정리·청소 자체가 주제가 아님 |
| coordinate | 의도 전달·상호 확인·협조 | 연애만의 해석으로 좁히지 않음 |
| change | 기존 방법의 변경·낯선 조건에 적응 | 특별한 사건이 반드시 생긴다는 뜻 아님 |
| organize | 순서·누락·남은 것의 마무리 | 쉬는 시간과 여력 회복은 recover |
| recover | 쉬었다 이어가는 속도와 집중 여력 | 의학적 회복이나 건강 예측이 아님 |
| balance | 우선순위와 여러 요구의 배분 | 결론을 ‘좋기도 나쁘기도’로 회피하지 않음 |

분야의 정식 표시명은 한국어 `애정 / 금전 / 일·학업 / 활력`, 영어 `Love / Money / Work & study / Energy`,
일본어 `恋愛運 / 金運 / 仕事・学業 / 活力`로 맞춘다. `work`를 학업만, `energy`를 건강만으로 번역하지 않는다.
애정은 호감과 마음의 전달, 금전은 소비 판단과 관리, 일·학업은 해야 할 일의 이해·진행,
활력은 일상 에너지와 페이스를 해석한다. 신체 증상·수익률·합격·상대 마음의 확정은 범위를 벗어난다.

### 4.4 언어별 문체와 반복 관리

- **한국어:** 캐피는 친근한 반말을 사용한다. 판정과 권유를 분리하고 모든 문장을 ‘좋아/해봐/수 있어’로
  끝내지 않는다. ‘흐름이 열린다’, ‘진전을 만든다’, ‘무난하게’ 같은 추상 번역투를 피한다.
  버튼·설정은 간결한 명사/동작형을 사용해 캐릭터 대사와 구분한다.
- **일본어:** 일상 운세에서 쓰는 짧은 판정과 부드러운 제안을 사용한다. `〜だよ`, `〜してみて`,
  `流れ`, `整える`를 만능 연결어로 쓰지 않는다. 매 문장 `あなた`를 주어로 넣지 않는다.
  버튼은 짧은 동작형, 설명은 캐피의 친근한 보통체로 맞춘다.
- **미국 영어:** 짧고 직접적인 일상 표현과 자연스러운 축약형을 사용한다. `It is a day when`,
  `make progress`, `create space`, `momentum`을 반복하는 코칭 문장을 피한다.
  실제 천체 계산과 별개로, 별자리별 목록이 아닌 이 화면의 제목은 `Today’s fortune`으로 통일한다.

한 묶음에서 같은 결론을 네 번 풀어 쓰지 않는다. 같은 유형의 이웃 점수 구간은 **강도뿐 아니라 어떤
선택이 적절한지** 차이가 있어야 한다. 부정어 한 개만 바꾼 대칭 문장은 탈락시킨다.
다른 분야와의 조합에서 조사·관사 같은 기능어는 반복 제한 대상이 아니다. 색 이름·UI 버튼처럼 같은 의미를
일관되게 표시해야 하는 항목은 오히려 동일 용어를 유지한다.

완전 중복과 문자열 유사도는 편집 검토용으로 전수 확인한다. 유사도만 낮추려고 길게 늘이거나 생소한 말을
넣지 않는다. 언어별 본문 전체의 완전 중복을 금지한다. 근접 중복은 실제 대체 후보인 **같은 경로·같은 필드의 20개**를
`SequenceMatcher >= 0.72`로 비교한다. 기존 길이 조건(한국어·일본어 12자, 영어 30자 이상)은 유지한다.
전체 문장 약 6,271만 쌍 대신 언어별 106,400쌍을 비교하며, 점수별 의미 차이와 영역 간 반복은 별도 편집 검수한다.
한국어 상투어 조합 규칙도 유지한다. 검사만 통과하려고 문장을 늘이거나 핵심 행동을 바꾸지 않는다.
문서 검수를 런타임 테스트 통과나 사람 원어민 검수로 표현하지 않는다.

### 4.5 전체 문구 읽는 법

- 부록 A는 종합 80경로의 20변형 전체 전문으로 연결한다. 각 행에 한국어·영어·일본어가 함께 있다.
- 부록 B는 분야 40경로의 20개 두 문장 묶음을 같은 순서로 제시한다.
- 부록 C는 12색의 현지 이름과 HEX다. 기존 색 키는 유지하고, 코랄에 해당하던 한 항목의 표시명과 HEX만 회색으로 교체한다.
- 부록 D는 기존 33 UI 키와 서버 배너다. `Sample`과 고정 결과 대사는 프리뷰용 검수안이며 서비스 결과로 쓰지 않는다.
- 부록 E는 아직 키가 없는 상태·접근성 문구의 신규 제안이다. 문구와 함께 필요한 동작을 정의한다.

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

한국어·일본어·미국 영어 담당 에이전트가 공통 상황 명세를 기준으로 원고를 작성했다. 총괄도 일부 유형을
직접 집필하고 해당 언어 담당의 교차검수를 받았다. 같은 ID의 상황·행동·주의를 언어 간 대조했으며,
기계 검사와 다른 에이전트의 서버 선택 로직 검증을 결합했다.

1. 유형·분야마다 20개 상황과 저점·중간·고점의 방향, 행동·주의 의도를 먼저 고정한다.
2. 일부 점수·변형의 파일럿을 대조한 후 전체 점수 구간을 집필한다. 숫자별 접두사만 바꾼 복제는 허용하지 않는다.
3. 각 단계에서 완전 중복·같은 경로 필드의 근접 중복·언어 혼입·금지어·문장 수를 확인한다.
4. 점수가 높다는 이유로 다른 행동을 권하거나 다른 언어와 주의의 원인이 달라지면 의미 명세로 돌아가 수정한다.
5. 전체 자산에 대해 선택·snapshot 호환·동시 요청·실패 시 원자성을 검증하고 전문을 실행 자산에서 생성한다.

2026-09-08 최종 검증 결과:

| 검증 | 결과 |
| --- | --- |
| 언어별 수량 | 각 120경로 × 20변형 = 2,400묶음, 11,200표현; 전체 33,600표현 |
| 원고·실행 자산 일치 | 세 언어 최종 원고 36개와 실행 JSON의 모든 경로·문장 일치 |
| 문구 구조·어휘 | 누락·언어 혼입·검사 대상 금지어·전체 완전 중복 0 |
| 같은 경로·필드 내 20변형 유사도 | 0.72 이상 후보 0; 짧은 표현 제외 범위는 4.4절과 테스트에 명시 |
| 점수 간 같은 변형 표본 유사도 | 긴 문장의 0.88 이상 검토 후보 0; 의미 품질을 자동 보장하는 지표는 아님 |
| 서버 회귀 테스트 | `uv run pytest -q --ignore=tests/integration` — 2,215개 통과 |
| 개발 DB 통합 테스트 | `MOLY_FORTUNE_TEST_ENV=dev uv run pytest -q tests/integration/test_fortune_variants.py` — 5개 통과 |
| 정적 검사 | `uv run ruff check app worker scripts tests db` 통과 |
| 전문 문서 | 실행 JSON에서 12개 전문 문서를 생성; `uv run python scripts/build_fortune_copy_docs.py --check`로 일치 검증 |

개발 DB 통합 검증은 이번 브랜치의 서비스 코드를 **기존 개발 Supabase DB**에 연결해 실행했다.
동일 경로를 반복 검증하기 위해 점수 계산을 고정하고 기능 활성화·구독 접근 판정은 대체했다.
이 5개 테스트가 실제 HTTP 호출·광고/구독 판정·천체 계산 정확성까지 검증했다는 뜻은 아니다.
동시 최초 요청·언어 변경, 날짜 진행·당일 프로필 수정·날짜 역행, flush 이후 커밋 실패와 재시도,
구 문구·코랄 당일 보존 후 새 날짜 회색 적용, 프로필 삭제 시 선택 상태 CASCADE를 확인했다.
각 테스트가 만든 임시 사용자와 운세·개인정보 상태는 종료 시 삭제하고 잔여 0을 확인했다.
배포된 개발 API에 이번 브랜치가 이미 반영됐다는 뜻은 아니며, PR·배포는 실행하지 않았다.

첫 전체 검사에서 회복 운세 3묶음의 `분명하게`가 한 응답에서 반복될 수 있음을 찾아 문구를 수정했다.
검사 기준을 낮추지 않고 새 자산·전문을 다시 생성한 뒤 전체 2,215개를 재실행해 통과했다.
최종 서버 diff도 별도 에이전트가 읽어 선택 상태 정합성·트랜잭션·당일 호환·비공개 상태 노출·개발 DB
정리 범위를 확인했으며, PR 준비를 막는 중대한 신규 결함은 발견하지 못했다.

언어별 검수 기록: [한국어](fortune-content-v3/review-notes.ko.md),
[미국 영어](fortune-content-v3/review-notes.en.md), [일본어](fortune-content-v3/review-notes.ja.md).
외부 원어민 교열자 승인, 기기 렌더링·실제 광고 동작 검증은 수행하지 않았다. 에이전트 문구 검수와
서버 테스트 결과를 클라이언트 구현이나 사람 원어민의 전수 검수 완료로 해석하지 않는다.

### 4.8 현지 표현 참고 자료

2026-09-08 확인. 아래 자료는 각 언어권의 운세 문체·분류 관습을 확인하기 위한 것이며 예측 정확성의
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
      "headline": "쉬는 방법은 익숙한 것 중에 골라도 되겠어.",
      "do": "잘 맞았던 휴식 방식 하나 선택하기",
      "pause": "휴식 방법을 새로 익힐 숙제로 만들기",
      "flow": [
        "새로운 방식을 찾아야만 휴식이 되는 것은 아니야.",
        "지금 부담 없이 할 수 있는 것을 택하면 충분하겠어.",
        "잘 맞았던 휴식 방식 하나를 선택해봐."
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
          "마음을 나눌 대화가 있다면 상대 이야기를 끝까지 들어봐.",
          "말을 마칠 때까지 듣고, 자기 경험으로 곧장 대화를 바꾸지는 마."
        ]
      },
      "money": {
        "score": 58,
        "text": [
          "실제 이용량을 보면 예상했던 비용과 비교할 수 있겠어.",
          "현재 얼마나 썼는지 살피고 단가가 낮다는 점만으로 총금액을 잊지 마."
        ]
      },
      "work": {
        "score": 51,
        "text": [
          "어려운 과제는 작은 질문으로 나눠 차례로 살펴봐.",
          "지금 답할 질문을 구분하고, 전체를 한 번에 해결하는 것만 방법으로 보지는 마."
        ]
      },
      "energy": {
        "score": 65,
        "text": [
          "남은 힘을 어디에 쓸지 살피면 원하는 활동에 여유를 보탤 수 있겠어.",
          "무언가 추가하기 전에 뒤의 몫부터 확인하고 지금의 기분만으로 계속 더하지 마."
        ]
      }
    }
  },
  "versions": {
    "ephemeris": "astronomy-engine-2.1.19-geocentric-apparent-v1",
    "rules": "fortune-rules.v2.1",
    "copy": "fortune-copy.v2-variants.1"
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

### 이번 20변형 확장의 배포 계약

- 코드와 세 언어 JSON·manifest를 같은 이미지에 포함한다. 새 카탈로그는
  `fortune-copy.v2-variants.1`, 내부 선택 상태는 `fortune-selection.v1`이다.
- DB 테이블·컬럼·제약·인덱스·RLS 변경이 없으므로 이번 변경을 위한 운영 마이그레이션 SQL은 없다.
  기존 `semantic_result`의 JSON 객체 제약 안에서 선택 상태만 추가한다. 운영 머지 때 별도의 데이터
  채우기나 과거 snapshot 일괄 재작성도 하지 않는다.
- 이미 공개한 유효한 당일 snapshot은 기존 문구 버전과 코랄 표시까지 유지한다. 다음 날짜 또는 기존
  freshness 조건에 따른 재계산부터 새 문구·회색을 적용한다. 배포 직후 모든 사용자에게 즉시 바뀌는 것은 아니다.
- 롤링 배포 중 구 이미지가 현재 snapshot을 읽는 것은 공개 스키마 3 안에서 호환된다. 다만 구 이미지가
  새 날짜 결과를 생성하면 새 선택 상태를 저장하지 않으므로 순환 이력이 사라질 수 있다. 배포·롤백 경계까지
  20회 비반복을 보장하지 않으며, 새 이미지로 통일된 이후 상태가 이어진다.
- 롤백은 코드와 해당 버전의 자산을 함께 되돌린다. 새 snapshot 자체를 삭제하지 않는다. 구 이미지가
  결과를 다시 만들기 전까지 이미 저장된 문구를 읽고, 이후에는 구 선택 방식으로 복귀한다. DB 복구 SQL은 필요 없다.
- PR은 전체 문구·검증·문서 정리를 마친 후에만 준비한다. 이번 작업에서는 사용자 요청에 따라 PR을 생성하지 않는다.

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
3. 모든 80개 전체 경로·40개 분야 경로·12색 완전성 및 hash 테스트
4. 언어별 11,200개, 총 33,600개 표현의 완전 중복·동일 경로 필드의 근접 중복·응답 안 상투어 반복 검사
5. 프로필 동일 PUT/변경 PUT과 unlock 보존 테스트
6. 광고 SSV 즉시 unlock·중복 transaction·만료·소유자 불일치 테스트
7. 광고 전 기본 공개·상세 누출 방지와 해금 후 전체 응답 Pydantic/OpenAPI 계약 테스트
8. 빈 로컬 DB의 schema·seed 재생성, 검토 SQL rollback 검증, dev 구조 검증, infra의 읽기 전용 DB preflight, 인증 포함 실제 HTTP smoke test
9. 출시 전 문구 전수 사람 검수, 점수 분포·경로 도달률 장기 시뮬레이션

오늘의 운세와 운세 대화 연결은 운영에서 활성화돼 있다. 운영 배포는 DB preflight, 두 인스턴스 롤링,
버전 일치와 synthetic health를 모두 통과해야 한다. 새 앱 빌드가 광고 연동을 시작할 때에는 iOS·Android에서
각각 실제 광고 1회를 사용해 `세션 발급 → SSV → 당일 unlock → status 재조회`를 출시 전 최종 확인한다.


## 부록 A. 종합 운세 전문 — 80경로 × 20변형 × 3언어

전체 본문은 실행 JSON에서 생성한 아래 전문으로 관리한다. 각 파일에서 점수 구간 → 문구 ID 순서로
한국어·영어·일본어를 나란히 읽을 수 있다. 파일당 200묶음이며, 대표 예시로 생략한 문구는 없다.
점수 100은 d90을 사용한다. 동일 ID의 상황·행동·주의 기준은 [공통 의미 명세](fortune-content-v3/briefs.json)에 있다.

| 유형 | 전체 전문 | 언어별 묶음 / 표현 |
|---|---|---|
| 시작 | [start](fortune-content-v3/overall-start.md) | 200 / 1,200 |
| 전진 | [advance](fortune-content-v3/overall-advance.md) | 200 / 1,200 |
| 집중 | [focus](fortune-content-v3/overall-focus.md) | 200 / 1,200 |
| 조율 | [coordinate](fortune-content-v3/overall-coordinate.md) | 200 / 1,200 |
| 변화 | [change](fortune-content-v3/overall-change.md) | 200 / 1,200 |
| 정리 | [organize](fortune-content-v3/overall-organize.md) | 200 / 1,200 |
| 회복 | [recover](fortune-content-v3/overall-recover.md) | 200 / 1,200 |
| 균형 | [balance](fortune-content-v3/overall-balance.md) | 200 / 1,200 |

## 부록 B. 분야별 운세 전문 — 40경로 × 20변형 × 3언어

각 분야는 점수 구간별 20개의 완성된 두 문장 묶음을 갖는다. 종합과 분야 사이의 문장은 조립하지 않는다.

| 분야 | 전체 전문 | 언어별 묶음 / 표현 |
|---|---|---|
| 애정 | [love](fortune-content-v3/category-love.md) | 200 / 400 |
| 금전 | [money](fortune-content-v3/category-money.md) | 200 / 400 |
| 일·학업 | [work](fortune-content-v3/category-work.md) | 200 / 400 |
| 활력 | [energy](fortune-content-v3/category-energy.md) | 200 / 400 |

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
`fortuneResultGreeting`과 `fortuneSample*`의 문구는 프리뷰용이다. 서비스 결과는 4.6의 API 필드로 교체한다. 프리뷰 세 언어도 동일 경로로 맞춰두었으며, 한 샘플 분야 본문을 모든 탭에 재사용하지 않는다.

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
| `fortuneResultGreeting` | 프리뷰 → API 결과 | 남은 매듭을 짓고 한결 가벼워질 수 있겠어. | The final pieces of unfinished business may fit together with less effort. | 長く残ったことを片づけ、次のための余白を作れそう。 |
| `fortuneResultDate` | 고정 UI | {date} | {date} | {date} |
| `fortuneScore` | 고정 UI | {score} | {score} | {score} |
| `fortuneLuckScoreLabel` | 고정 UI | 운세 점수 | Fortune score | 運勢スコア |
| `fortuneLuckColorLabel` | 고정 UI | 행운색 | Lucky color | ラッキーカラー |
| `fortuneSampleLuckColor` | 프리뷰 → API 결과 | 노랑 | Yellow | 黄色 |
| `fortuneTryLabel` | 고정 UI | 해볼 것 | Try this | やってみること |
| `fortuneSampleTryBody` | 프리뷰 → API 결과 | 마칠 준비가 된 일 끝내기 | Seek the final confirmation | 保留していた用事の結論を確かめる |
| `fortuneAvoidLabel` | 고정 UI | 조심할 것 | Watch out for | 控えたいこと |
| `fortuneSampleAvoidBody` | 프리뷰 → API 결과 | 완벽한 정리를 기다리며 보류하기 | Reopening completed matters | 終わったことに未練を残す |
| `fortuneDetailTitle` | 고정 UI | 자세한 운세 | Your full reading | 詳しい運勢 |
| `fortuneDetailUnlockCta` | 고정 UI | 광고 보고 자세한 운세 보기 | Watch an ad for the full reading | 広告を見て詳しい運勢を読む |
| `fortuneSampleDetailBody` | 프리뷰 → API 결과 | 흩어진 것을 제자리에 두는 판단이 수월하겠어.<br>정리할 범위를 분명히 하면 오래 끌던 마무리도 손에 잡힐 수 있어.<br>끝낼 준비가 된 일부터 닫고 필요한 자료만 남겨봐. | Roles, order, and remaining details may be easier to pin down.<br>Seek the final confirmation needed to finish.<br>Once it is settled, use the new space for what comes next. | 保留のままだった用事も、結論を確かめやすい日。<br>結果待ちのものがあれば、状況を確認して区切りをつけてみて。<br>終えたことは手放し、空いた場所を次の準備に使おう。 |
| `fortuneCategoryLove` | 고정 UI | 애정 | Love | 恋愛運 |
| `fortuneCategoryMoney` | 고정 UI | 금전 | Money | 金運 |
| `fortuneCategoryStudy` | 고정 UI | 일·학업 | Work & study | 仕事・学業 |
| `fortuneCategoryHealth` | 고정 UI | 활력 | Energy | 活力 |
| `fortuneSampleCategoryBody` | 프리뷰 → API 결과 | 다정하게 다가가면 한 걸음 가까워질 여지가 보여.<br>함께 시간을 보내고 싶은 사람이 있다면 부담 없는 일을 제안해봐. | An open exchange has a promising chance to deepen a connection.<br>Suggest something you would enjoy together, leaving the other person free to choose. | 心が弾み、人との距離を近づけるきっかけをつかめそう。<br>一緒にやりたいことがあるなら、相手の都合も聞きながら誘ってみて。 |

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
