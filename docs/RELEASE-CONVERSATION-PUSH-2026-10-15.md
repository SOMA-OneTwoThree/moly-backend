# 대화·푸시·운영 알림 출시 절차

이 문서는 이번 변경의 PR 리뷰부터 운영 머지·배포·복구까지의 절차다. **문서와 PR 작성은 운영 SQL 적용·머지·배포를 실행했다는 뜻이 아니다.** 기존 main 자동 배포와 프로젝트 전용 접속 경로를 사용한다. 개인 AWS 계정·새 SSH 접속·임의 workflow로 대체하지 않는다.

## 1. 범위와 확정 정책

| 묶음 | 이번 범위 | 유지할 계약 |
|---|---|---|
| 대화 | 세 언어 말투·시스템 정보권한, 인용/줄바꿈/임의 물음표 후처리, 잘림·빈 답 발행 방지, 회상 결과 없음과 조회 실패 구분, 필요한 문맥·시간 정합성 | 기존 모델·추론·호출 예산, 발행/잠금·취소·privacy fence·원장·실패 API 계약 |
| 사용자 푸시 | 현지 09시 루틴·10시 일기·20시 공지/스트릭/대화, 문구·TTL·deeplink·저녁 첫마디 연결 | 기존 FCM 오류 분류·무효 토큰 처리·사용자 설정·날짜별 claim·worker deadline·타임존 처리 |
| 운영 알림·번역 | 지속 사건의 변화/악화/재알림/복구·하루 요약, RC 환경 분류·추정 비용/unknown 요약, 외국어 피드백 번역 | 원문 피드백·기존 API 경보·synthetic/deadman·큐 replay 이력·안전한 로그 |

- 저녁 `diary_teaser`는 삭제한다. 아침 일기 알림을 끈 사용자는 일기 푸시를 받지 않는다. 재확인 대기 정책이 아니다.
- 운영 아침 일기 활성 상태는 유지한다. 코드의 `morning_push_enabled=False` 기본값으로 기존 운영 값을 덮지 않는다.
- 새 `routine_morning`·`routine_risk`는 기본 false다. 1단계 배포 후 **최소 2주** 관측한 뒤 `routine_risk`부터, 이후 `routine_morning` 순서로 별도 활성한다. 스모크 통과 직후 켜지 않는다.
- 두 루틴 flag를 꺼도 10시 이동·20시 일반 대화·문구·teaser 삭제가 되돌아가지는 않는다.
- 오늘 감정은 허용된 다섯 감정 중 오늘 선택한 enum 한 개만 내부 전달한다. 본문·과거 감정은 제외하고, 오늘 앱에서 선택한 감정 이름을 명시적으로 물었을 때만 쓴다. 일반 기분 질문이나 말투·주제·질문 조정에 쓰지 않는다.
- 기존 운세·카탈로그·구독/체험·루틴·BGM 표시·광고 정책은 바꾸지 않는다. 과거 실험·구형 정책·불필요한 DB/저장소 변경을 함께 이식하지 않는다.

## 2. PR을 만들기 전

1. 최신 main 기준 별도 통합 브랜치에서 이번 범위만 합친다. 다른 작업의 미커밋 원본을 보존한다. chat/tick/schema_contract 충돌은 main의 운영 보호를 기준으로 최소 통합한다.
2. 기존 개발 실험을 유지하며 선택 기능을 검증한다. main 전체 이미지를 개발 서버에 덮지 않는다. 기존 개발 DB에 있는 범위만 합성 fixture·외부 rollback으로 검사한다. 없는 루틴 네 테이블 관련 경로는 SQL·단위 계약 검사이며 실제 DB 루틴 E2E는 미검증이다. 부족한 운영 정책 RPC를 개발에 복사하지 않는다.
3. 말투·실패 계약을 세 언어로 확인한다. 반복·상대 말 재설명·약속만 하는 답·불필요 질문과 잘림/빈 답·인용·줄바꿈·물음표·회상 실패 구분·오늘 감정 권한을점검한다. 테스트 ASGI의 가짜 SDK 경로와 실제 개발 API·모델 결과를 구분한다. 최종 이미지가 아직 개발에 배포되지 않았다면 이전 버전 결과를 최종 통합본 정상 API 통과로 사용하지 않는다. 단발 모델 비교도 정상 경로 통과를 대신하지 않는다.
4. 푸시는 설정·09/10/20 분기·선점/실패·TTL·토큰 처리·deeplink·인사 연결을 검증한다. 현재 개발 FCM이 설정되지 않은 범위는 가짜 전송으로 계약을 검사하고 **실제 기기 전달은 미검증**으로 남긴다. 운영 첫 발송 관측을 사전 E2E 통과라고 쓰지 않는다. 자동 worker를 임의로 켜지 않는다.
5. 번역·Slack도 가짜 전송 검사와 실제 전달 확인을 구분한다. 혼합 한국어가 번역 호출을 시도할 수 있고, 응답을 잃은 비용은 unknown이다. 본문만 번역하며 식별 정보·provider 본문·키를 로그로 남기지 않는다.
6. 최종 같은 snapshot에서 단위·개발 DB·정상 API·공개 API 계약 검증과 아래 정적 검사를 마친다. 기존 부분 준비 시험 숫자를 합친 전체 결과로 사용하지 않는다.

