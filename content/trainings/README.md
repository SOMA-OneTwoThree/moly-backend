# 승인된 트레이닝 원고

2026-10-21 사용자 제공 원고와 승인 구성: 긍정 회로 / 자기 수용, 각각30단계.

- `arrangement.json`: 승인된 한국어 단계·출처 번호·편집 기록. 무디 3.15.2 실제 서버 응답에서 확인한 답 방식·일기·문장 지우기 위치를 따른다. 무디의 숨겨진 채점 규칙을 복원했다는 뜻은 아니다.
- `ko-source.json`, `translations.tsv`: 순서가 고정된 원문과 영어·일본어 대응 번역. TSV 첫 열은 원문 배열 인덱스다.
- `rubrics.tsv`: 서버 전용 단계 기준과 평가용 예시. 화면에는 전송하지 않는다. 2026-10-22 요청으로 26개 기준을 뉘앙스 중심으로 완화했다. 예시는 회귀 평가에만 사용하고 AI 채점 프롬프트에는 보내지 않는다. 상황에 맞는 긍정적/자기수용적 방향 하나면 충분하며 상세 해설이나 특정 문구를 요구하지 않는다.
- `grading-eval.json`: 모범답안과 독립적으로 쓴 26단계의 짧은 자연어 답·명백한 반례·사용자 QA 재현. `grading-eval-holdout.json`: 기준 작성 후 추가한 별도 표현으로 검증한다. `scripts.evaluate_training_grading --suite all|natural|heldout`으로 실제 모델 평가를 명시 실행한다.
- `legacy-catalog.json`: 옛 원고. 새 카탈로그에서 archived 처리하므로 목록에는 안 나오고, 옛 id의 진행/답 조회는 유지한다.
- `build_catalog.py`: 실행용 `app/resources/trainings/catalog.json` 생성. 새 문제는 새 id로 분리했다. 2026-10-22 승인으로 활성 두 과정 보상은 30/60/150. 기존 지급 이력은 재지급하지 않는다.

```sh
uv run python content/trainings/build_catalog.py
uv run python content/trainings/build_catalog.py --check
uv run pytest -q tests/test_training_catalog.py tests/test_training_grading.py
```

원고별 객관식/주관식: 긍정 회로13/17, 자기수용21/9. 같은 사례를 다른 형식으로 다시 연습한다.
2026-10-22 사용자 QA에 따라 16개 단계 해설에서 보기 번호·부가 평가·반복 설명을 제거했다. ko/en/ja 원고에 함께 반영했다.
자기수용18은 원본처럼 빈칸 없는 일반 객관식이다. 앱은 통과/재열람에서 선택한 답을 별도 표시한다.

무디의 `review_type` 값은5/15/30에 있지만 이를 자동 답변 모아보기로 단정하지 않는다.
앱은 보상 후 다음 단계/완료로 진행한다. 2026-10-22 요청으로 하단 기록 모아보기 진입은 제거하며, 완료한 단계의 답 재열람은 유지한다.

## 출시 순서

1. `db/changes/explore_training.sql`이 이미 적용된 환경에서 `db/changes/training_submissions.sql`을 검토·적용한다. 기존테이블 초기화나 계정 데이터 전환은 없다.
2. DB 계약을 검증한 뒤 이 백엔드와 앱을 배포한다. 빈칸 없는 선택형을 제공하므로 해당 앱 수정도 함께 필요하다.
3. 동시 제출의503 `TRAINING_SUBMISSION_PENDING`은 같은 요청 id로 재시도한다. 서버 중단 시30초 선점 만료 후 다시 채점한다. 결과가 있으면 최초 판정을 재사용한다.
4. 롤백은 서버/앱 버전을 되돌리되 새 테이블·저장 답을 삭제하지 않는다. 구버전 서버에는 이번 멱등 오류 수정이 없으므로 롤백 시 해당 보장이 사라진다.

2026-10-21 dev DB에 `training_submissions.sql`을 guarded dry-run → SHA-256 확인 → commit 순서로 적용했다. `uv run python -m db.verify --env dev`의 전체 계약 검증 통과. 운영 DB와 배포 서버는 변경하지 않았다. 로컬 PostgreSQL17 통합 검사에 이어 dev DB·실제 AI·iOS 시뮬레이터로 검증한다.
실제 AI 평가는 `uv run python -m scripts.evaluate_training_grading --output /tmp/training-grading.json`으로 명시 실행한다. 합성 답만 보내며 계정·DB 기록은 사용하지 않는다. 모델 실패를 평가 통과로 세지 않는다.

## 문제 그림 (2026-10-21)

`arrangement.json`의 단계별 `icon_key`는 앱에 이미 포함된 Fluent Emoji 3D 에셋 키다. 두 과정 60개 모두 상황에 맞춰 지정했다. 생성 원고와 공개 API로 전달하며 언어에 따라 바뀌지 않는다. 예전 과정의 누락 키는 그림 없음으로 처리한다. 무디 원본 그림을 복사하지 않는다.
