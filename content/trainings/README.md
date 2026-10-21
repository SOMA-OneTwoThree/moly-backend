# 승인된 트레이닝 원고

2026-10-21 사용자 제공 원고와 승인 구성: 긍정 회로 / 자기수용, 각각30단계.

- `arrangement.json`: 승인된 한국어 단계·출처 번호·편집 기록. 무디 3.15.2 실제 서버 응답에서 확인한 답 방식·일기·문장 지우기 위치를 따른다. 무디의 숨겨진 채점 규칙을 복원했다는 뜻은 아니다.
- `ko-source.json`, `translations.tsv`: 순서가 고정된 원문과 영어·일본어 대응 번역. TSV 첫 열은 원문 배열 인덱스다.
- `rubrics.tsv`: 서버 전용 단계 기준과 통과 예시. 화면에는 전송하지 않는다. 사용자가 요청한 주제별 채점으로 작성한 우리 기준이다.
- `legacy-catalog.json`: 옛 원고. 새 카탈로그에서 archived 처리하므로 목록에는 안 나오고, 옛 id의 진행/답 조회는 유지한다.
- `build_catalog.py`: 실행용 `app/resources/trainings/catalog.json` 생성. 새 문제는 새 id로 분리했다. 보상은 기존15/40/100 유지.

```sh
uv run python content/trainings/build_catalog.py
uv run python content/trainings/build_catalog.py --check
uv run pytest -q tests/test_training_catalog.py tests/test_training_grading.py
```

원고별 객관식/주관식: 긍정 회로13/17, 자기수용21/9. 같은 사례를 다른 형식으로 다시 연습한다.
자기수용14의 해설은 보기 편집으로 원문 번호가 맞지 않아 빌더에서 수정했다.
자기수용18은 원본처럼 빈칸 없는 일반 객관식이다. 앱은 통과/재열람에서 선택한 답을 별도 표시한다.

무디의 `review_type` 값은5/15/30에 있지만 이를 자동 답변 모아보기로 단정하지 않는다.
앱은 보상 후 다음 단계/완료로 진행한다. 기존 수동 기록 모아보기는 유지한다.

## 출시 순서

1. `db/changes/explore_training.sql`이 이미 적용된 환경에서 `db/changes/training_submissions.sql`을 검토·적용한다. 기존테이블 초기화나 계정 데이터 전환은 없다.
2. DB 계약을 검증한 뒤 이 백엔드와 앱을 배포한다. 빈칸 없는 선택형을 제공하므로 해당 앱 수정도 함께 필요하다.
3. 동시 제출의503 `TRAINING_SUBMISSION_PENDING`은 같은 요청 id로 재시도한다. 서버 중단 시30초 선점 만료 후 다시 채점한다. 결과가 있으면 최초 판정을 재사용한다.
4. 롤백은 서버/앱 버전을 되돌리되 새 테이블·저장 답을 삭제하지 않는다. 구버전 서버에는 이번 멱등 오류 수정이 없으므로 롤백 시 해당 보장이 사라진다.

운영/개발 DB에는 이 작업에서 적용하지 않았다. 로컬 PostgreSQL17 폐기용 DB에서만 생성·통합 검증했다.
실제 AI 평가는 `uv run python -m scripts.evaluate_training_grading --output /tmp/training-grading.json`으로 명시 실행한다. 합성 답만 보내며 계정·DB 기록은 사용하지 않는다. 모델 실패를 평가 통과로 세지 않는다.