```bash
uv run pytest -q --ignore=tests/integration
uv run ruff check app worker scripts tests db
git diff --check
```

DB 계약은 최신 main의 카탈로그·정책을 보존하고 `schema.sql`과 `schema_contract.json`의 두 컬럼만 한정 조립했다. 원격 CI의 `uv run python -m db.schema_contract --check-generated`로 재생성 결과를 대조한다. 통과 여부는 PR check에서 확인한다. CI 실패 시 원인을 수정하며 로그 값을 반복 복사해 맞추지 않는다. 검증은 기존 개발 API·Supabase를 사용하며 새 로컬 DB를 띄워 대체하지 않는다.

7. 구현·검증·수정이 끝나면 최종 경로를 명시해 staging하고 draft PR **한 개**를 만든다. SQL·해시·시험 결과·실기기 한계·아래 적용/검증/복구 절차를 첨부한다. 이 시점의 운영 DDL·머지·배포는 0이다.

### 실행 결과를 구분해 기록할 것

| 증거 | 현재 확인 범위 | 대신할 수 없는 것 |
|---|---|---|
| 최종 통합 단위·ASGI 시험 | 최종 전체 실행 결과와 source SHA는 PR에 기록한다. timeout/잘림·빈 답은 ASGI 계약으로, 정상 정제는 서비스 합성 mock으로 구분 | 실제 모델 말투·기기 수신·외부 전달 |
| 개발 DB·푸시 검증 | 실제 SQL dry-run/ROLLBACK·기존 컬럼 경로 확인. 개발 DB 임시 표에서 신규 ADD·재적용·타입/default/NOT NULL/typmod 오류 6검사 통과, 모두 rollback·영구 schema 변경0. 아침 일기 통합 15개는 FCM mock·임시 사용자 정리 확인이며 전체 단위시험과 별개 범위 | 임시 표 SQL 검증이며 운영 적용·실제 루틴 DB E2E·기기 수신은 미검증 |
| 실제 개발 API | 최종 통합 이미지 미배포 상태에서는 이전 버전의 결과뿐 | 최종 통합 이미지 정상 경로 검증 |
| 실제 FCM·기기 | 개발 FCM 미설정 범위는 미검증 | 가짜 전송·운영 첫 발송 관측으로 사전 E2E 통과 주장 |
| 스키마 생성 계약 | main 계약에 두 컬럼 한정 조립; 원격 CI `check-generated`로 재생성 대조, 통과 여부는 PR check에서 확인 | CI 완료 전 생성 계약 일치 확정 |

## 3. PR 리뷰 후, 머지 직전

### 3.1 사전 확인

- `git ls-remote origin refs/heads/main`으로 리뷰 기준 main이 바뀌었는지 확인한다. 바뀌었으면 정책·충돌·최종 snapshot을 재대조한다.
- `gh run list --workflow deploy.yml --limit 5`로 진행 중/대기 중 배포를 확인한다. 동시 작업을 시작하거나 진행 중 롤링을 취소하지 않는다.
- 기존 운영의 실제 버전·이전 ECR tag/digest·infra SHA·ENV 복구 기준을 프로젝트 전용 경로로 확인·기록한다. 현재 알려진 복구 후보는 `42b57fd108c7ff128819d08d406f67c4f450f42e`이며 **배포 직전 재확인해야 하는 값**이다.
- 두 API 호스트·consumer·worker 담당 한 대와 잔류 tick, 운영 아침 활성 값·FCM 설정을 확인한다. 미설정 기본값을 운영 실효값으로 해석하지 않는다.
- 11:00~19:30 KST는 한국·일본 주요 시각을 피하는 권장 창일 뿐 글로벌 안전 시간이 아니다. 04/09/10/20시는 사용자 현지 기준이다. 빌드·롤링 소요와 기존 worker 실행도 확인한다.

