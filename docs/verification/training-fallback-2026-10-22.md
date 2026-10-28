# 트레이닝 AI 장애 자동 통과 검증 (2026-10-22)

사용자 최종 결정: AI 10초 제한 초과·제공자 오류·해석할 수 없는 응답은 기회 차감 없이 자동 통과한다. 기존 정책을 유지하며 오류 화면이나 사용자 재시도는 추가하지 않는다. 짧은 답·복사 답 규칙 검사는 그대로다. 정상 AI 불통과는 기존처럼 기회를 차감한다.

## 변경

- `judge=fallback` 판정과 답/진행/보상 저장 경로는 기존 구현을 유지한다. AI가 쓴 개별 힌트는 없으며, 제공되는 풀이는 원고의 고정 설명이다.
- fallback 로그에 단계·언어·모델·오류 종류·소요 시간을 추가한다. 답안 원문·사용자 ID·제공자 예외 메시지는 로그에 남기지 않는다.
- 기존 모바일 응답 계약에 변화가 없어 설치된 dev 앱으로 검증할 수 있다. 이번 작업에서는 앱을 수정하거나 다시 설치하지 않는다.

## 원인에 대한 근거와 한계

이전 19단계 실패 시 사용량 원장은 provider 호출 이후 `foreground_cancelled` / `unknown_usage`였다. 단순 DB 완료 기록 지연이나 잘못된 정답이라는 근거는 없다. 동일 19단계 예시를 DB 원장 없이 별도 3회 호출했을 때 모두 통과했으며 2.045초·7.259초·1.185초였다. 응답 지연 편차는 확인했지만 제공자 내부 원인까지 특정한 것은 아니다. 이 변경은 제공자 시간 초과 자체를 없앤다고 주장하지 않는다. 사용자에게 불이익 없이 자동 통과시키는 기존 방어를 검증한다.

## 실행한 검증

- `uv run pytest -q --ignore=tests/integration`: 3,468 passed, 13 warnings.
- `MOLY_SCHEMA_TEST_DSN=postgresql://postgres@localhost:5433/moly_schema_training uv run pytest -q tests/integration/test_trainings_local.py`: 11 passed.
- `uv run ruff check app worker scripts tests db`: 통과.
- `uv run python -m scripts.openapi_contract --check`: 통과.
- `git diff --check`: 통과.

DB 테스트는 기존 로컬 PostgreSQL의 `moly_schema_training`을 재사용했다. 과거 `moly_schema_training_revision`은 삭제되어 첫 실행이 연결 실패했다. 남아 있는 테스트 DB에 누락된 기존 additive `training_submissions.sql`만 적용하고 strict schema contract 일치를 확인한 뒤 실행했다. dev/운영 스키마 변경은 없다.

실제 독립 DB 세션에서 timeout(실제 deadline 취소)·제공자 예외·응답 형식 오류 각각에 대해:

1. 기존 오답으로 기회 4개인 상태에서 완료 보상 단계를 제출.
2. 채점 중 같은 요청 7개는 처리 중 응답. 첫 요청만 채점한다.
3. 자동 통과 후 같은 요청 8개를 동시에 재전송해 동일 판정 반환.
4. 새 요청 ID로 이미 통과한 단계를 제출하면 거절.
5. 기회 4개와 충전 시작 시각 유지. 답/제출 영수증/해당 보상/건초 거래가 각 1건이며 잔액은 보상만큼 한 번 증가. 완료 진행과 답 원문 보존.

이 검증은 장애를 주입한 코드/DB 통합 테스트이며 실제 OpenAI 장애를 발생시킨 시험은 아니다. 폰의 수동 UI 테스트는 사용자가 수행한다.
