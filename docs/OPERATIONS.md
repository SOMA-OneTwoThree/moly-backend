# 운영과 장애 대응

이 문서는 반복해서 사용하는 배포·관측·복구 절차다. 실행 시점의 SHA, 환경 설정, DB 계약과 실제 검사 결과는 해당 PR·배포 기록에 남긴다. 코드 기본값이나 과거 영수증을 현재 운영 상태로 간주하지 않는다.

## 일일 보고

`worker/daily_report.py`와 `worker/monitoring.py`는 가입·활동·구독·일기를 독립 조회한다.
실패 항목을 0으로 바꾸지 않는다. 활동 조회의 PostgreSQL 시간 초과(57014)는 새 세션에서
한 번만 재시도하며 전체 모니터링 15초 제한과 취소를 유지한다. 오류 로그는 항목·대상일·
오류 종류·SQLSTATE·소요 시간·시도 횟수만 기록한다. 푸시는 수락·무효 토큰·전송 오류·
설정 오류를 구분하고, AI 비용은 확인된 사용량 합계·집계 대기·사용량 미확인을 구분한다.

배포 workflow는 개발 컨테이너와 운영 두 호스트에서 아래 읽기 전용 검사를 실행한다.
DB 연결에 읽기 전용을 강제하며 snapshot 저장·Slack 전송 없이 실제 보고를 생성하고
필수 지표·조회 실패 여부·버전을 검사한다. 운영 검사는 두 호스트 정상 등록 후 실행한다.
점검 스크립트는 Dockerfile에서 이미지에 포함하며 새 이미지 배포에서는 누락·실행 실패를
배포 실패로 처리한다. 기존 이미지 태그를 지정하는 복구 배포는 구버전 호환을 위해 이 검사만 제외한다.

```sh
python scripts/check_daily_report.py --environment development --expected-sha <40자리 SHA>
python scripts/check_daily_report.py --environment production --expected-sha <40자리 SHA>
```

이 보고 수정에는 DB 스키마·운영 데이터 변경이 없다. 이전 이미지 태그를 기존 배포 workflow에
지정해 복구한다.

## 실행과 환경 분리

| 프로세스 | 실행·역할 | 확인 기준 |
|---|---|---|
| API | 같은 릴리스 이미지의 HTTP 서버 | `/health`의 환경·SHA와 `/health/ready`의 DB 상태 |
| consumer | 같은 이미지의 상주 작업 소비자 | 실행 이미지·프로세스·큐 진행 상태. 공개 health로 대신하지 않음 |
| worker | systemd timer가 실행하는 배치 | worker 소유 호스트·timer·작업 종료와 마지막 성공 시각 |

- 운영은 `main` push의 [deploy.yml](../.github/workflows/deploy.yml), 개발은 `dev` push의 [deploy-dev.yml](../.github/workflows/deploy-dev.yml)을 쓴다. ECR·OIDC 역할·대상 태그·동시 실행 그룹이 분리되어 있다. 개발 배포는 단일 호스트 중단 배포다.
- 개인/default AWS 계정은 프로젝트 계정이 아니다. 확인된 프로젝트 OIDC·기존 접속 경로만 사용한다. 개발 검증을 새 EC2 SSH 접근이나 로컬 DB 생성으로 바꾸지 않는다.
- `backend.env`는 인프라가 환경별 SSM에서 만든다. 생성 파일을 임의로 고치거나 운영 설정·FCM 자격을 개발에 복사하지 않는다. `/etc/moly-env`와 실제 환경·설정 출처를 함께 확인한다.
- worker 소유권은 `/etc/moly-worker-host`가 결정한다. **development라는 이유로 worker가 없거나 꺼져 있다고 가정하지 않는다.** 개발 workflow는 기존 worker 소유권과 설정 지문을 보존하고, API·consumer의 `development` 환경 및 배포 SHA를 검사한다.
- API·consumer·worker의 모델/SDK/DB 연결 풀은 프로세스별이다. 풀 변경 시 전체 호스트·롤링 중 구/신 프로세스 합계를 계산한다.

## 배포 순서와 복구

### 기억 보존·위기 가드레일의 개발 검증

새 기능 기본값은 끔이다. 도입할 때는 전역 설정으로 전체 사용자에게 한 번에 적용한다.
사용자 목록·비율·A/B 시험으로 적용 대상을 나누지 않는다. 기존 파생 정보의 원문 근거와
전체 사용자 자료 호환 준비는 전역 도입 전에 끝내며, 사용자별 공개 절차로 대신하지 않는다.

다음 검증은 기존 개발 Supabase만 허용한다. 실제 모델 호출은 별도 설정이 필요하며 비용이 발생한다.
합성 원문·진행 정보 시험은 rollback하고, 실제 개발 API 시험은 자체 생성한 인증 계정만 사용한 뒤
설정 복구와 계정·대화·벡터 기억 삭제를 확인한다. API 시험은 기대 배포 SHA와 고정 개발 주소를 검사한다.
개발 환경의 기존 전역 판정 정책·제한 시간이 다르면 시험 도구가 임의로 바꾸지 않는다.

```bash
MOLY_CHECKPOINT_TEST_ENV=dev uv run pytest -q \
  tests/integration/test_context_retention_dev.py \
  tests/integration/test_checkpoint_sweep_resume_dev.py \
  tests/integration/test_context_pressure_dev.py \
  tests/integration/test_contract_fallback_dev.py \
  tests/integration/test_checkpoint_packing_dev.py
MOLY_CHECKPOINT_TEST_ENV=dev MOLY_RETENTION_MODEL_TEST_ENV=dev uv run pytest -q \
  tests/integration/test_retention_model_dev.py \
  tests/integration/test_checkpoint_packing_model_dev.py
uv run python scripts/replay_guard_context_dev.py --env-file <개발-env-경로> \
  --input <저장소-밖-비식별-검토완료-입력.json> --expected-sha <개발-배포-SHA> --execute-dev
```

재현 입력과 복구 기록은 저장소 밖에 소유자만 읽을 수 있게 둔다. 입력 출처는 비공개 기록에
정확히 보존하고, 공유 결과에는 원문·식별자 대신 검사 수와 오류 유형만 남긴다. 시험이 중단되면
같은 스크립트의 `--cleanup-state <복구-기록> --expected-sha <SHA> --execute-dev`로 정리한다.
Auth 삭제 성공만으로 완료 처리하지 않고 개인정보 처리 장벽의 삭제 완료 및 잔여 0을 두 번
확인한다. 실패 시 복구 기록을 보존한다. 개발 배포 전 `db/changes/guard_context_v2.sql`을
개발 DB에 검토 적용한다. 운영 적용·운영 PR은 이 절차에 포함하지 않는다.

재현 도구는 해당 실행의 소유 표시가 있는 시험 계정에 한해 최초 대기 기억 작업의 예약 시각을
앞당길 수 있다. 실패 재시도 간격·횟수는 바꾸지 않는다. 삭제는 같은 개발 DB에서 기존 사용자별
삭제 처리 함수를 호출해 마무리할 수 있다. 이를 평상시 3분 대기·주기 실행의 지연 측정으로
해석하지 않는다. 작은 같은 날 대화에는 계약 작업이 예약되지 않을 수 있으므로 모든 대화에서
계약 커서 최신화를 강제하지 않는다. 실제로 예약된 계약·요약은 종료를 기다리고, 게시된 요약의
원문 제외는 서버의 사실·계약 완료 조건을 그대로 검증한다.

`require_checkpoint`는 실제 검증 게시와 원문 보존을, `require_recovery`는 저장된 최신 정리 목표가
해제되고 검증 게시가 그 목표를 덮는지까지 구분해 검사한다. 선제 정리만으로 오류를 피한 경우와
실제 입력 초과 후 재요청으로 복구한 경우는 HTTP 결과와 재요청 수로 따로 보고한다.
예약된 작업이 없다는 이유만으로 미예약 목표를 완료 처리하지 않는다.
자동 주석의 질문 유형 차이는 명시한 입력에 한해 기록하고 후속 턴을 계속 검사할 수
있지만, 원래 기대값·불일치·최종 비성공 상태는 유지한다. 이를 전체 기대 일치로 합산하지 않는다.

2026-11-03 문구 정책 변경 후에는 `self_thought`와 `meaning_unclear` 모두 같은 고정 뜻 질문을
기대한다. 방법·양 요청의 거절 접두사는 유지한다. 분류 선택지·확률·분기는 변경하지 않는다.
새 배포의 문구 평가와 기존의 두 질문 정책 평가를 분리하고, 과거 질문 선택 불일치 기록은
지우거나 소급해 성공으로 바꾸지 않는다. 출력 문구가 같다는 사실을 두 분류가 일치했다는
검증 결과로 해석하지 않는다.

뜻을 묻는 고정 질문 뒤의 답은 질문 문구 자체가 아니라 앞선 사용자 발언과 연결해 판정한다.
같은 생각이 현재도 이어진다고 밝히고 농담임을 부정한 경우에는 실행 계획이 없어도 확인된 답으로
본다. 짧은 동의만 있거나 화제를 바꾼 답은 이 조건에 포함하지 않는다.
주어·현재성·비유 여부가 해소되지 않은 단독 죽음 표현은 앞선 주제가 일상이라는 이유만으로
일반 대화로 확정하지 않는다. 문맥으로 관용·비유·인용·타인·창작·해결된 과거가 분명하면 기존
일반 대화 분류를 유지한다. 일상 사건이 이유인 진지한 현재 자기 생각은 생각 표현으로 분류한다.

