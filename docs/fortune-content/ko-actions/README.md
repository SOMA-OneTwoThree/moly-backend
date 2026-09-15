# 해볼 것·조심할 것 개선과 기본 본문 공개

기준 dev: `e02a8f67`, 작업 브랜치: `feat/fortune-actions-and-basic-flow`. 세 언어 작성·독립 독해 검수, 정본·서버 자산 연결과 정적 검사를 완료했다. 사용자 승인 범위는 두 변경을 함께 dev에 PR·머지·배포하고 실제 API에서 검증하는 것까지다.

[한국어 예시 6쌍](examples.md) · [첫 개선안 이후 수정](revision-comparison.md) · [한국어 전문 비교](comparison.md) · [문장 기준](CRITERIA.md)

## 문구 범위와 기준

| 언어 | 대상 | 별도 독해 검수 |
| --- | ---: | --- |
| 한국어 | 156쌍·312개 | 작성자 2명, 별도 검수자 전량, root 표본 84쌍 |
| 영어 | 156쌍·312개 | 작성자와 별도로 root 72쌍 + 검수자 84쌍 독해 |
| 일본어 | 156쌍·312개 | 일본어 작성자와 다른 검수자가 전량 독해 |

카드 78종 × 정·역 2종의 `do`·`pause`만 수정한다. 총평·총운 두 단락·나머지 네 분야의 문구와 카드 해석은 유지한다. 첫 방문 6세트에는 같은 카드의 수정 문구를 연결한다. 문구 버전은 `fortune-copy.v8-localized.2`다.

한국어 기존 ‘해볼 것’은 평균 15.4자, ‘조심할 것’은 17.0자였다. 수정본은 각각 59.7자·65.7자로, 행동 이름만 나열하던 부분에 조건·방법·이유를 더했다. 글자 수는 공백·문장부호를 포함한다. 길이는 품질 판정 기준을 대신하지 않는다.

첫 개선안에도 ‘미뤄둔 즐거운 일’처럼 대상이 불분명하거나 활동 제안 뒤에 갑자기 남의 반응·자기 축하가 붙는 문제가 남았다. `ko-actions.r2`에서는 현재 말투와 1~2문장 형식을 유지하며 다음을 다시 확인했다.

- 어떤 상황에서 무엇을 하라는지 두 행동 각각만 읽어도 알 수 있어야 한다.
- 둘째 문장은 같은 행동의 방법·이유·범위를 설명한다. 새 목적을 갑자기 붙이지 않는다.
- 독자가 뜻을 고쳐 읽어야 하는 추상적인 결합을 평이한 말로 바꾼다.
- 설명이 충분한 한 문장은 그대로 완결한다. 반복이나 별개 조언으로 길이를 채우지 않는다.
- 영어·일본어는 확정 한국어의 조건·대상·확신을 유지하되 문장 순서를 직역하지 않는다. 카드 이름은 앱 문구에 노출하지 않는다.

## 원고와 검수 기록

한국어 후보 원고는 [candidates.json](candidates.json), 작성자는 `major-cups` 72쌍과 `minor-actions` 84쌍을 나눠 재독했다. [한국어 독립 검수](review.json)는 156쌍에 대해 각 행동과 문장 연결을 자기 말로 기록하고 여섯 항목을 별도로 판정했다. [root 기록](root-review.json)은 실제 읽은 84쌍의 범위만 표시한다.

영어·일본어 원고는 [en.json](en.json)·[ja.json](ja.json), 작성자 기록은 각각의 `author-review.json`, 독립 검수는 [en.review.json](en.review.json)·[ja.review.json](ja.review.json)에 둔다. 두 행동을 단독으로 읽고 기존 현지어 총평·본문 및 확정 한국어와 대조한다. 수정된 원고는 재독한 뒤 해시를 갱신한다. 이 작업은 에이전트 독해 검수이며 인간·원어민 감수 인증이 아니다.