### 3.2 적용할 SQL

대상은 [push_routine_and_evening_variant.sql](../db/changes/push_routine_and_evening_variant.sql)의 **두 nullable 컬럼뿐**이다.

| 테이블·컬럼 | 정의 | 영향·복구 |
|---|---|---|
| `public.user_daily_stats.routine_notified_at` | `timestamptz`, NULL 허용, 기본값 없음 | 루틴 아침 선점. 기존 행 backfill 없음 |
| `public.user_daily_stats.evening_push_variant` | `text`, NULL 허용, 기본값 없음 | 저녁 문구 연결. 기존 행 backfill 없음 |

SQL SHA-256은 `06d86f76085eda1df241decbab568739a03670dddde79aaba00150113ff56297`이다. 개발에는 두 컬럼이 이미 있으므로 재ADD하지 않는다. 운영에는 PR에 고정한 SQL만 적용하며 schema/seed/config를 통째 반영하지 않는다. 기존 이미지가 추가 nullable 컬럼을 무시할 수 있는지 검토하고, SQL 파일이 바뀌면 해시 검토부터 다시 한다.

```bash
RELEASE_SQL=db/changes/push_routine_and_evening_variant.sql
# PR에서 리뷰한 64자리 SHA-256을 입력한다. 파일에서 자동 계산해 승인값으로 삼지 않는다.
REVIEWED_SQL_SHA=06d86f76085eda1df241decbab568739a03670dddde79aaba00150113ff56297
uv run python -m db.verify --env prod
uv run python -m db.apply "$RELEASE_SQL" --env prod --expected-sha256 "$REVIEWED_SQL_SHA"
# 위 기본 실행은 ROLLBACK. 결과 검토 후에만 실제 적용한다.
uv run python -m db.apply "$RELEASE_SQL" --env prod --commit --allow-prod --expected-sha256 "$REVIEWED_SQL_SHA"
uv run python -m db.verify --env prod
```

운영 읽기 전용 연결에서 다음 metadata만 확인한다. 조회 결과를 PR에 첨부할 때 연결 정보나 사용자 행을 함께 출력하지 않는다.

```sql
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'user_daily_stats'
  AND column_name IN ('routine_notified_at', 'evening_push_variant')
ORDER BY column_name;
```

- 첫 `db.verify`의 예상 차이는 두 컬럼이다. 그 밖의 타입·NULL/default·권한·함수·정책 차이가 있으면 중단한다. 사후 검증은 최종 계약과 맞아야 한다.
- metadata로 두 컬럼의 타입·nullable/default를 확인한다. 기존 컬럼이 같은 이름인데 정의가 다르면 덮지 않는다. SQL의 타입/존재 가드를 검토한다.
- 기본 lock 2초·statement 60초 제한을 늘리지 않는다. 잠금·검증·대상 보호·해시 실패면 rollback하고 **머지하지 않는다**. 자동 재시도하지 않는다.
- 적용 후 현재 기존 이미지의 health/ready·API·consumer 동작이 정상인지 확인한다. DB가 정상이어도 기존 이미지 회귀가 있으면 머지하지 않는다.
- 정상이라면 리뷰된 정확한 소스 SHA를 main에 머지한다. main push가 자동 배포를 시작하므로 SQL과 기존 이미지 확인은 반드시 그 전에 끝낸다.

## 4. 머지 후 자동 배포와 첫 관측

기존 `.github/workflows/deploy.yml`을 유지한다. 워크플로는 이미지·자산 검사와 **두 호스트 모두의 사전점검 후** 한 대씩 deregister/drain/배포/register한다. 실패한 호스트나 다음 호스트를 임의로 건너뛰지 않는다.