원문 생략은 메시지 식별 정보와 검증된 처리 범위를 함께 확인한다. 전환한 사용자의 읽기에서
구형 위기 압축이 실행되지 않는지, 날짜·이름 변환과 도구 결과 추가 뒤에도 미처리 원문이 유지되는지 확인한다.
유효한 일반 판정에서는 과거 `safety`·`safety_check`의 양쪽 메시지가 일반 입력과 선발화 system에
없는지 검사한다. 판정용 최근 6개·현재 입력·일반 메시지와 원문 식별 정보는 유지해야 한다.
판정 장애·시간 초과에는 원문 fallback을 보존하고, 고정 안내 경로의 입력과 차감은 바꾸지 않는다.
신규 일반 요약의 `packed-v1`은 실제 입력 크기에 맞춰 여러 원자 조각을 한 호출로 처리한다.
구형 작업과 완성 초안의 재개는 기존 방식이며, 새 root에만 버전을 붙인다. 아직 호출하지 않은
ready 작업의 범위를 합칠 때도 기존 버전을 유지하고, 진행·실패 예산·게시 초안이 있는 작업은
재계획하지 않는다. 확대 출력 잘림·OFF/ON 뒤의 동일 시작 위치와 남은 예산, 원문 전 구간의
근거 위치·해시 및 내용 보존을 검사한다. 원자 수를 실제 모델 호출 수로 보고하지 않는다.
묶음 전용 시스템 문구가 바뀌면 기존 packed 진행·완성 초안의 해시가 맞지 않아 중단될 수 있다.
배포 전에 해당 작업을 확인하고, 자동 호환이나 모델 호출 없는 게시를 가정하지 않는다.
선호 정리의 확대 입력·출력 잘림 복구는 기존 배경 상한과 실패 예산 안에서 시험한다. 이 보완에는
새 DB 변경이나 계정 API 배포가 필요하지 않다.
정리 대기의 완료 알림은 현재 DB 상태를 다시 확인하는 신호다. 선행 처리만 기다리던 게시 작업의
`available_at`만 앞당기고 실제 오류·실행 중 작업·실패 예산은 보존하는지 검사한다. 선행 처리 완료와
게시 작업의 대기 등록 순서가 바뀌어도 다음 처리 시도 뒤 신호로 재확인하며, 신호 실패 때는 기존
5초 대기가 남는다. 새 처리 목표가 기존 고정 요약보다 커도 최초 기억 작업의 기본 대기를 깨워야 한다.
빠진 기억 연결의 보완은 기존 활성 작업과 같은 커서의 종료 작업이 없을 때만 원래 키로 예약한다.
같은 요청 내 복구는 정상 입력 준비 단계에 한 번만 허용한다. 일반 요청 8초와 정리 대기 요청
12초를 구분해 측정한다. 12초는 최초 요청부터의 전체 상한이며 매 대기나 모델 호출마다 늘리지 않는다.
동시 동일 키·클라이언트 취소·처리 권한 교체·날짜 및 개인정보 변경에서 중복 저장·차감이 없는지,
정리 진전 후 기억과 선호를 새로 읽는지, 판정과 질문 임베딩은 한 번인지 함께 확인한다.
예약 당시 목표에 도달하기 전의 부분 게시가 재준비 기회를 소비하지 않는지 확인한다. memory/content의
빈 조회 대기는 최대 1초이며 다른 큐·슬롯·실제 실패의 available_at은 유지한다. 기본 설정에서 소비자
하나의 두 빈 큐 조회는 합계 초당 약 2회이고 완료 알림에 의한 추가 조회는 별도다.
개발 검증은 `tests/integration/test_pressure_handoff_dev.py`의 기존 개발 DB rollback 시험을 포함한다.
회상 검증은 전체 registry revision이 아니라 실제 제공한 기억과 원출처를 기준으로 시험한다.
순수 정리 변경은 통과하고 내용·원출처·활성 상태·표시 날짜 변경은 차단해야 한다. 최종 검증 뒤
기억 수정·원문 수정·새 출처 삽입은 대화 커밋까지 기다리는지 별도 두 트랜잭션으로 확인한다.
반대 잠금 순서와 제한 시간 초과는 500이나 대기 고착 없이 503으로 처리하고 메시지·차감의 부분
저장이 없어야 한다. 관련 단위 검사는 `tests/test_provided_memory.py`, DB rollback 검사는
`tests/integration/test_provided_memory_dev.py`에 있다. 임시 개발 진단 응답과 선택적 검색 시간 단축은 남기지 않는다.

중단할 때는 새 게시·작성 설정을 끈다. 이미 전환한 사용자의 v5 읽기나 확장 종류·컬럼을 지우거나
구형 읽기로 되돌리지 않는다. 계약 작업은 같은 목표 이상인 진행 중 작업이 있으면 중복 예약하지
않는다. 처리 중 목표보다 큰 새 목표는 별도 작업이 생겨 모델 호출이 겹칠 수 있으며, 최종 커서와
원문·세대 재검사로 늦은 결과의 게시를 막는다. 이를 단일 모델 호출 보장으로 설명하지 않는다.

### PR 전

1. 최신 대상 브랜치에서 필요한 변경만 통합한다. 다른 작업의 미커밋 원본과 미추적 사용자 문서를 보존한다.
2. DB 차이를 `db/schema.sql`, 모델과 `db/schema_contract.json`에 맞춰 검토한다. 기존 정책 RPC·seed·설정을 통째 복사하지 않는다. [DB 절차](../db/README.md)를 따른다.
3. 기존 개발 API·Supabase에서 가능한 계약을 검증한다. 가짜 SDK/FCM/Slack, rollback DB 시험, 실제 모델·기기 결과를 구분한다. PostgreSQL sequence는 rollback 후에도 전진할 수 있다.
4. 같은 최종 snapshot으로 검증·수정을 마친 후 PR을 만든다. CI가 동작하도록 먼저 PR을 열고 실험 커밋을 반복하지 않는다.

```bash
uv run pytest -q --ignore=tests/integration
uv run ruff check app worker scripts tests db
uv run python -m scripts.openapi_contract --check
uv run python -m db.schema_contract --check-generated
git diff --check
```

`check-generated`의 DB 준비 방식은 CI와 [DB 절차](../db/README.md)를 따른다. 이 명령을 실행하려고 사용자 작업 환경에 임의 DB를 새로 띄우지 않는다.

### 리뷰 후 머지 직전

1. 대상 main SHA·최종 PR head·CI 통과와 진행 중 배포를 확인한다. 배포 concurrency를 우회하거나 진행 중 롤링을 취소하지 않는다.
2. 이전 이미지의 **tag와 digest**, 인프라 버전·설정 복구 기준을 확보한다. 복구 이미지를 새로 빌드하지 않는다. 과거 문서의 고정 SHA를 복구 기준으로 재사용하지 않는다.
3. 자동 배포가 새 스키마를 요구하면 리뷰한 additive SQL을 먼저 dry-run/검증하고, 승인된 대상에만 적용한다. 이전 이미지 호환·사후 DB 계약·health를 확인한 뒤 머지한다. 그 외 차이가 있거나 SQL 실패면 머지하지 않는다.

```bash
git ls-remote origin refs/heads/main
gh run list --workflow deploy.yml --limit 5
# 아래 경로·해시는 해당 PR에서 검토한 실제 값으로 지정한다.
RELEASE_SQL='<리뷰한 SQL 경로>'
REVIEWED_SQL_SHA='<리뷰한 SHA-256>'
uv run python -m db.verify --env prod
uv run python -m db.apply "$RELEASE_SQL" --env prod --expected-sha256 "$REVIEWED_SQL_SHA"
# 기본 실행은 ROLLBACK. 검토·승인된 적용 단계에서만 다음 명령을 사용한다.
uv run python -m db.apply "$RELEASE_SQL" --env prod --commit --allow-prod --expected-sha256 "$REVIEWED_SQL_SHA"
uv run python -m db.verify --env prod
```

### 자동 배포와 사후 확인

- main push는 이전 ECR tag/digest, 두 대상의 TG healthy 상태, 구버전 API/consumer SHA·ready, 아침 푸시 runtime/SSM 값과 FCM 인증, worker timer 한 대를 **모든 호스트에서 먼저** 검사한다. 실패·unknown이면 deregister 전 중단한다. 앞선 이미지 빌드·ECR 게시·인프라 파일 배치까지 읽기 전용이라는 뜻은 아니다.
- 이후 한 대씩 deregister/drain → 배포 → ready → register한다. API/consumer가 모두 최종 SHA인지, 대상 수·TG 상태와 worker timer를 로그에서 확인한다. timer 검사는 중복/잔류 `worker.tick`, 인프라·ENV 복구본 확보까지 증명하지 않는다.
- 인프라 배포는 후보 환경·FCM/이미지와 읽기 전용 DB preflight를 준비한 뒤 live 환경 파일을 교체한다. preflight 실패 때 기존 파일을 보존하지만, 컨테이너 교체 이후 모든 실패가 자동 복구되는 것은 아니다.
- protected synthetic는 실제 모델 호출과 비용을 발생시킨다. 단순 읽기 감사용 health처럼 임의 호출하지 않는다. 공개 health·DB ready·consumer·worker·synthetic 결과를 각각 기록한다.
- 현지 04/09/10/20시 경계는 전 세계 사용자에게 분산된다. 한국·일본 주요 시각을 피하는 창도 글로벌 무발송 시간은 아니다.

```bash
DEPLOY_RUN_ID='<이번 배포 run id>'
gh run watch "$DEPLOY_RUN_ID" --exit-status
curl --fail --silent --show-error https://voice.moly.asia/health
curl --fail --silent --show-error https://voice.moly.asia/health/ready
```

실패하면 확대 배포·flag 변경을 중단한다. 같은 workflow의 `workflow_dispatch`와 검증된 이전 `image_tag`로 복구하고 이전 환경·인프라와의 호환을 확인한다. `expected_instances=1`은 정상 두 대 구성을 대신하는 기본값이 아니라 명시적인 비상 절차다. 새 nullable 컬럼·원장·snapshot은 기본적으로 남긴다. 쓰기 형식 변경이 있었다면 이전 이미지가 새 저장본을 읽을 수 있는지 먼저 확인한다.

## 설정과 프로세스

- 설정은 env/SSM과 `app_config`의 소유 범위를 구분한다. 기본값·DB 값·현재 프로세스 실효값은 서로 다르다. flag 변경은 기존 값을 읽고 필요한 키만 변경하며 전체 JSON을 덮지 않는다.
- `context_checkpoint_temporal_enabled`를 바꾸기 전 API/consumer 버전, 진행 중 작업과 구/신 checkpoint 읽기 호환을 확인한다. 롤백 전에 새 쓰기를 끄더라도 기존 저장본이 사라지지는 않는다.
- worker는 15분 단위 슬롯에서 사용자 현지 시간과 일 경계를 해석한다. tzdata와 legacy timezone alias를 유지한다. 잘못된 타임존을 임의 UTC로 처리해 발송 시각을 바꾸지 않는다.
- worker의 사용자 처리 soft deadline과 systemd hard timeout을 함께 보존한다. 운영 알림 emit에는 전체 15초 제한이 있어 남은 관측·통지가 다음 tick으로 밀릴 수 있다. 외부 취소는 전파한다.

### 모델·사용량·비용

- 역할 기본값은 `app/config.py`의 chat/utility GPT-6 Luna, diary GPT-6 Sol이며 기억 추출·임베딩은 별도 설정이다. 환경 override가 있으면 실효값을 확인한다. 모델 변경에 사용자 등급·차감 가중치·구독 한도를 함께 바꾸지 않는다.
- 호출별 추론·출력 상한·전체 deadline을 함께 본다. tool 결정 상한과 일반 답변 상한은 다르며 전역 `llm_max_tokens`만으로 첫 호출 한도를 설명하지 않는다. 짧은 utility 출력에서 추론 토큰이 상한을 소진할 수 있으므로 호출부의 `reasoning_effort` 계약을 확인한다.
- 사용자 가중 차감과 회사 원가를 구분한다. 입력·cache read·cache write는 배타 버킷으로 계산하며 명시된 write 0을 추정값으로 바꾸지 않는다. 미보고 cache write의 보수적 추정에는 표시를 남기고 usage 유실을 0원으로 처리하지 않는다.
- 원가는 `ai_price_catalog`의 해당 시점 유효 행·버전으로 계산한다. 과거 원장·단가를 덮거나 단순 token 합을 청구서 금액으로 표시하지 않는다. 새 모델의 실비·지연·도구 왕복·일기/검수 회귀를 확인한 뒤 전환하고, 복구 때도 가격 행과 원장을 보존한다.

## 사용자 푸시

원본: [notify.py](../app/services/notify.py), [push.py](../app/services/push.py), [문구](../app/services/push_copy.py), [worker](../worker/tick.py).

| 사용자 현지 시각 | 경로 | gate·날짜 |
|---|---|---|
| 09시 | 루틴 아침 | `push_settings.routine_morning`, `evening_chat` 사용자 설정. 루틴의 달력일 |
| 10시 | 발행된 미열람 일기 | `morning_push_enabled`, 아침 일기 사용자 설정. 전날 활동일의 발행본 |
| 20시 | 공지 우선 → 루틴 스트릭 위험 → 일반 대화 | `evening_chat`; 위험 경로는 `push_settings.routine_risk`. 대화 활동일 |