`source-overall.json`과 `source-overall.en.json`·`source-overall.ja.json`은 수정 전 전문이다. `round-1.candidates.json`은 사용자의 후속 지적 이전 개선안으로, 비교를 위해 보존한다. 실행 자산을 생성할 때에는 `ko-rewrite/overall.json`, `localization/{en,ja}/overall.json`의 확정본을 사용한다.

정본의 기존 검수 기록에는 새 행동을 실제 읽은 담당자와 이 폴더의 근거를 연결한다. 수정하지 않은 본문·분야는 이전 검수 범위를 유지한다. 기존 승인 해시만 새 문구에 덮어써서 읽은 것으로 처리하지 않는다.

한국어 비교 산출물과 기록의 일치는 다음 명령으로 확인한다. 자동 검사는 기록과 현행 원고의 일치 여부를 확인하며 독해 자체를 대신하지 않는다.

```sh
python3 docs/fortune-content/ko-actions/build_review.py --check
python3 docs/fortune-content/ko-rewrite/build_review_artifacts.py --check
python3 docs/fortune-content/localization/validate.py --check
python3 scripts/build_fortune_localized_copy.py --check
python3 scripts/build_fortune_copy_docs.py --check
python3 scripts/check_fortune_tarot_editorial.py
```

## API와 배포

- `locked.result.overall`에도 `flow`를 필수로 포함한다. `categories`만 필드 자체를 생략한다.
- 새 운세의 총운은 2단락이며, 기존 저장본의 3단락도 그대로 읽는다. 공개 계약은 `minItems: 2`, `maxItems: 3`, `schema_version: 3`이다.
- Python 응답 스키마·OpenAPI 구성 파일·`openapi/openapi.bundle.yaml`을 같이 갱신한다. 기본 공개 때문에 광고 해금이나 카테고리 접근 권한이 달라지지 않는다.
- 문구·본문 공개를 위해 당일 결과를 삭제하거나 다시 추첨하지 않는다. 기존 당일 결과의 본문은 바로 공개되지만, 행동 문구는 저장된 버전을 유지한다. 새 버전으로 생성되는 결과부터 수정 문구가 적용된다.
- 점수·점성술·카드 추첨·사용자 간 멱등성·첫 방문 카드 구성은 유지한다. DB 마이그레이션·환경변수 추가는 없다.

PR은 구현·문구 검수·정적 검사·배포 전 비교 자료 확보를 마친 뒤 생성한다. CI에서 운세 API를 포함한 전체 회귀 테스트를 통과한 후 dev를 머지한다. 배포는 기존 GitHub Actions 개발 배포 경로를 사용한다. 실제 dev API에서 한·영·일 잠금/해금 결과, 기존 snapshot 보존, 새 문구 일치, 사용자 간 멱등성을 확인하고 전용 검증 계정을 정리한다. 현재 생성 문서·원고 검수 기록·실행 자산의 재생성 일치, Ruff 및 OpenAPI 검사가 통과했다. CI·실제 API 실행 결과는 해당 PR에 구분해 기록한다.

개인 AWS 계정, 로컬 앱·Docker·DB·pytest는 사용하지 않는다. 병행 중인 대화 작업 폴더와 모바일 폴더도 수정하지 않는다. 개발 배포 확인 후 앱 담당자가 배포 커밋이 반영된 backend 작업 폴더의 OpenAPI 스냅샷을 동기화한다. 실제 모바일 화면 검증과 운영 배포는 이번 서버 작업 범위에 없다.

개발 배포를 되돌릴 때에는 이전 dev 커밋 `e02a8f67`의 이미지를 기존 배포 워크플로로 선택할 수 있다. 이번 변경은 DB 구조와 저장본 형식을 바꾸지 않으므로 역마이그레이션은 필요 없다. 되돌린 서버는 잠금 응답에서 다시 `flow`를 생략하므로 앱의 nullable 호환 처리를 유지해야 한다. 당일 생성된 새 문구 snapshot은 롤백을 이유로 지우지 않는다.