```bash
gh run list --workflow deploy.yml --limit 5
DEPLOY_RUN_ID='<이번 main 배포 run id>'
gh run watch "$DEPLOY_RUN_ID" --exit-status
curl --fail --silent --show-error https://voice.moly.asia/health
curl --fail --silent --show-error https://voice.moly.asia/health/ready
```

1. 실행 결과에서 정확 release SHA·production·ready/DB 정상, 대상 수와 등록 수 일치, API/consumer 같은 이미지, worker timer 한 대·중복/잔류 tick 없음, 기존 자산·synthetic 결과를 확인한다. 공개 health만으로 내부 consumer/worker 정상이라고 하지 않는다.
2. 운영 아침 활성 상태를 유지하고 새 루틴 두 flag는 false로 둔다. 다음 현지 슬롯에서 09시 루틴 0·10시 일기·20시 기존 일반 분기, 무효 토큰/전송 실패·TTL·인사 연결을 관측한다. FCM 수락은 실제 기기 수신과 다르다. 저녁 variant는 claim 시 비워 두고 수락 건수>0 뒤에만 best-effort 저장하므로 실패 응답을 첫마디로 연결하지 않는다. 수락 후 프로세스 종료·기록 실패 시에는 첫마디 연결이 누락될 수 있다.
3. 큐는 `app/services/jobs.py`의 `_STATS_SQL` 기준으로 성공 replay의 **모든 조상**을 제외한다. raw dead 수를 미해결로 보고하거나 성공 이력을 삭제/재실행하지 않는다.
4. 비용 원장의 unknown·timeout·미해결 lease와 알림의 변화/복구를 확인한다. 추정 비용은 청구서가 아니다. Slack 전송 뒤 ack 전 종료의 중복 가능성과 15초 총 한도 후 다음 tick 유예를 구분한다.
5. 이번에 필요한 temporal 쓰기를 포함했다면 `context_checkpoint_temporal_enabled` 새 활성화는 별도로 한다. API/consumer 버전·pending 호환 검증 전 새로 켜지 않으며 기존 값을 무조건 덮지 않는다.
6. 최소 2주 관측 후 별도 활성 단계에서 `routine_risk`를 먼저 켜고 첫날 실패/분포를 확인한 뒤 `routine_morning`을 켠다. 다른 `app_config` 키를 지우는 전체 JSON 교체를 하지 않는다.

## 5. 중단·복구

다음이면 배포 확대·새 활성화를 중단한다: ready/SHA/consumer 불일치, 정책·privacy·소유권 회귀, 중복 worker, 잘림/빈 답 정상 발행, 푸시 중복/잘못된 시간대·설정 무시, 미해결 큐/lease·timeout·unknown 증가. 근거 없는 새 임계 숫자로 정상 처리하지 않는다.

1. 진행 중인 롤링을 무조건 취소하지 않는다. 실패 상태와 deregister된 호스트를 확인하고 기존 프로젝트 복구 경로를 사용한다.
2. 이전 이미지 tag/digest·ENV·infra를 재확인한다. 이미지 롤백이 SSM·systemd·infra·DB까지 되돌리지는 않는다. 현재 복구 후보 full SHA를 확인한 뒤 기존 workflow로 복구한다.

```bash
ROLLBACK_SHA=42b57fd108c7ff128819d08d406f67c4f450f42e
# 실제 이전 배포 버전·보존 digest·설정을 확인한 뒤만 실행한다.
gh workflow run deploy.yml --ref main -f image_tag="$ROLLBACK_SHA" -f expected_instances=2 -f verify_morning_push=true
```

3. 복구 run의 ready·SHA·등록 수·consumer·worker 한 대·기존 아침 활성 상태를 다시 확인한다. 새 루틴 flag를 끄는 것만으로 일반 푸시 변경이 복구되는 것은 아니다.
4. temporal 쓰기를 새로 켰다면 먼저 중지하고 pending과 기존 버전 읽기 호환을 확인한다. 새 DB 컬럼은 **삭제하지 않는다**. 원장·claim·성공 replay·변경 이력을 보존한다.
5. SQL 실패/rollback은 애플리케이션 배포를 강행할 이유가 아니다. 필요한 최소 차이만 다시 검토하고 동일 요청을 자동 재시도하지 않는다.