- `push_settings`의 두 루틴 키는 미설정·조회 실패 시 false다. global 아침 기본값도 false지만 **이미 활성화된 환경을 기본값으로 덮지 않는다.** 신규 활성은 기존 승인·관측 계획에 따른 별도 단계다.
- 저녁 `diary_teaser` 경로는 없다. 아침 일기 알림을 끈 사용자는 일기 푸시를 받지 않는다. 두 루틴 flag가 false여도 10시·20시 일반 분기나 문구가 이전 정책으로 돌아가지 않는다.
- 루틴 보상일은 현지 00시, 일기·대화 활동일은 04시 경계다. DST는 다음 현지 날짜로 계산한다.
- 최근 60분 억제는 `chat_contexts.last_active_at` 기준이다. 실제 마지막 사용자 메시지 시각과 같은 지표라고 쓰지 않는다.
- 유효기한은 `min(발송 시각+2시간, 현지 21시)`다. collapse/deeplink 유형과 사용자 설정을 함께 검증한다. 앱에서 열 수 있는 경로인지 구버전도 확인한다.
- 날짜별 DB claim은 재발송 중복을 막는다. 아침은 인증 준비 후 claim한다. FCM 시도 후에는 결과를 잃었어도 claim을 풀어 즉시 재전송하지 않는다. FCM 수락은 실제 기기 수신이 아니다.
- 저녁 claim 때 `evening_push_variant=NULL`, `sent>0`일 때만 best-effort 저장한다. 기록 실패/프로세스 종료는 인사 연결 누락으로 남을 수 있다. 다음 대화 첫마디 연결은 푸시 클릭만을 요구하는 계약이 아니다.
- 실제 기기 전달·앱 deeplink/첫마디는 mock 시험과 별도다. 개발 환경의 기존 worker·자격을 임의로 바꿔 시험하지 않는다.

### FCM 무효 토큰 비활성 표시

FCM 구조화 오류 코드를 기준으로 분류한다. HTTP 상태만으로 토큰을 폐기하지 않는다. 소유 사용자+토큰 조건으로 표시하고 재등록된 토큰은 새 활성 시각으로 복구할 수 있다. `FCM_INVALIDATE_DEAD_TOKENS` 기본값과 현재 환경 값은 구분한다.

| 로그 문자열 | 의미·조치 |
|---|---|
| `HTTP 404 UNREGISTERED` | 등록 해제 토큰. 설정과 표시 집계를 함께 확인 |
| `HTTP 403 SENDER_ID_MISMATCH — 무효 토큰(표시 안 함)` | 프로젝트 불일치. 자동 비활성 표시 대상과 구분 |
| `[payload_error]` | payload 오류. 토큰 일괄 폐기 금지 |
| `[setup_error]` | 인증/프로젝트 설정 오류. 발송 확대 중단 후 설정 확인 |
| `비활성 표시 꺼짐` | 오류는 분류했지만 표시 flag가 꺼진 상태 |
| `FCM 수락 0건인데 무효 토큰` | 대량 분류 이상 경고. 실제 원인 확인 전 전체 초기화 금지 |

기존 프로젝트 호스트의 승인된 로그 경로에서 사용한다. 자격·토큰·본문을 외부에 붙이지 않는다.

```bash
journalctl -u moly-worker.service --since '1 day ago' | grep -c '비활성 표시 꺼짐'
journalctl -u moly-worker.service --since '1 day ago' | grep -c 'HTTP 404 UNREGISTERED'
journalctl -u moly-worker.service --since '1 day ago' | grep -E '\[(payload_error|setup_error)\]'
```

## 관측과 장애 대응

- JSON 로그의 `ts/level/logger/msg`와 요청 상태·단계 소요를 사용한다. health 200 access log는 기본 억제되며 `LOG_ACCESS_HEALTH`로 조절한다. httpx/httpcore DEBUG나 provider 본문·자격 로그를 켜지 않는다.
- `/health`와 `/health/ready`는 공개 상태 확인, `/health/deep`, `/health/queues`, `/health/synthetic`는 `X-Health-Token` 보호 대상이다. synthetic는 모델 비용·외부 의존성이 있는 별도 검사다.
- `AI_UNAVAILABLE` 503은 timeout/연결/일시 provider 오류뿐 아니라 잘림·빈 답·locale 필터 후 빈 답에도 사용한다. 실패 답은 대화에 저장·사용자 차감하지 않는다. `Idempotency-Key`를 유지하고 API 실패를 정상 대사로 감추지 않는다. 500은 별도 내부 오류로 조사한다.
- 첫 도구 판단 호출에서 도구·구조화 응답 없이 일반 텍스트만 `length`로 잘리면, 남은 턴 시간 안에서 도구 없이 짧고 완결된 한 문장 답변으로 한 번 재생성한다(`decide_length_fallback`). 직접 답변·복구 호출은 일반 답변 출력 예산을 쓰고, 복구 실패 시 추가 재생성 없이 기존 503 처리를 따른다. 실패 호출 원가는 AI 원장에 남지만 사용자 차감에는 성공한 복구 호출만 포함한다. 정상 도구 호출의 합산 차감·일일 한도·전체 deadline은 유지한다.
- 회상 결과 없음과 조회 실패를 구분한다. 빈 결과를 장애로 세지 않고, 실패를 사용자가 알려주지 않은 사실로 바꿔 답하지 않는다.
- 지속 사건은 `monitoring:alert:<환경>:<신호>` 상태로 신규/변화/악화/재알림/복구를 판단한다. 정상 worker 요약을 매 tick 전송하지 않는다. `daily_status`는 하루 상태 보고용이며 해당 행의 active를 장애라고 해석하지 않는다.
- 같은 미해결 사건은 6시간, 내용 변화는 1시간, 악화·재발은 5분 간격의 억제를 적용한다. 정상 재관측 때 복구를 알린다. 하루 요약은 KST 13시 이후 첫 전송 성공으로 하루 한 번이며 tick 지연·전송 실패에 따라 늦어질 수 있다. 정확히 13시에 도착한다는 보장은 아니다.
- 일일 보고는 전일 KST 가입·기능 활동 DAU/최근 30일 MAU·스토어 구독 변동과 보고 시각의 구독·정기 작업 상태를 구분한다. DAU/MAU는 남아 있는 기능 사용 기록 기준이며 앱 실행 전체나 다운로드 수가 아니다. 푸시는 FCM 수락 기준이며 기기 수신을 보증하지 않는다.
- 일기 대상·완료는 공개 예정 시각이 전일 KST에 속하는 대화와 당시 구독 기간으로 집계한다. 삭제한 일기는 누락에서 제외한다. 비용 미확정은 비용 항목에만 표시하며 정기 작업 상태와 구분한다.
- `monitoring:daily_totals`는 사용자 식별자 없이 최근 4일 실행/발송 건수만, `monitoring:subscription_totals`는 최근 3일 구독 이용자 수만 유지한다. 첫 집계일은 적용 이후 수치로 표시하며 비교 자료가 없으면 0으로 대체하지 않는다.
- 구독 기간은 현재 기간 시작과 만료, 이전 기간은 처리된 스토어 이벤트로 확인한다. 시작 정보 보정은 `python scripts/subscription_period_starts.py --env <절대경로>`로 미리보기 후 같은 SHA-256을 `--expected-sha256`에 넣어 `--commit`한다. 운영 쓰기에는 `--allow-prod`도 필요하다. 스키마 변경은 없으며 배포 후 보정을 적용한다. 코드 복구 시 검증된 기간 값은 유지하고 이전 값으로 일괄 되돌리지 않는다.
- RC SANDBOX 자체 미해결은 하루 요약에 포함한다. provider 이력 조회 불가·처리기 실패는 환경과 무관하게 장애 신호로 관측한다. RC는 PRODUCTION/SANDBOX/UNKNOWN을 나누고, 비용은 원장 상태·추정치·미확인 usage를 구분한다. cache token/billable 합계만으로 실제 청구액이나 장애를 단정하지 않는다.
- Slack 수락과 상태 ack 사이 종료는 드문 중복을 만들 수 있다. 알림은 exactly-once가 아니다. Slack 미수신도 서비스 정상의 증거가 아니다. deadman과 worker 종료 상태를 별도로 본다.
- 외국어 피드백은 원문을 보존하고 한국어 번역을 덧붙인다. 본문만 번역하며 실패 시 원문을 전달한다. Hangul-only/nonletter 판별이므로 `BGM`+한글 같은 혼합문도 모델 호출을 시도할 수 있다. 모든 한국어가 무과금이라고 보장하지 않는다.

### 큐·RC 점검

- actionable 큐의 원본은 [jobs.py의 `_STATS_SQL`](../app/services/jobs.py)이다. 성공 replay의 **모든 조상**을 제외한다. raw dead 수를 미처리 장애 수로 보고하지 않는다.
- 복구는 원본 이력을 남기고 `replay_of`로 새 작업을 연결한다. 이미 성공 replay된 과거 dead를 재실행/삭제하지 않는다.
- RC inbox의 pending/failed는 환경·나이·오류 유형·결제 권한과 함께 본다. `held:related_store_subscription_history` 같은 소유권 모호성을 sandbox라는 이유만으로 종결하지 않는다. [구독 계약](SUBSCRIPTIONS.md)에 따라 근거를 확인한다.

## 데이터 수명 주기

원본은 [retention_jobs.py](../worker/retention_jobs.py)와 계정 삭제 RPC다. 보존 기간만 보고 모든 행을 삭제하지 않는다.

| 자료 | 자동 정리 경계 |
|---|---|
| 만료 idempotency key | 만료값이 있는 항목. 영구 구매 멱등키는 제외 |
| AI 원장 | 90일 경과 completed/failed를 일별 rollup과 원자적으로 이동. started/unknown 보존 |
| 비동기 작업 | 14일 경과 succeeded/cancelled 중 replay 관계가 없는 항목. dead/관계 이력 보존 |
| 기억 추출 작업 | committed 14일·dead 90일 및 cursor/참조 조건. 미처리 작업 증거 보존 |
| RC inbox | processed 365일. pending/failed·결제/주문 이력은 같은 방식으로 지우지 않음 |
| 일기 claim·대화 lease | 만료 후 유예를 둔 정리. 진행 중 소유권을 지우지 않음 |

- 사용자 관계 이벤트는 전체 이력을 보존한다. 인덱스 사용량이 0이라는 단일 관측만으로 인덱스를 삭제하지 않는다.
- 계정 삭제는 moly-auth가 소유하며 삭제 fence/RPC와 배치 잔여 정리가 협력한다. API만 고쳐 background 쓰기를 허용하지 않는다. 삭제·취소·락·원장 처리 순서를 성능 최적화 과정에서도 보존한다.
- 기억 복구 도구·replay는 대상과 예상 영향을 dry-run으로 고정하고 실행한다. 데이터 삭제/모델·차원 변경/보관 이전을 단순 장애 해결에 묶지 않는다.

## 공용 일기 검색 벡터 공유

원본은 `diary_recall_repo.py`, `diary_shared_embeddings.py`, `recall_diaries.py`,
`worker/diary_recall_jobs.py`, `memory_sweep_jobs.py`다. 개인 일기·대화·장기 기억은 보존한다.
승인 원고와 제목/본문/검색 입력이 정확히 일치한 캐피·환영 일기만 공유한다. 같은 원고라도
벡터가 다른 과거 문서는 private로 남긴다. 모델·차원·색인 버전·원고 해시를 섞지 않는다.

- `diary_template_embeddings`: 사용자와 독립된 공용 원고와 벡터. RLS로 클라이언트 직접 접근 차단.
- `diary_recall_documents.shared_embedding_id`: 본인의 일기 연결을 유지하는 nullable 참조.
  조회는 본인 일기를 먼저 제한한 뒤 유효한 ready 공용 벡터 또는 기존 private 벡터를 사용한다.
