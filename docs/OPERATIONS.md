# 운영과 장애 대응

이 문서는 반복해서 사용하는 배포·관측·복구 절차다. 실행 시점의 SHA, 환경 설정, DB 계약과 실제 검사 결과는 해당 PR·배포 기록에 남긴다. 코드 기본값이나 과거 영수증을 현재 운영 상태로 간주하지 않는다.

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
- 회상 결과 없음과 조회 실패를 구분한다. 빈 결과를 장애로 세지 않고, 실패를 사용자가 알려주지 않은 사실로 바꿔 답하지 않는다.
- 지속 사건은 `monitoring:alert:<환경>:<신호>` 상태로 신규/변화/악화/재알림/복구를 판단한다. 정상 worker 요약을 매 tick 전송하지 않는다. `daily_status`는 하루 상태 보고용이며 해당 행의 active를 장애라고 해석하지 않는다.
- 같은 미해결 사건은 6시간, 내용 변화는 1시간, 악화·재발은 5분 간격의 억제를 적용한다. 정상 재관측 때 복구를 알린다. 하루 요약은 KST 13시 이후 첫 전송 성공으로 하루 한 번이며 tick 지연·전송 실패에 따라 늦어질 수 있다. 정확히 13시에 도착한다는 보장은 아니다.
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

## 주간 운영자 일기 DB 전환

이 절의 앵커는 일기 운영 절차의 진입점이다. 스키마와 생성 정책 원본은 [DB 문서](../db/README.md), [아키텍처](ARCHITECTURE.md), `app/services/diary_generation.py`다.

- 주간 원고는 `moly_life_ments.week_start_date/sequence_no`, 결과는 `preset_ment_id`와 배정 유일성으로 연결한다. 주간당 두 편을 언어별 검수하고, 다른 원고를 덮어쓰지 않는다.
- `diary_weekly_start_date` 미설정/null은 legacy 모드다. 설정은 유효한 주 시작일로 검증한다. 잘못된 설정·조회 실패 때 legacy로 조용히 fallback하지 않는다.
- 원고 없음/소진은 `no_entry`이며 빈 일기를 만들거나 나중에 소급 채우지 않는다. 일기 본문을 대화에서 미리 공개하지 않는다.
- 전환은 리뷰한 DB 차이 → 호환 코드 → 설정 순서다. 주간 결과가 생긴 뒤에는 legacy 이미지/설정으로 무작정 되돌리지 않고 기존 결과를 읽는 호환을 확인한다. 기존 발행본·원고·배정 이력을 보존한다.
- 개발 수동 생성도 종료된 활동일과 `force=false` 규약을 따른다. `DIARY_FORCE_NOT_ALLOWED`/`DIARY_POLICY_UNAVAILABLE`를 무시하고 우회 생성하지 않는다.
- 해당 개발 통합시험은 합성 fixture와 외부 호출 대체 범위를 확인한 뒤 기존 개발 DB에서 실행한다. 시험 자체가 실제 일기 발송·실모델 품질을 증명하지 않는다.

## 둘러보기 아티클 DB 전환

`db/changes/explore_content.sql`(검토 SHA-256 `75db677a6b21859b745f5deec594eb2f8a0556272f93c3fcf83289fd0a52debb`)은 아티클을 코드 원고에서 DB 데이터로 옮긴다. 개발 DB에는 2026-10-28 적용·검증했다. 오늘의 한마디는 main 계열의 `GET /daily-affirmation`(`public.daily_affirmations`)을 쓴다.