- canonical은 최초 선정 후 고정한다. 기존 ready 벡터를 덮어쓰거나 공용행을 GC하지 않는다.
- `app_config.diary_embedding_storage_mode`: `private`(기본), `dual`, `shared` 중 정확한 문자열.
  값 오류는 실패한다. 조회는 모드와 무관하게 유효한 기존 참조를 읽는다. `private`로 설정하는
  것은 새 공유 쓰기의 중단이며, 이미 shared-only인 문서의 바이너리 롤백을 대신하지 않는다.

환영·개인·운영 원고 일기 모두 기존 발행 트랜잭션에서 검색 문서/잡을 등록한다. 본문·발행일·
운영 원고 receipt·개인 일기의 메시지 근거·중복 생성 방지는 바꾸지 않는다. 번역된 본문이 승인
원고와 다르면 private를 유지한다. 목록/상세와 아침 푸시 대상은 `diaries`를 읽으므로 공용 벡터
pending/ready가 일기 노출·알림 자격을 바꾸지 않는다. 환영 일기는 기존 행이 있으면 그대로
반환하고, 새 INSERT 전에 사용자 잠금과 삭제 장벽을 확인해 구독/탈퇴 경로의 잠금 순서를 맞춘다.
초기 dual에서는 개인/공용 embedding 작업이 동시에 진행돼 외부 호출이 한시적으로 중복될 수 있다.
실제 LLM 출력/FCM 전달은 SQL·시험 대역 검증과 별도로 구분한다.

### 적용 순서

1. 최신 코드·대상·동시 배포를 확인하고 이전 이미지와 DB 계약을 기록한다. 기존 개발 API/DB에서
   개인/공용 일기·3언어 검색 전후 동등성·CAS·삭제 경합·역복사·실제 잠금 순서를 먼저 검증한다.
2. `db/changes/diary_shared_embeddings.sql`의 SHA를 고정해 dry-run 후 적용한다. 이 SQL은
   새 테이블/인덱스/RLS와 nullable 참조만 추가한다. 데이터와 설정은 바꾸지 않는다. 객체가 이미
   존재하면 자동 건너뛰지 않고 중단하므로, 부분 적용 여부를 계약으로 확인하고 재실행을 판단한다.
3. 이전 계약과 새 계약을 모두 확인하고 호환 API/consumer를 전부 배포한다. 모드는 `private`다.
   아래 도구로 `link` 계획을 만들고 검토 후 연결한다. 기존 private는 유지된다. 이 단계에서
   가장 큰 동일 벡터 묶음을 canonical로 고정한 뒤 새 공유 쓰기를 켠다. 이미 ready canonical이
   있으면 이를 존중하므로 예상 공유량이 달라질 수 있다.
4. `dual`에서 신규 발행과 기존/복구 잡을 확인한다. 개인 일기와 다른 벡터 변형은 유지한다.
   공용 pending 잡은 `user_id=NULL`이며, 한 계정 탈퇴가 다른 계정의 공용 잡을 취소하지 않는다.
5. 검색 결과/본문/count/점수/정렬과 소유권, dangling/key mismatch/누락 0, 워커 교체와 private
   재생성 여부를 확인한 뒤 `shared`로 전환한다. 소량 `prune` 계획을 새로 만들고 검토해 적용한다.
6. 실제 payload와 재사용 공간·relation 크기·WAL·지연을 별도로 측정한다. NULL 처리만으로
   디스크 파일/할당 용량/청구액 감소를 보고하지 않는다. `VACUUM FULL`은 기본 절차에 없다.

```bash
# 기본은 조회만 한다. 환경은 실제 프로젝트 ref로 검증한다.
uv run python -m db.apply db/changes/diary_shared_embeddings.sql --env dev
uv run python -m db.apply db/changes/diary_shared_embeddings.sql --env dev --commit --expected-sha256 <SQL-SHA>
uv run python -m db.verify --env dev
uv run python scripts/diary_shared_embeddings.py plan --for-action link --env dev --output /private/link.json
uv run python scripts/diary_shared_embeddings.py link --env dev --plan /private/link.json --expected-plan-sha256 <PLAN-SHA>
# 검토한 동일 계획을 실행할 때만 --commit을 추가한다. 운영에는 별도 승인과 --allow-prod도 필요하다.
```

`link`는 private/dual, `prune`은 shared, `restore`는 private/dual에서만 쓴다. 각 작업마다
`plan --for-action <작업>`으로 별도 계획을 만든다. 파일은 대상 ref·작업·cutoff·승인 원고/벡터
지문에 묶인다. 기본 배치는 50행·10회이며 재실행은 이미 처리한 행을 건너뛴다. 처리량을 늘리기
전에 잠금/지연을 확인한다. 실제 연결·제거는 vector 값 동등성과 잠금 후 현재 원고·행 시각·
세대·삭제 epoch를 다시 확인한다. 계획 hash를 맞추기 위해 파일을 임의로 고치지 않는다.

### 중단·복구

- 초기 dual이라도 private 누락이 없는지 검증한 뒤 이전 읽기/이미지로 되돌린다. 새 객체부터 삭제하지 않는다.
- shared-only 발행 또는 private 제거 이후에는 새 쓰기를 dual로 전환 → 별도 restore 계획을
  검토/적용 → 전체 `remaining_shared_only=0`과 `legacy_rollback_ready=true` 확인 → 이전 읽기/이미지
  순서다. restore는 현재 context 세대와 다른 기존 문서도 원래 세대를 유지해 역복사한다.
  삭제 중/변경된 문서와 맞지 않는 참조는 건너뛰고 잔여 수를 숨기지 않는다.
- 공용 ready+private NULL은 정상이다. private 누락과 공용 pending/dangling/mismatch/exhausted를
  구분한다. `memory_sweep`은 공용 pending을 별도 예산으로 회수하며 오래됐다는 이유로 제외하지 않는다.
- 단위검사는 실제 SQL/복구 증거를 대신하지 않는다. 개발 통합검사는
  `MOLY_DIARY_SHARED_TEST_ENV=dev`, 절대 경로 `MOLY_DIARY_SHARED_ENV_FILE` 및 DB ref 검증이 필요하다.
  운영 DSN으로 시험하거나 새 로컬 Docker/DB로 대체하지 않는다.

## 주간 운영자 일기 DB 전환

이 절의 앵커는 일기 운영 절차의 진입점이다. 스키마와 생성 정책 원본은 [DB 문서](../db/README.md), [아키텍처](ARCHITECTURE.md), `app/services/diary_generation.py`다.

- 주간 원고는 `moly_life_ments.week_start_date/sequence_no`, 결과는 `preset_ment_id`와 배정 유일성으로 연결한다. 주간당 두 편을 언어별 검수하고, 다른 원고를 덮어쓰지 않는다.
- `diary_weekly_start_date` 미설정/null은 legacy 모드다. 설정은 유효한 주 시작일로 검증한다. 잘못된 설정·조회 실패 때 legacy로 조용히 fallback하지 않는다.
- 원고 없음/소진은 `no_entry`이며 빈 일기를 만들거나 나중에 소급 채우지 않는다. 일기 본문을 대화에서 미리 공개하지 않는다.
- 전환은 리뷰한 DB 차이 → 호환 코드 → 설정 순서다. 주간 결과가 생긴 뒤에는 legacy 이미지/설정으로 무작정 되돌리지 않고 기존 결과를 읽는 호환을 확인한다. 기존 발행본·원고·배정 이력을 보존한다.
- 개발 수동 생성도 종료된 활동일과 `force=false` 규약을 따른다. `DIARY_FORCE_NOT_ALLOWED`/`DIARY_POLICY_UNAVAILABLE`를 무시하고 우회 생성하지 않는다.
- 해당 개발 통합시험은 합성 fixture와 외부 호출 대체 범위를 확인한 뒤 기존 개발 DB에서 실행한다. 시험 자체가 실제 일기 발송·실모델 품질을 증명하지 않는다.

## 둘러보기 아티클 DB 전환

탐색 탭 운영 승격은 **`db/changes/explore_prod.sql` 한 파일을 한 트랜잭션으로 적용**한다.
생각 연습·제출 receipt·채점 이의·아티클 진행 테이블, main의 `mood_reward`를 보존한 건초
원장 type 합집합, `articles` 테이블·원고 5편(한국어·영어·일본어)·오늘의 한마디 365개가 모두 포함된다. 구독 게이트는 코드 변경이며
추가 DB 구조가 없다. 오늘의 한마디 `daily_affirmations`는 main에 있는 기능으로, 기존 행을
보존하면서 검수한 365개만 추가하고 dev DB의 임시 문장을 복사하지 않는다.

- 통합 SQL SHA-256: `98262a1dc02a7e02b5f918f012ec1e5802cc6da1833394e99ecaf47beb571368`.
- 과거 dev 적용용 `explore_training.sql`·`training_submissions.sql`·`training_claim_reasons.sql`·
  `explore_articles.sql`·`hay_transactions_types.sql`·`explore_content.sql`을 운영에 따로 실행하지 않는다.
  통합본과 중복 실행하면 실패한다. 개발 DB는 이미 이 구조가 있어 통합 SQL의 적용 대상이 아니다.
- 적용 대상: 탐색 기능 테이블이 아직 없는 최신 main DB. `article_progress`만 과거에 존재한다면
  기존 행은 보존하고 기존 sample-id CHECK만 제거한다. 이미 일부 적용했거나 계약이 다르면 중단해
  차이를 먼저 대조한다. `IF NOT EXISTS`로 부분 적용을 숨기거나 기존 데이터를 지우지 않는다.

2026-10-28 원고 갱신: 원고 5편에 영어·일본어를 더하고 한국어의 연구 인용 4곳(2·3·4·5편)을 실제 연구에 맞게
고쳤다. 블록 수·종류·강조 구절 수는 세 언어가 같다. 개발 DB에는 같은 `content_i18n`을 행별 UPDATE로 반영했고,
`explore_content.sql`은 처음 적용한 한국어 원고 기록으로 그대로 둔다. 위 SHA는 이 갱신 뒤의 값이다.

2026-10-28 통합 SQL 검증: 기존 개발 Supabase의 트랜잭션 안에 임시 스키마와 main 기준 원장
CHECK를 만들고 단일 SQL을 실행했다. 테이블 8개의 컬럼 계약·RLS, 원고 5편, 기존 mood_reward
및 신규 두 보상 type을 확인했다. 의도적인 중간 실패의 전체 롤백도 확인했고, 임시 스키마는
남기지 않았다. 최종 운영 후보에서는 제약·인덱스·ACL·RLS까지 대조하고, 읽을거리 3언어 5편과 한마디
365개·과거 5행 보존·예약 날짜 충돌·날짜 가드·옛 아티클 보상 보존을 같은 SPI 실행 경로로 확인했다.
모든 임시 구조는 롤백 후 제거했다. 운영 DB는 읽기 전용으로 main 계약 통과와 탐색 8테이블
미적용을 확인했다. 이는 운영 SQL 적용 증거는 아니며 운영 잠금 획득·최종 계약 dry-run은
승격 직전에 별도로 필요하다.

### 오늘의 한마디 365개 — 통합 SQL에 포함

별도 `daily_affirmations_365.sql`은 dev 적용 이력이며 운영에서는 실행하지 않는다. 통합 SQL 안의
검수 문장을 **2026-10-28~2027-10-27**에 날짜당 하나씩 넣는다. 기존 운영 5행(2026-10-21~25)은
읽기 확인 시 그대로 있었고 미래 예약은 0행이었다. 실행 직전 같은 범위가 비어 있는지 다시 검사한다.

- 시작 날짜는 파일 안의 `start_day` 한 곳에 고정했다. dry-run과 commit 사이에 자정이 지나도
  날짜 범위가 달라지지 않는다. 365건이 정확히 삽입되지 않으면 전체 실패다.
- 2026-10-28 사용자 지시로 **오늘 시작·운영 반영**을 승인했다. `start_day`가 실행 시점의 KST
  당일과 같아야 통과한다. 자정이 지나면 날짜와 SHA를 다시 검토하고, 별도365 SQL로 우회하지 않는다.
- 기존 예약 문장이 하나라도 충돌하면 오류로 중단한다. SHARE ROW EXCLUSIVE 잠금으로 다른
  입력과 경합을 막고 일반 조회는 허용한다. 기존 문장을 덮어쓰거나 날짜를 밀어 붙이지 않는다.
- 앱 심사/노출 여부와 관계없이 현재 오늘의 한마디 API는 DB를 읽는다. 오늘 시작 지시에 따라
  적용 직후 KST 사용자는 1번을, 이미 다음 날인 시간대 사용자는 해당 현지 날짜의 문장을 받는다.

### 기회 제한과 테스트 편의 설정 — 운영 후보 확인

- 운영 환경은 정확히 `production`이어야 한다. 기존 배포 preflight와 배포 후 health에서 확인한다.
  dev의 기회 무제한/실패 차감 생략은 정확히 `development`에서만 작동한다. production은 최대 5회,
  실패 답마다 1회 차감, 1시간마다 1회 회복이다. 통과 답은 차감하지 않는다.
- 완료 요청의 동일 ID 재전송은 기회를 다시 차감하지 않는다. 0회이면 정답 제출도 채점 전에 막는다.
  live 요청은 T1/T2 잠금 뒤 시각을 다시 읽어 대기·채점 중 충전 경계를 반영한다.
- 종료된 옛 과정은 상세·과거 답·완료 receipt 재조회와 이의 기록은 유지하고 신규 제출/보상은 404로 막는다.
  공개 목록에는 현재 3개 과정만 나타나며 과정별 새 보상 우회를 허용하지 않는다.
- AI 10초 초과/공급자 오류/잘못된 JSON 때 `judge=fallback`으로 통과시키는 것은 승인된 운영 정책이다.
  QA 무조건 통과 설정이 아니다. 기호·반응·복사 답 규칙은 AI fallback보다 먼저 실행하고,
  fallback 통과의 단계 보상도 원장/receipt로 한 번만 지급한다. 이 정책은 이번 준비에서 변경하지 않는다.
- `MODEL_TRAINING_GRADE` 기본값은 `gpt-6-luna`, 추론 none/출력 200/전체 10초다. 실제 운영 override와
  기존 OpenAI 자격이 유효한지 배포 preflight에서 확인한다. 대화 모델·한도는 main의 정책을 유지한다.
- `/dev`는 production에서 플래그가 잘못 켜져도 등록하지 않는다. QA 서버/채점 평가 스크립트는
  운영 Docker 이미지의 COPY 대상이 아니다. 테스트 계정 identity override는 운영 앱에 연결하지 않는다.
- 앱이 탐색 탭을 숨겨도 API 직접 호출은 가능하다. 구독 gate·현재 단계·기회·삭제 장벽·보상 멱등은
  서버에서 항상 적용한다. 앱 업데이트 전에 미리 배포할 수 있지만 UI 숨김을 보안으로 취급하지 않는다.

### 구버전 지갑 이력 호환

구버전 iOS는 mood/training/article 보상 type을 모르면 페이지 전체 decode가 실패한다.
`GET /wallet/transactions`는 capability가 없는 요청에 세 신규 type만 `admin_adjustment`로
표시한다. 원장에는 원래 type을 그대로 보존하며 id·금액·잔액·시간·페이지 커서와 행 수는 바꾸지 않는다.
옛 앱에서는 해당 보상에 기존 조정/기타 라벨이 보이는 것이 호환상의 제한이다.

새 세 type을 모두 해석할 수 있는 앱만 `X-Moly-Capabilities: wallet-ledger-types-v2`를 보내
원래 type을 받는다. 서버 권한이나 실제 원가·지급을 바꾸는 헤더가 아니다. 현재 앱에 헤더를
강제로 추가하거나 원장 행을 숨겨 decode 실패를 우회하지 않는다.

### 최종 후보 검증 기록 (2026-10-28)

- 기준: main `c1817a68` + dev `b54dbfa1`. 운영의 출석 40/100·감정 보상20·가입일 한도1.25배·
  채팅 잘림 복구·timeout 추적·일기 문구를 유지했다. 운영의 기존 보상·한도·대화 정책을 유지했고, 경제 이력 응답에 구버전 호환 처리만 추가했다.
- 최종 비통합 테스트 3,753개, Ruff, OpenAPI bundle/계약, 병합된 DB 카탈로그 검증을 통과했다.
  구버전 지갑 호환 추가 후 CI와 같은 placeholder DSN 환경에서 전체 재검증했고, 실제 옛 iOS enum으로
  혼합 11행 응답 decode 성공과 canonical 신규 type의 실패 대조를 확인했다.
  카탈로그는 기존 개발 DB에 main의 추가 구조를 트랜잭션 안에서만 반영해 대조한 뒤 롤백했다.
  DB에 별도로 존재하는 `subscription_launch_config(uuid)`와 해당 서비스 권한은 기존 backend
  schema 원본 밖의 추가 객체로 유지했으며 복사·삭제하지 않았다.
- production 동작을 기존 개발 DB에서 실행해 5→0 차감·0에서 정답 제출 차단·replay 무차감·
  충전 정각·마지막 기회 병렬4요청 중1회만 접수·채점 중 충전/경합·archived 차단을 확인했다.
  이는 배포된 dev의 무제한 모드 시험으로 대체한 결과가 아니다. 전용 QA 계정은 정리했다.
- 준비 단계에서는 운영을 변경하지 않았으며, 2026-10-28 후속 지시로 오늘 시작과 운영 반영을 승인받았다.
  최종 CI, 실행 시점의 이미지 tag/digest·환경·worker 소유권·SQL 유효 날짜는 머지 직전 확인한다.

### 운영 승격 때의 실행 순서

1. 최신 main과 승격 후보 SHA를 고정한다. 코드·OpenAPI·DB 계약에 main의 최신 출석·감정 보상 등도
   포함됐는지 확인한다. 당시 운영 이미지 tag/digest와 이전 DB 계약을 보관하고 진행 중 배포가 없음을 확인한다.
2. **main checkout에서** `db.verify --env prod`가 통과해야 한다. 운영의 `daily_affirmations`와
   `mood_reward`도 확인한다. 지금 dev 계약을 운영 DB에 그대로 대조하지 않는다.
3. **승격 후보 checkout에서** 아래 단일 파일을 dry-run 후 적용한다. `db.apply`가 트랜잭션을 소유하여
   실패하면 테이블·CHECK·원고 입력을 모두 롤백한다. SQL SHA가 이 문서와 다르면 중단한다.

   ```bash
   uv run python -m db.apply db/changes/explore_prod.sql --env prod --expected-sha256 98262a1dc02a7e02b5f918f012ec1e5802cc6da1833394e99ecaf47beb571368
   # 운영 승격 승인 후, 머지 직전에만 실행한다.
   uv run python -m db.apply db/changes/explore_prod.sql --env prod --commit --allow-prod --expected-sha256 98262a1dc02a7e02b5f918f012ec1e5802cc6da1833394e99ecaf47beb571368
   uv run python -m db.verify --env prod
   ```

4. 검증은 **승격 머지 후보 계약**을 사용한다. main의 추가 객체를 덮거나 dev의 과거 계약으로
   재생성하지 않는다. 최종 CI·리뷰와 SQL 적용 결과를 확인한 뒤 main에 머지해 자동 배포한다.
5. 사후 확인: 미인증 401, 인증된 목록·상세 정상, 아티클 5편과 진행 id 일치, 무료 현재 6단계와
   구독 전용 글 첫 보상은 전용 403, 기회·원장 변경 0. 비무료 계정은 허용하며 완료 요청/기지급 보상
   재요청은 중복 차감·지급이 없어야 한다. API·consumer SHA·worker 한 대·큐도 기존 배포 절차대로 확인한다.

SQL은 기본 lock 2초·statement 60초로 제한한다. `hay_transactions` CHECK 재검사 및 기존
`article_progress`가 있으면 CHECK 변경에 짧은 ACCESS EXCLUSIVE lock이 필요하다. 잠금을
얻지 못하면 전체 롤백 후 시점을 바꿔 재시도한다. **성공한 SQL은 재실행하지 않는다.**

### 복구와 보존

- 원고·진행·답·보상·원장·제출 receipt를 지우지 않는다. 이 변경은 기존 보상을 소급 회수하지 않는다.
- 통합 SQL은 실행 중인 이전 코드와 호환되지만, 바뀐 원장 type CHECK는 이전 이미지의 배포
  preflight에서 `changed constraints`로 거부될 수 있다. SQL을 적용한 뒤 예전 이미지를 즉시
  재배포할 수 있다고 가정하지 않는다.
- 새 보상 원장 행이 없을 때만 main의 이전 CHECK로 되돌리고 이전 이미지 복구를 검토한다.
  `training_reward`·`article_reward`가 하나라도 있으면 CHECK를 축소하지 않는다. 새 DB를 읽는
  호환 이미지로 전진 복구한다. 무료 권한 게이트 이전 dev 코드로 돌아가면 API 우회가 다시 열린다.
- 운영에 적용하기 전 단일 SQL의 실행·실패 롤백·권한·스키마 계약 검증 증거를 최종 후보 SHA와
  함께 남긴다. 과거 분리 SQL 검증을 이번 통합 SQL의 실행 증거로 사용하지 않는다.

### 구독 게이트 계약

- 서버 `plan == "free"`만 신규 6단계 이상 제출·전용 글 신규 보상을 막는다. 구독·스토어 체험·
  유효한 기존 런칭/가입 체험은 서버 entitlement를 따른다. 새 체험 기간을 부여하지 않는다.
- 채점 전 승인한 작업은 도중 자연 만료돼도 같은 lease일 때 완료한다. 완료 receipt 재조회는
  만료 후에도 유지하지만, 미완료 요청을 다시 실행하려면 현재 권한을 확인한다.
- 글의 전용 여부는 `preview_block_count IS NOT NULL`이다(0 포함). 평가는 무료 계정도 허용하고,
  기지급 보상은 `granted=0`과 현재 잔액으로 응답한다. 서버 거절은 읽음/진행 행을 새로 만들지 않는다.
- 무료 계정도 `GET /articles`에서 전체 본문을 받는다. 이번 작업은 신규 제출·보상 제한이며
  본문 보호는 포함하지 않는다. 앱은 두 전용 403 코드를 처리하고 보상 권한을 blur 여부와 분리해야 한다.
- 개발 환경의 기회 무제한 특례는 유지하지만 권한 게이트는 모든 환경에서 작동한다.

### 원고 편집

원고는 한국어가 원본이고 번역이 없는 언어는 한국어로 표시된다. 내용 변경은 검토 SQL을 같은
`db.apply` 절차로 적용한다. 새 글은 새 id를 사용하고 사용자별 기존 보상 이력을 유지한다.

```sql
UPDATE public.articles SET content_i18n = content_i18n || jsonb_build_object('en', '<검토한 JSON>'::jsonb) WHERE id = 'sample-1';
UPDATE public.articles SET is_active = false WHERE id = 'sample-3';
```

오늘의 한마디는 현지 날짜 이하의 최신 행을 사용한다. dev의 2026년 행은 임시 문장이므로 운영에
일괄 복사하지 않는다. 날짜별 검수한 문장만 추가하고 지난 날짜의 행은 임의로 바꾸지 않는다.

## 오늘의 작은 실천 DB·템플릿