- 내용: `article_progress_article_id_check`(sample-1~7 고정 id) 제거, `public.articles` 생성(RLS, `service_role`만 권한), 아티클 5행(sample-1~5) 입력. 기존 진행·보상 행은 바꾸지 않는다.
- prod 반영 순서(위에서 아래로 그대로 실행, 각 SQL은 dry-run → `--commit --allow-prod --expected-sha256` → 다음 단계):

  | 순서 | 실행 | 검토 SHA-256 |
  |---|---|---|
  | 0 | origin/main 체크아웃에서 `db.verify --env prod`. 실패하면 중단한다. 통과는 `daily_affirmations`·`mood_reward` 등 main 변경이 이미 반영됐다는 뜻이다 | - |
  | 1 | `explore_prod.sql`: 생각 연습·아티클 진행 테이블 | `9f60bf9072e76658f814bfc829062361a15e8ceb00321ff94870cae856147469` |
  | 2 | `hay_transactions_types.sql`: 원장 type 목록 = main 목록 + `training_reward`·`article_reward` | `d92d09205302d3b10f81e2fd9c87f286fa3057c7bb4bd4da99cb4f1078daf178` |
  | 3 | `explore_content.sql`: 아티클 테이블·5행 | 위 SHA |
  | 4 | 승격(dev → main) 머지 커밋 체크아웃에서 `db.verify --env prod` | - |

  `explore_prod.sql`은 dev에 적용한 `explore_training.sql`·`training_submissions.sql`·`training_claim_reasons.sql`·`explore_articles.sql`을 순서대로 합치고 원장 type CHECK 문만 뺀 것이다. 원본 네 파일은 dev의 main `mood_reward`가 없는 목록을 넣으므로 prod에 실행하지 않는다. 2026-10-28 origin/main의 `schema.sql`·`seed.sql`로 만든 로컬 PostgreSQL 17에 `mood_reward` 원장 행을 넣고 0~3을 실행해 통과했고, dev 계약 기본 검증도 통과했다. `--strict`는 main에만 있는 `attendance_streak_seeds`와 `user_daily_stats.mood_reward_claimed_at`만 추가 객체로 보고하며, 승격 머지 계약에는 둘 다 들어간다. 개발 DB에는 `hay_transactions_types.sql`을 2026-10-28 적용했다. 2번은 목록을 넓히기만 해 실행 중인 구버전 코드에는 영향이 없다. 다만 배포 preflight는 CHECK 정의 차이를 거부하므로, 2번 이후에는 main의 이전 이미지 재배포·`workflow_dispatch` 복구가 `changed constraints: public.hay_transactions.hay_transactions_type_check`로 멈춘다(시뮬레이션에서 확인). 그래서 1~3은 승격 머지 직전에 실행한다. 승격 전 이미지로 복구해야 하면 `training_reward`·`article_reward` 원장 행이 0건일 때만 main `schema.sql`의 type 목록으로 CHECK를 되돌린 뒤 복구한다.
- 순서: **SQL 적용 → `db.verify` → main 머지·배포.** 새 코드는 `articles`가 없으면 아티클 조회가 실패한다. 이전 이미지는 새 테이블을 쓰지 않고 CHECK 제거는 제약 완화라, SQL 적용 후 롤링 중 구/신 혼합도 안전하다. `db.verify`는 이 릴리스 커밋에서 실행한다.
- 실행: [배포 순서](#리뷰-후-머지-직전)의 명령에 `RELEASE_SQL`과 표의 SHA를 넣는다. `article_progress`·`hay_transactions`에 짧은 ACCESS EXCLUSIVE lock이 걸린다(2번은 기존 원장 행을 다시 검사한다). 기본 lock 2초 제한에 걸리면 rollback 후 한가한 시점에 재시도한다. `explore_content.sql` 재실행은 첫 `DROP CONSTRAINT`부터 실패하고 rollback되므로 이미 적용된 DB에 다시 실행하지 않는다.
- 사후 확인(인증 필요): `GET /articles`가 5개, `GET /article-progress`가 같은 id 5개를 반환한다. 미인증은 401이다.
- 되돌리기: 코드는 이전 이미지로 되돌리면 되고 테이블은 남겨도 된다. DB까지 되돌릴 때만 `DROP TABLE public.articles;`를 실행한다. CHECK는 `SELECT count(*) FROM public.article_progress WHERE article_id NOT IN ('sample-1','sample-2','sample-3','sample-4','sample-5','sample-6','sample-7')`가 0일 때만 `explore_articles.sql`의 정의로 다시 추가한다. 새 id의 진행·보상 기록이 있으면 지우지 않는다.
- 내용 변경은 검토한 SQL을 같은 `db.apply` 절차로 실행한다. 원고는 한국어가 원본이고 번역이 없는 언어는 한국어로 보인다.

```sql
-- 번역 추가·수정: 언어 키 하나를 통째로 바꾼다({"title", "summary", "blocks": [...]})
UPDATE public.articles SET content_i18n = content_i18n || jsonb_build_object('en', '<검토한 JSON>'::jsonb) WHERE id = 'sample-1';
-- 숨김. 새 글은 새 id로 INSERT한다. 보상이 사용자·id당 한 번이라 다른 글에 기존 id를 재사용하지 않는다.
UPDATE public.articles SET is_active = false WHERE id = 'sample-3';
```

오늘의 한마디(`GET /daily-affirmation`)는 `public.daily_affirmations`의 현지 날짜 이하 가장 최근 행을 보여 준다. 문장을 날짜별로 추가하며 이미 지난 날짜의 행은 바꾸지 않는다. 개발 DB의 2026년 행은 임시 문장이다.

```sql
INSERT INTO public.daily_affirmations (local_date, text_ko, text_en, text_ja)
VALUES ('2027-01-01', '<검토한 한국어>', '<검토한 영어>', '<검토한 일본어>');
```

## 변경 시 확인할 원본

- DB·권한·반영: [db/README.md](../db/README.md), `db/schema.sql`, `db/schema_contract.json`.
- 구독·체험·RC/건초: [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md).
- 사용자 노출 자산: [배너](BANNER_SDUI.md), [BGM](REMOTE_BGM.md), [운세](DAILY-FORTUNE.md).
- HTTP 계약: [API_SPEC.md](API_SPEC.md), `openapi/`. 배포 당시 검사 결과는 PR/Actions에서 해당 SHA와 함께 확인한다.