자동 배포는 DB를 바꾸지 않는다. `db/changes/daily_practice.sql`은 새 테이블 둘만 만들고 기존 행은 바꾸지 않으므로
이전 서버와 함께 돌아도 문제가 없다. 이미지 배포 전에 적용한다. 스키마 사전 검사가 새 계약을 보므로 순서를 바꾸면
배포가 실패한다.

```bash
uv run python -m db.apply db/changes/daily_practice.sql --env dev --commit --expected-sha256 <검토한-SHA256>
uv run python -m db.verify --env dev
```

운영도 같은 절차에 `--env prod --commit --allow-prod`를 쓴다. 되돌리기는 기능 없는 이미지로 내린 뒤
`DROP TABLE public.daily_practice_offers, public.daily_practice_templates;`다. 사용자 행도 함께 사라진다.

템플릿은 마이그레이션에 들어 있지 않다. 운영자가 직접 넣으며 모든 사용자가 함께 쓴다. 활성 템플릿이 없으면 API는
오류 없이 `options: []`를 낸다. 3개보다 적으면 그만큼만 낸다. 문구는 언어별 1~50자(루틴 이름 한도와 같다)이고 `title_ko`는 유일하다.

```sql
INSERT INTO public.daily_practice_templates (title_ko, title_en, title_ja)
VALUES ('물 한 잔 마시기', 'Drink a glass of water', 'お水を一杯飲む');
```

내릴 때는 끈다. 한 번이라도 사용자에게 보인 템플릿은 사용자 행이 참조하므로 `DELETE`가 실패하고, 보인 적 없는 템플릿만
지울 수 있다. 끈 템플릿은 새 제안에 나오지 않지만 이미 받은 제안과 진행 중인 실천에는 그대로 보인다.

```sql
UPDATE public.daily_practice_templates SET is_active = false WHERE title_ko = '물 한 잔 마시기';
```

## 구독 환불 권한 전환

체험·결제 기간은 `subscriptions.access_evidence`, 웹훅의 결제·보상·권한 진행은
`revenuecat_events.processing_state`로 분리한다. NULL인 기존 고객은 기존 정책을 유지한다.
새 전환 설정은 기본 꺼짐이며 이미 전환한 고객과 미완료 단계는 꺼도 새 처리기를 유지한다.

1. `db/changes/subscription_refund_access.sql`의 SHA-256을 검토하고 `db.apply`로
   rollback 검사 후 운영에 적용한다. nullable JSONB 두 컬럼만 추가하며 고객 행은 수정하지 않는다.
   `db.verify`를 통과한 뒤 main에 구독 변경만 머지하고 정상 배포 완료를 확인한다.
2. 별도 읽기 전용 조회로 보정 대상의 구독·소유자·결제 번호·상태를 확인하고 대상 checksum과
   환불/정보 보정/대조군 수를 준비한다. 고객 식별자와 원문은 Git·Actions 입력·로그에 넣지 않는다.
3. main의 **Production subscription operations** workflow를 `report`로 실행한다.
   입력은 배포 SHA, 임의 session 이름, 위 checksum·건수다. 보고서 checksum을 받은 뒤 같은 입력과
   `expected_manifest`로 `repair_refunded` → `repair_metadata` → `verify` 순서로 실행한다.
4. 보정·최신 RevenueCat 상태·대조군을 모두 확인한 뒤 `activate`로 새 수신 이벤트 전환을 켠다.
   마지막 `verify`와 읽기 전용 조회로 확인한다. 기존 고객을 일괄 전환하는 작업은 아니다.

workflow는 프로젝트 GitHub OIDC 역할과 SSM만 사용하며 로컬 AWS 계정을 사용하지 않는다.
API 두 대·consumer·실행 중 worker·다음 worker 이미지의 배포 SHA, 정상 target 두 대와 timer 소유자
한 대를 확인한다. 배포와 같은 concurrency group으로 직렬화한다. 고정 작업만 허용하며 서버의
RevenueCat 키를 외부로 출력하지 않는다.

서버 Git 밖 `/var/lib/moly/subscription-ops/<session>`에 보고서·보정 계획·commit 전후 journal을
소유자 전용 권한으로 보관한다. 대상·소유권·결제·금융 이력·대조군 변화, 새 미처리 이벤트,
불명확한 commit 결과가 있으면 멈춘다. 환불 권한 보정은 결제·건초를 다시 처리하지 않으며,
정보 보정은 정상 고객의 권한·만료일·건초를 보존한다. 실패 시 예전 스냅샷을 덮어쓰지 않는다.

문제 시 호환 배포 SHA와 session으로 `deactivate`를 실행해 새 전환을 끈다. 이 작업은 오래된
보고서·RevenueCat 접속·전체 fleet 정상 여부에 의존하지 않으며 이미 전환한 고객을 되돌리지 않는다.
JSON 데이터가 저장된 뒤에는 컬럼을 삭제하거나 이 컬럼을 모르는 이미지로 돌아가지 않는다.
`subscription_refund_access_rollback.sql`은 모든 writer/reader가 정지했고 두 컬럼이 전부 NULL인
경우에만 사용한다. 그 외에는 호환 이미지로 전진 복구한다.

## 가드레일·대화 맥락의 무중단 전체 전환 (2026-11-04)

### 가드레일 단독 활성화 — 기존 자료 유지 (2026-11-05)

가드레일은 기존 컨텍스트 reader와 함께 독립 활성화할 수 있다. 이번 실행은
`context_retention_v2_enabled`를 OFF, `context_retention_active_preparation`을 JSON null로
유지한다. 아래 전체 자료 준비/전환 명령은 실행하지 않으며 과거 요약·기억·계약을 재작성하지 않는다.
`crisis_guard_enabled=true`, `crisis_guard_policy_version="crisis-v2"`,
`crisis_guard_timeout_s=2`, `crisis_guard_history_messages=6`만 전역 적용한다.

호환 수정은 가드 ON일 때도 기존 원문/구형 요약의 과거 위기 정리가 유지되도록 하는 두 조건이다.
한·영·일 최종 LLM 입력 회귀 9개가 기존 일반 정보·계약·기억·현재 입력을 보존하고,
수정 전 두 조건을 각각 메모리에서 복원하면 언어별 세 검사 모두 실패함을 확인했다.
기존 단위 5,391개와 추가 회귀를 통과했고, 기존 개발 API에서 가드 ON·retention OFF의
21턴이 고정 문구·메시지 종류·고정 응답 0차감·화제 복귀를 통과했다. 실제 벡터 회상 적중을
입증한 시험은 아니다. 재현 도구는 `scripts/smoke_guard_legacy_dev.py`이며 개발 환경만 허용한다.
활성화는 현재 운영 SHA와 일기 claim/후속 큐가 비었음을 확인한 뒤 수행한다. 변경 전 설정을
보존하며, 복구는 자신이 기록한 값/버전과 일치할 때만 원래 값과 행 존재 여부를 되돌린다.
추가 DDL이나 일반 worker 중지는 필요하지 않다. 기존 컨텍스트 누락 수정 완료와 혼동하지 않는다.

### 범위와 보존 원칙

운영 기준은 `64c133d97c0c61022600c3b9074aa72fa92a113b`다. 이 기준 위에 개발 검증본
`49f1f9940a49f2315414d8bff3beea9836cf62af`의 가드레일·맥락 변경과 무중단 준비 경로를
통합한다. 구독 환불, 루틴, 일상 연습, 교정 원고, OpenAPI와 기존 배포 workflow는 보존한다.
사용자별 기능 목록, 노출 비율, A/B 적용은 없다. 준비 자료를 검증한 뒤 전역 포인터와 설정을
한 트랜잭션에서 바꾸며, 이후 새 요청부터 전체 사용자가 같은 활성 자료를 읽는다. 준비 자료의
행별 일괄 상태 변경을 최종 전환에 넣지 않고, 준비 단계에서 검증 가능한 checkpoint를 seal한다.

**준비 중에도 공개 대화·일반 worker·결제/환불 webhook·인증·스케줄러를 계속 운영한다.**
기존 공개 계약과 요약을 준비 자료로 덮어쓰거나 공개 원문을 줄이지 않는다. 준비 계약과 요약은
준비 ID로 분리해서 저장한다. 내부 manifest의 사용자 키는 원문 처리 목표·감사용이며 일반
reader의 기능 허용 목록이 아니다. 개인정보 삭제 중/완료 계정에는 기존 접근 금지를 유지한다.

이 절은 실행 절차다. 운영 PR·배포·DDL·자료 준비·활성화는 아직 실행하지 않았다. 운영 전체
전환은 실행 시점의 최종 SHA, 실제 자료 감사와 API 확인을 통과해야 한다.

### DB와 도구

| 파일 | 용도 |
| --- | --- |
| `db/changes/guard_context_release.sql` | 최종 구조의 검토 참조. 운영에서 한 TX로 실행하지 않음 |
| `db/changes/guard_context_release_metadata.sql` | 짧은 구조 변경. 기존 테이블 전체 NOWAIT 잠금을 먼저 잡고, CHECK는 NOT VALID로 추가. 전체 DDL statement 제한 750ms |
| `db/changes/guard_context_release_validate_messages.sql` | messages 새 CHECK만 별도 TX에서 검증 |
| `db/changes/guard_context_release_validate_registry.sql` | 기억 registry priority CHECK만 별도 TX에서 검증 |
| `db/changes/guard_context_release_validate_candidates.sql` | ingest candidate priority CHECK만 별도 TX에서 검증 |
| `db/changes/guard_context_release_validate_pressure.sql` | pressure CHECK만 별도 TX에서 검증 |
| `db/changes/guard_context_release_swap.sql` | 검증된 messages CHECK의 이름만 NOWAIT/750ms로 교체 |
| `db/changes/guard_context_online.sql` | metadata에 포함된 무중단 준비 구조. 최초 운영 도입 시 중복 실행하지 않음 |
| `db/changes/guard_context_online_proof.sql` | 이전 online 구조가 이미 있는 개발 DB에 `validation_json` 한 컬럼과 object CHECK만 추가. 최초 운영 metadata에는 이미 포함 |
| `db/changes/guard_context_online_indexes.sql` | 이미 online 구조가 있는 개발 DB의 게시 unique를 기존 NULL 준비 영역/각 준비 ID 영역으로 분리. 최초 운영 metadata에는 이미 포함 |
| `db/changes/guard_context_online_revision_fence.sql` | 이벤트 보완 전 sentinel 도입의 검토 참조. 최신 DB에 이전 함수를 재적용하지 않음 |
| `db/changes/guard_context_online_revision_events.sql` | 이미 revision/sentinel이 있는 개발 DB에 이벤트 테이블과 최신 NOWAIT 함수를 추가. 기존 clock/trigger/원문은 유지 |
| `db/changes/guard_context_online_revisions.sql` | 준비 subject의 변경 revision과 원 writer TX의 잠금 fence. 최초 metadata와 기존 개발 DB 추가에 같은 정의 사용 |
| `db/changes/guard_context_release_disable.sql` | 두 쓰기 기능 OFF. 활성 준비 포인터와 읽기 호환성·기존 자료는 유지 |
| `scripts/prepare_guard_context_release.py` | `plan`·`start`·`catch-up`·`audit`·`activate`·`abort`. 모델/provider 호출 없이 준비 예약·감사·원자 전환 수행 |
| `scripts/run_guard_context_preparation_worker.py` | 지정한 준비 ID의 building 계약/checkpoint만 처리. 일반 소비자와 별도 실행 |

DDL은 기존 원문·기억·일기·공개 계약·요약을 갱신하거나 삭제하지 않는다. 준비 단계에서는
입증된 NULL canonical 본문을 채우고 처리 목표를 예약할 수 있지만, 기존 공개 계약/요약과
앵커를 준비 자료로 바꾸지 않는다. 기존 게시 window unique는 NULL 준비 영역으로 한정하고,
준비 ID별 게시 unique를 별도로 둬 같은 원문 경계의 기존 공개행과 준비행이 공존하게 한다.
같은 준비 ID 안의 중복 게시는 계속 거부한다. 구독·일상 연습의 스키마도 보존한다. 활성 포인터 초기값은
JSON null이며, 이미 있는 포인터는 DDL로 덮어쓰지 않는다.

준비 worker는 memory/content에서 정확한 준비 ID와 building 표시가 있는 작업만 소비하고
각 큐의 동시 실행은 1개로 제한한다. 공개 사실 파이프라인과 다른 업무는 일반 worker가 계속
처리한다. 준비 worker는 전역 reaper·만료 정리·광고 정리를 실행하지 않으며, 기존 사용량
원장 마감은 유지한다. 일반 worker를 준비 worker로 대체하거나 전체 큐를 정지하지 않는다.

### 실행 전 검증

- 최신 main 위에서 최종 CI, 구독/루틴 보존 검사, DB 계약과 전역 ON/OFF 검사를 통과시킨다.
  main이 바뀌면 다시 통합한다. 다른 배포를 취소하거나 우회하지 않는다.
- 초기 전환의 일반 설정은 guard/retention 모두 OFF, 활성 포인터는 JSON null인지 확인한다.
  이미 active 준비 ID가 있다면 이 최초 전환 절차로 재활성화하거나 포인터를 지우지 않는다.
- 원문·준비 자료 변경 거부, 기존 요청이 모델을 생성하는 동안의 활성화, 준비 worker 최종 저장과
  활성화의 양쪽 잠금 순서, 새 대화/신규 계정, 활성화 뒤 OFF 읽기 호환을 실제 경합으로 검증한다.
- 긴 감사는 읽기 일관성이 있는 트랜잭션에서 수행하고, 마지막 승격은 NOWAIT 잠금과 전체
  1초 제한을 쓴다. 실제 전체 규모에서 이 제한 안에 끝나는지는 별도 측정 대상이다.
  제한을 늘리거나 검사 항목을 빼서 성공시키지 않는다.

최종 개발 DB catalog에서 이번 가드 변경의 컬럼 39개, 제약 28개, 함수 3개, 인덱스 11개,
권한 25개, sequence 1개, 테이블 4개와 trigger 12개가 후보 계약과 일치했다.
최종 운영 후보의 개발 API 배포를 위해 개발에 빠져 있던 기존 main의 `daily_practice.sql`을
개발 DB에만 적용했다. 기존 행·템플릿 seed·설정 변경 없이 테이블 두 개를 추가했고, 이후
전체 non-strict `db.verify`를 통과했다. 기존 개발 구독 helper의 추가 객체는 별도로 남으며
strict 전체 catalog 일치로 처리하지 않는다. 2026-11-04 운영 metadata
읽기 점검은 당시 main 계약과 차이가 없었으나 실제 적용 직전에 현재 metadata와 원문 처리
경계를 다시 확인한다.

### 현재 검증 판정 (2026-11-04)

단위 검사 5,391개, 개발 DB의 온라인 승격·비공개 읽기·분리 DDL 잠금 검사 34개와
revision/event·동시 작성자·등록 경계 검사 36개, 정리 대기·재개 DB 검사 13개를 통과했다. 별도 에이전트가 최종 잠금 순서,
원문/출처/계약/요약 검증의 유지, 최초 감사 기한과 재시도 제한을 다시 검토했다.
Ruff·diff 검사와 전체 후보 134개 파일의 비밀값 검사도 통과했다. 구독·루틴·일상 연습·훈련의
제품 코드, OpenAPI와 기존 배포 workflow는 운영 기준 main64와 동일하다.

최종 규모 시험은 합성 사용자 3,207명, 기억/근거 각각 43,000행, 원문 6,414행,
요약/계약 각각 3,207행으로 수행했다. 전환 직전의 전체 자료 재해시는 없애고, 준비 단계에서
전체 근거를 감사한 뒤 원 writer 트랜잭션의 변경번호/이벤트로 변경 여부를 증명한다.
검증·잠금·전역 게시·caller의 savepoint commit까지 같은 1초 안에 측정한다.
연결 확보·metadata payload 업로드·비공개 seal은 전환 잠금 전에 별도로 수행한다.
동일 제품 코드로 세 번 측정해 **947.51ms, 922.57ms, 703.35ms**로 모두 통과했다.
최대값은 947.51ms이며 네트워크/부하 변화에서도 항상 1초 내 성공한다는 보장은 아니다.
제한 초과 시 전환만 rollback하고 공개 서비스는 유지한다. 이전 전체 재해시 방식의 timeout
기록도 보존하며 최신 성공 결과로 덮어쓰지 않는다.

성능용 token과 8명 표본 감사를 사용한 합성 시험은 운영 전체 자료의 감사 완료나 실제
운영 commit/WAL 내구성·실제 원문량의 대표성을 증명하지 않는다. 전역 실 commit·운영 변경은
0회이며 시험 뒤 합성 auth 잔여도 0건이다. 실제 배포 때 같은 SHA의 전체 감사와 현재 상태
확인, 운영 API 점검을 아래 순서대로 수행해야 한다. 운영 PR·push·배포는 실행하지 않았다.

최종 개발 API/consumer 배포 SHA는 `2339e307c4b28b89a4a091a4a2ccc1ec34462f86`이며
[개발 배포 Actions](https://github.com/SOMA-OneTwoThree/moly-backend/actions/runs/41816367599)를
통과했다. 개발 전용 merge의 제품/검사 tree는 운영 후보 `6ee53ae8`과 동일하다. 공개 health와
ready에서 같은 SHA·development 환경·DB 정상 상태를 확인했고, 한·영·일 21개 시나리오의
115턴을 실제 개발 API로 호출해 모두 통과했다. 고정 문구/메시지 구분·중복 요청의 동일 응답과
중복 저장/차감 없음·위기 원문의 파생 기억 제외·긴 대화 압력 발생/요약 게시와 처리 완료 근거·
원문 보존을 검사했다. 종료 코드 0, 시험 계정 삭제·설정 CAS 복원 성공, 잔여 정리 journal 0이다.

첫 개발 배포에서는 긴 대화 재개 중 `CHAT_TURN_STALE` 한 건이 발생했다. 최초 설정 해시에만
새 활성 준비 ID가 포함된 것이 원인이었다. 정책 키 집합을 하나로 정의하고 시작·재개 양쪽이
같은 집합만 비교하도록 고쳤다. 준비 ID는 요청 binding에 고정하며 실제 정책값 변경/삭제의
차단은 유지한다. 옛 해시로 null/UUID 두 회귀의 실패를 재현하고 수정 후 전체 검사와 위 API
시나리오를 통과했다. 첫 실패·수정 전/후 산출물은 별도로 보존했다.

### 준비와 원자 전환 순서

1. 공개 서비스를 유지한 채 운영 metadata를 검토된 기준과 대조한다. metadata 파일만
   먼저 dry-run하고, 같은 SHA를 750ms 제한으로 commit한다. 기존 테이블 잠금을 NOWAIT로
   모두 얻은 뒤 변경하므로 경합 시 전체 rollback한다. commit 후 네 CHECK를 각각 별도
   파일·별도 TX로 검증하고 마지막 swap 파일을 750ms로 실행한다. 검증을 metadata TX에
   합치면 ACCESS EXCLUSIVE 잠금이 scan 동안 유지되므로 한 번에 합쳐 실행하지 않는다.
   각 검증은 SHARE UPDATE EXCLUSIVE를 사용해 일반 대화 쓰기와 병행한다. 해당 파일의
   제한 초과·잠금 실패는 그 TX만 rollback하며 서비스나 일반 worker를 끄지 않는다.
2. DDL 적용 뒤 호환 API·일반 worker 이미지를 기존 운영 workflow로 배포한다. 두 TG 대상,
   API·consumer의 production 환경/최종 SHA, `/health/ready`와 DB 계약을 확인한다.
   기존 `.github/workflows/deploy.yml`의 무중단 롤링을 그대로 쓴다. healthy 2대와 기존 API/consumer
   SHA를 확인하고 한 대씩 deregister·drain·배포·healthy 확인 후 다음 대로 진행하며, 양쪽의
   새 SHA를 확인한다. 이 작업에서 workflow를 바꾸지 않는다. 준비를 시작하기 전 호환되지 않는
   구버전 actor가 교체됐는지 확인하며 공개 대화는 유지한다.
3. `plan`의 차단 사유와 집계를 검토한다. 승인 checksum은 코드/환경/전체 준비 방식을 승인한다.
   `start`는 그때의 실제 완결 원문 경계를 별도 준비 manifest로 봉인하고 준비 ID를 기록한다.
   이 ID를 이후 모든 명령과 준비 worker에 동일하게 전달한다.
4. 같은 SHA의 준비 worker를 별도 프로세스로 실행한다. 일반 사실 처리와 준비 계약/요약 계산이
   함께 진행된다. 본문·근거를 입증할 수 없는 계정을 임의 제외해 전환하지 않는다. 처리할 수
   없는 원문을 고치거나 커서·실패 예산을 초기화하지 않는다.
5. 준비 중 새 대화가 생기면 `catch-up`으로 원래 봉인한 원문의 불변성을 확인한 뒤 목표를
   갱신한다. 같은 준비 ID를 유지한다. 새 경계의 작업이 끝나면 `audit`로 전체 처리 근거와
   durable 계약 chain, 요약 원자/해시, 현재 세대·권한·미처리 tail의 입력 예산을 검사한다.
   `async_jobs` payload가 만료됐다고 처리 근거를 잃지 않도록 계약 행의 증명을 사용한다.
6. `activate`는 먼저 비공개 준비 checkpoint의 완성 증명 형태를 확인해 `activation_sealed`
   표시를 저장·commit한다. 이 seal만으로 자료가 공개되지는 않는다. 이어서 새 **REPEATABLE
   READ READ ONLY 감사**에서 전체 원문·기억 근거·계약 durable chain·요약 proof와 처리 완료를
   검증하고 같은 snapshot의 등록 경계와 유효 revision을 읽는다. 변경된 subject의 재감사도
   RR 읽기 전용으로 수행하며 원래 token 기한과 재시도 제한을 늘리지 않는다.
   마지막 1초 전환 TX는 **READ COMMITTED**로 실행한다. registry FOR UPDATE NOWAIT를 먼저
   잡고, 등록 clock을 사용자 순서로 FOR UPDATE NOWAIT 잠근 뒤 유효 revision과 대상/경계를
   비교한다. 유효 번호는 base revision+COUNT(events)이며 sequence MAX는 쓰지 않는다.
   이 단계에서 전체 기억/근거를 재해시하거나 source 전체 테이블을 SHARE로 잠그지 않는다.
   권한·인구 metadata, 현재 >T tail과 입력 예산은 최신 상태로 확인하며 필요한 tail 행 잠금은
   유지한다. 서로 다른 snapshot을 묶지 않도록 최종 TX에서 RR은 거부한다.
   원 writer는 clock을 기다리며 source 행을 보유하지 않는다. semantic 변경은 clock UPDATE
   NOWAIT 성공 시 번호를 올리고, 경합 시 append-only event를 넣는다. ignored 변경/>T append는
   clock SHARE NOWAIT를 쓰고 경합하면 번호/event 변경 없이 registry KEY SHARE로 우회한다.
   미등록 subject의 의미 변경도 registry KEY SHARE에 참여한다. event의 registry FK와 이
   SHARE가 최종 registry UPDATE와 변경 전/후를 직렬화하며 clock 복합 FK는 두지 않는다.
   모든 source writer는 기술 sentinel을 SHARE로 잡고, 신규 등록/경계 확장은 같은 행을 UPDATE해
   등록 전 writer와 직렬화한다. 오래된 RR writer snapshot은 40001로 거부한다. 준비가 없을 때도
   sentinel 잠금에 참여하되 원문 JSON 복사는 건너뛴다.
   최종 commit은 registry active, 전역 포인터·guard/retention 설정과 남은 준비 작업 취소만
   함께 반영한다. seal한 checkpoint와 준비 계약의 ready 행을 대량 UPDATE하지 않는다. 활성
   reader는 같은 준비 ID의 sealed ready/published checkpoint와 ready/published 계약을 읽으며
   draft 계약은 읽지 않는다. 기존 공개 자료와 봉인 경계 뒤에 붙은 미처리 원문은 유지한다.
7. 경합·원문 변경·시간 초과가 나면 전체 승격을 rollback하여 기존 공개 경로를 그대로 유지한다.
   새 출력 경로로 원인을 확인한 뒤 필요한 `catch-up`·감사를 다시 수행한다. 부분 승격이나
   활성화 SQL 직접 실행으로 우회하지 않는다. 성공 시 준비 worker를 SIGTERM으로 드레인하고
   전체 사용자 전환 뒤 API·기억·회계 검사를 수행한다. 대화 차단/해제 단계는 없다.

DDL은 단계마다 commit하므로 일부 실패가 이전에 완료한 단계까지 되돌리는 것은 아니다.
실행 결과가 불분명하면 현재 catalog부터 확인한다. metadata 전이면 동일 SHA로 재시도하고,
metadata 후에는 기존/새 messages CHECK 이름과 정의, 각 CHECK의 validated 상태, 컬럼·index·
권한이 검토된 중간 구조와 같은지 확인한 뒤 미완료 검증부터 이어 간다. metadata를 무조건
다시 실행하거나 누락 제약을 임의로 건너뛰지 않는다. swap 전에는 기존 CHECK와 검증된 임시
CHECK가 모두 있어야 하며, 이미 swap이 완료됐다면 최종 이름/정의/검증 상태를 확인하고
재실행하지 않는다. 마지막 swap은 원본 또는 임시 CHECK 부재를 오류로 처리한다. 중간 상태에서는
최종 `db.verify`가 성공한다고 기대하지 않으며, 모든 단계 완료 전 새 기능을 배포·활성화하지 않는다.
기존 서비스는 계속 두고 중간 자료·제약을 삭제해 처음으로 되돌리지 않는다.

기존 요청은 시작할 때 캡처한 None 또는 활성 준비 ID로 끝나고, 활성화 뒤 새 요청부터 새 ID를
쓴다. 전환 순간에 이미 진행 중인 모델 호출을 재실행하거나 같은 요청에 새 자료를 섞지 않는다.
준비 시간·운영 전체 비용·1초 내 승격 성공률은 아직 확정하지 않았다. 자동 타이머로 전환하거나
계속되는 대화를 중단해 감사 성공을 강제하지 않는다.

명령은 프로젝트에서 확인한 운영 자격으로 실행한다. 개인 기본 AWS 계정을 사용하지 않는다.
`GUARD_ENV`는 Git 밖 0600 파일의 절대 경로이며 기존 DB 도구를 위해 `.env.prod`로 끝난다.
출력은 Git 밖 0700 폴더의 매번 새로운 파일이다. `GUARD_SHA`는 실행 이미지와 같은 검토된
40자리 SHA이고, `GUARD_PREPARATION_ID`는 start 결과의 준비 ID다. 비밀값은 출력하지 않는다.

```bash
# 각 파일은 먼저 rollback으로 검사하고, 같은 파일/SHA의 실반영에만 --commit --allow-prod를 추가한다.
# metadata를 실제 commit한 다음에만 네 검증 파일을 순서대로 각각 실행한다.
uv run python -m db.apply db/changes/guard_context_release_metadata.sql --env "$GUARD_ENV" --statement-timeout-ms 750 --expected-sha256 bdbd5dbb558e851d95625cd8c93f7353c888627228e7e0c7832e99f2a8e832e7
uv run python -m db.apply db/changes/guard_context_release_validate_messages.sql --env "$GUARD_ENV" --statement-timeout-ms 60000 --expected-sha256 a409e9b06001a0f0c6b131bcde9b0b918049aaf7cf9f035a0c947636e21f8f89
uv run python -m db.apply db/changes/guard_context_release_validate_registry.sql --env "$GUARD_ENV" --statement-timeout-ms 60000 --expected-sha256 a6edfd65a9e844f342a7516dc7d4330a0d6b3a65e52a5724eadc012e540d0061
uv run python -m db.apply db/changes/guard_context_release_validate_candidates.sql --env "$GUARD_ENV" --statement-timeout-ms 60000 --expected-sha256 c54834fb758936b6559ff6950fce63b4ddc1c6797c734187f21140af97265507
uv run python -m db.apply db/changes/guard_context_release_validate_pressure.sql --env "$GUARD_ENV" --statement-timeout-ms 60000 --expected-sha256 d83d2429eecb9e33accabef993da12b7595278fc86330e8d0f954547a01dc0e0
uv run python -m db.apply db/changes/guard_context_release_swap.sql --env "$GUARD_ENV" --statement-timeout-ms 750 --expected-sha256 708b2654cf0c7d0df31192c9906c96b01dcf909cd60f67e79e9855654c303c63
# 네 검증과 swap의 실제 commit까지 완료한 뒤 최종 계약을 확인한다.
uv run python -m db.verify --env "$GUARD_ENV"

uv run python scripts/prepare_guard_context_release.py plan --env prod --env-file "$GUARD_ENV" --expected-project-ref qkgjlgzsharnilxnkytd --expected-code-sha "$GUARD_SHA" --production-ack --output "$GUARD_PLAN"
uv run python scripts/prepare_guard_context_release.py start --env prod --env-file "$GUARD_ENV" --expected-project-ref qkgjlgzsharnilxnkytd --expected-code-sha "$GUARD_SHA" --production-ack --plan "$GUARD_PLAN" --plan-sha256 <승인한-plan-SHA256> --output "$GUARD_STARTED"
# 준비 ID를 확인한 뒤 별도 프로세스에서 실행한다.
uv run python scripts/run_guard_context_preparation_worker.py --env prod --env-file "$GUARD_ENV" --expected-project-ref qkgjlgzsharnilxnkytd --expected-code-sha "$GUARD_SHA" --preparation-id "$GUARD_PREPARATION_ID" --production-ack
uv run python scripts/prepare_guard_context_release.py catch-up --env prod --env-file "$GUARD_ENV" --expected-project-ref qkgjlgzsharnilxnkytd --expected-code-sha "$GUARD_SHA" --preparation-id "$GUARD_PREPARATION_ID" --production-ack --output "$GUARD_CAUGHT_UP"
uv run python scripts/prepare_guard_context_release.py audit --env prod --env-file "$GUARD_ENV" --expected-project-ref qkgjlgzsharnilxnkytd --expected-code-sha "$GUARD_SHA" --preparation-id "$GUARD_PREPARATION_ID" --production-ack --output "$GUARD_AUDIT"
uv run python scripts/prepare_guard_context_release.py activate --env prod --env-file "$GUARD_ENV" --expected-project-ref qkgjlgzsharnilxnkytd --expected-code-sha "$GUARD_SHA" --preparation-id "$GUARD_PREPARATION_ID" --production-ack --output "$GUARD_ACTIVATED"
```

각 명령은 작업자가 결과를 검토하고 별도로 실행한다. 한 shell에서 연속 자동 실행하지 않는다.
종료 코드 0과 결과, 실제 active 포인터/설정·health를 함께 확인하며 파일이 생겼다는 이유만으로
성공으로 판정하지 않는다. audit 미완료·경합은 공개 서비스의 중단 사유가 아니다.

### 전환 뒤 확인과 복구

- 일상 대화, 고정 재확인/위기 안내, 화제 전환과 이전 일반 맥락 회상을 운영 점검 계정으로
  확인한다. 한·영·일 고정 안내는 링크·전화번호·응급 지시 없이 정해진 문구와 같아야 한다.
- 멱등 재요청의 답/메시지 ID 일치, 중복 저장·중복 차감 0, 고정 응답 차감 0을 확인한다.
  과거 기억·선호·일기와 구독·루틴이 유지돼야 한다. 실사용자 대신 점검 계정을 사용하며
  결제·환불은 시험 거래로 실행하지 않는다.
- 정상 응답·503·판정 timeout/error·지연, 준비/활성 작업과 큐 정체를 구분해 기록한다.
  큐 오류는 `jobs._STATS_SQL`의 성공 replay 조상 제외 기준으로 판정한다. raw dead 행 수를
  새 오류로 세지 않는다. 원문·키·사용자 ID는 보고서·로그·Git에 넣지 않는다.
- 판정 timeout이면 일반 경로로 가는 한계는 남는다. 이번 전환이나 검증을 전체 요청 무실패,
  위기 판정 완벽성의 증명으로 설명하지 않는다.

**활성화 전 취소:** `abort` 명령은 해당 준비를 aborted로 표시하고 해당 준비의 잠기지 않은
ready 작업만 취소한다. 준비 worker를 SIGTERM으로 드레인하고 일반 서비스는 유지한다.
이미 시작한 호출의 원가는 정상 마감한다. 준비 자료·원문·실패 이력은 지우지 않는다.
전역 disable만으로 building 전용 worker가 멈추는 것은 아니므로 이 절차를 사용한다.

**활성화 후 기능 중지:** 검토한 `guard_context_release_disable.sql`로 두 전역 기능을 끈다.
활성 준비 포인터·registry active와 이미 게시한 새 자료는 유지한다. OFF는 새 위기 분기와 맥락 보존의 예약·게시를
멈추는 설정이며, 이미 진행 중인 요청은 캡처한 설정으로 끝날 수 있다. 일반 대화·사실 처리의
기존 모델 호출을 모두 중지하는 설정은 아니다. 활성 자료를 읽는 호환
경로는 유지되는지 확인한다. active 준비에는 `abort`를 쓰거나 포인터를 JSON null로 지우지 않는다.

구버전 main64는 새 준비 ID·v5·계약 증명의 읽기 계약을 보장하지 않는다. 새 형식을 읽고 최신
구독/루틴을 보존하는 **검증된 호환 이미지**만 복구에 사용한다. 같은 후보의 전역 OFF는
구버전 이미지 복구 검증과 다르다. 검증하지 않은 복구 SHA를 임의로 적거나 새 CHECK/컬럼을
제거하거나 safety 종류를 normal로 바꾸지 않는다. 원문·기억·일기·준비 자료·결제 이력을 잃는
DB 전체 snapshot 복원도 하지 않는다. 자료 수정이 필요하면 영향 행과 원문 근거를 확인한
별도 전진 복구로 처리한다.

## 변경 시 확인할 원본

- DB·권한·반영: [db/README.md](../db/README.md), `db/schema.sql`, `db/schema_contract.json`.
- 구독·체험·RC/건초: [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md).
- 사용자 노출 자산: [배너](BANNER_SDUI.md), [BGM](REMOTE_BGM.md), [운세](DAILY-FORTUNE.md).
- HTTP 계약: [API_SPEC.md](API_SPEC.md), `openapi/`. 배포 당시 검사 결과는 PR/Actions에서 해당 SHA와 함께 확인한다.
