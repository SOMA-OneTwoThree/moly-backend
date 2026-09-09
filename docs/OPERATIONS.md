# 백엔드 운영 계약

코드의 기본값은 `app/config.py`, 호스트의 환경변수·컨테이너·nginx·타이머는 moly-infra가
소유한다. 이 문서는 배포와 장애 대응, 계속 보존해야 하는 데이터 조건을 설명한다.
DB 초기화·구조 검증·수동 SQL 적용은 [db/README.md](../db/README.md)를 따른다.

## 실행과 배포

API와 상주 consumer, 15분 배치는 같은 이미지를 다른 명령으로 실행한다.
HTTPS는 ALB에서 ACM으로 종료하고 nginx :8080 → API 127.0.0.1:8000으로 전달한다.
계정 API는 moly-auth가 담당하며 같은 환경의 Supabase 데이터를 사용한다.

| 환경 | 배포 원본 | 이미지·대상 |
|---|---|---|
| prod | `.github/workflows/deploy.yml`, main | `moly-backend:<sha>`, 기본 2대 순차 롤링 |
| dev | `.github/workflows/deploy-dev.yml`, dev | `moly-backend-dev:dev-<sha>`, 1대 중단 배포 |

prod는 대상 수를 확인하고 한 호스트씩 ALB에서 제외·drain → SSM 배포 → 검증 → 재등록한다.
실패하면 다음 호스트로 진행하지 않는다. dev는 별도 IAM·대상 태그·ECR을 사용하며 ALB를 조작하지
않는다. 두 workflow는 진행 중 배포를 취소하지 않고 뒤 배포를 대기시킨다.

스키마 변경은 공유 DB를 사용하는 moly-auth와 구·신 backend 이미지의 호환성을 먼저 검토한다.
검토한 SQL을 수동 적용하고 구조 검증을 통과한 뒤 코드를 배포한다. 배포 자체는 DB를 변경하지 않는다.
infra 변경은 저장소 반영만으로 실행 중 프로세스에 적용되지 않으므로 검증한 이미지로 재배포한다.

`deploy.sh`는 SSM에서 후보 env 생성 → 이미지 pull → 읽기 전용 DB preflight → live env 교체 →
API·consumer 기동 → 헬스·이미지·nginx 검사 → 워커 타이머 갱신 순서다. 새 이미지는 전체 스키마
계약을 검사하고, 검증 모듈이 없는 구 이미지는 운세·광고 구조 검사로 롤백을 지원한다.
preflight 실패 시 기존 `.env`·`backend.env`·FCM 파일을 유지한다. FCM도 후보 파일에서 검증 성공
후 반영한다. 이후 컨테이너 기동 실패까지 모든 파일을 자동 rollback하는 것은 아니다.

롤백은 해당 workflow의 `workflow_dispatch`에서 검증된 기존 `image_tag`를 지정한다.
prod의 `expected_instances`는 기본 2다. 1로 줄이면 남은 호스트를 제외하는 동안 서비스가
비므로 가용성을 바꾸는 운영 판단이다. 호스트에서 컨테이너를 직접 교체해 롤링 절차를 우회하지 않는다.
호스트는 해당 저장소의 최근 이미지 3개를 유지하며 ECR 보관 정책과는 별개다.

## 설정과 프로세스

비밀값은 환경별 SSM `/moly/dev/`, `/moly/prod/`에 둔다. 필요한 키와 실제 환경변수 매핑은
moly-infra `deploy.sh`가 원본이다. 생성된 `.env`·`backend.env`·`secrets/`를 직접 편집하지 않는다.
앱의 실행 중 조정값은 `app_config`, 소비자 동시성·lease와 배포 플래그는 환경변수로 관리한다.

`/etc/moly-env`가 없으면 prod, 내용이 `dev`면 dev다. 빈 내용이나 잘못된 값은 배포 오류다.
`/etc/moly-worker-host`가 있는 단일 호스트에만 `moly-worker.timer`를 활성화한다.
상주 consumer는 각 호스트에서 `FOR UPDATE SKIP LOCKED`로 잡을 나눠 처리한다.

배치는 매시 :00·:15·:30·:45에 실행된다. 사용자 현지 04시 일기, 09시 아침 푸시(기본 꺼짐),
20시 저녁 푸시를 처리한다. 고유 타임존을 Python에서 해석한 뒤 해당 문자열의 사용자만 조회한다.
잘못된 타임존 한 건이 전체 틱을 실패시키는 SQL `AT TIME ZONE` 전환은 하지 않는다.
RevenueCat 수신함·기억 재개·retention 예약·하트비트는 사용자 루프와 별개로 처리한다.

## 관측과 장애 대응

```bash
# 호스트에서
cd /root/moly-infra
docker compose --env-file .env -f docker-compose.yml ps
docker logs --tail 200 moly-backend
curl -fsS http://127.0.0.1:8000/health/ready
curl -fsS http://127.0.0.1:8080/health
systemctl list-timers moly-worker.timer
systemctl status moly-worker.service
journalctl -u moly-worker.service -n 100
```

- 외부 연결 장애: ALB 대상 상태 → nginx :8080 → API readiness → 컨테이너 로그 순으로 확인한다.
  TLS 인증서는 ALB의 ACM 설정을 확인한다. 호스트 certbot 갱신 절차는 사용하지 않는다.
- 배포 실패: Actions의 SSM 출력과 실패한 호스트 로그를 확인한다. preflight 차이를 먼저 해결하고
  같은 검증 이미지로 재시도한다. 검증을 건너뛰거나 DB 잠금 제한을 늘려 통과시키지 않는다.
- 큐 적체: `/health/queues`의 ready/running/dead와 미해결 dead의 나이를 확인한다. 오래된 dead를
  시간 필터로 숨기지 않는다. 원본 행을 ready로 되살리지 않고 `replay_of`가 있는 새 작업으로 재시도한다.
- 워커·정리 중단: 타이머 마커와 마지막 성공 기록, `/health/deep`을 확인한다. 월간 정리는
  `async_jobs` 행 존재가 아니라 `app_config`의 마지막 성공 시각으로 판정한다.
- `/health/ready`는 DB 연결 상태를 확인한다. `/health/deep`, `/health/queues`, `/health/synthetic`은
  `X-Health-Token`을 요구한다. synthetic은 실제 모델 호출을 하므로 비용이 발생한다.

## 데이터 수명 주기

원본 조건은 `worker/retention_jobs.py`다. 정리는 maintenance 큐에서 배치별로 커밋하며,
한 번에 최대 2,000건·한 실행에 최대 10배치를 처리한 뒤 잔량은 다음 잡으로 이어간다.
일별 잡은 KST 05시 이후 그날 첫 성공 예약으로 수렴하고 결제 이벤트 정리는 매월 1일 예약한다.

| 대상 | 정리 조건 | 반드시 남기는 것 |
|---|---|---|
| 중복 방지 키 | `dedupe_expires_at IS NOT NULL AND <= now()` | NULL 만료인 상점 구매 키는 영구 보존 |
| AI 비용 원장 | 90일 지난 completed/failed를 started_at 기준 KST 날짜로 집계·삭제 | started·unknown_usage는 삭제하지 않음 |
| 작업 | 14일 지난 succeeded/cancelled, replay_of 없음, 참조하는 replay 자식 없음 | dead와 재처리 사슬 전체 |
| 기억 후보 | committed 14일: 판정 커서 통과, dead 90일 | planned와 pending registry가 참조하는 후보 |
| RevenueCat 이벤트 | processed_at 기준 365일 지난 processed | pending·failed와 결제·주문 원장 |
| 단명 잠금 | 일기 claim·대화 lease가 만료 기준 7일 초과 | 살아 있는 claim·lease |

비용 집계와 원본 삭제는 한 SQL 문장의 `DELETE RETURNING → INSERT ON CONFLICT`로 실행한다.
`activity_date`는 백그라운드 비용에서 의도적으로 NULL일 수 있어 집계 기준으로 바꾸지 않는다.
집계한 비용과 호출 수가 삭제 전 합계와 같아야 하며, unknown 값을 0원으로 확정하지 않는다.
LLM 호출 전 `open_call` 기록은 선커밋한다. 호출 이후 기록만 묶어 처리할 수 있다.

`relationship_events`는 전체 이력으로 관계 상태를 다시 계산하므로 기간으로 잘라내지 않는다.
후보 삭제는 pending 기억 재판정의 입력을 보존해야 한다. 벡터 삭제가 일괄 실패했다고 전체를
failed로 확정하지 않으며, 재시도 가능한 pending 상태를 유지하는 기존 경로를 따른다.

## 성능 변경 검토 기준

성능은 같은 입력의 결과·순서·날짜·오류 코드·중복 처리·비용 집계가 같은지 먼저 확인한다.
조회 횟수와 로컬 측정값, 운영 관측값은 구분한다. 과거 누적 pg_stat_statements 평균은 현재
릴리스의 지연 시간으로 사용하지 않는다.

- 회상에서 사용자 필터·장부 검증보다 먼저 전역 ANN LIMIT을 두지 않는다. 일기 조회도 벡터 없는
  경로·NULL 임베딩·전체 개수와 no_content_match 판정을 보존한다.
- `turn_context`의 savepoint는 오류 후 외부 트랜잭션이 오염되는 것을 막는다. 전체 제거하지 않는다.
- 시간 기준은 호출자가 전달한 `now`와 도메인 날짜 경계를 유지한다. SQL `now()`로 임의 대체하지 않는다.
- 풀 크기는 API·consumer·worker와 롤링 중 구·신 프로세스의 합으로 산정한다. 개별 프로세스만 보고 늘리지 않는다.
- 인덱스 변경은 현재 쿼리의 EXPLAIN과 대체 인덱스를 확인한다. 사용 횟수 0만으로 삭제하지 않는다.
  서비스 시간에는 검토한 CONCURRENTLY 절차와 lock timeout을 사용하고 valid/ready를 확인한다.
- DB maintenance 도구로 다른 세션을 취소·종료하지 않는다. pg_repack은 사용 시 확장/CLI 버전과
  `--no-kill-backend`를 확인한다. Supabase가 소유한 스키마의 내부 인덱스는 정리 대상이 아니다.
- 가격·번역·참조·커서의 NULL은 [DB 정책](../db/README.md)을 먼저 확인한다. 기본값 보충이나 NOT NULL
  추가를 단순 정리로 취급하지 않는다.

완료된 재설계·전환·측정 보고서는 Git 이력에서 조회한다. 이 문서에는 현재 동작과 보존 조건을
갱신하며, 과거 실행 체크리스트나 미완료 계획을 누적하지 않는다.


## 수동 기억 복구 도구

저장소 루트에서 실행하며 `--env dev|prod|env-file`로 DB를 선택한다. 생략 시 기본 `.env`다.
SQLAlchemy 도구도 선택 파일의 DSN을 실제 엔진에 적용하고, 이미 만들어진 풀의 대상 변경은
거부한다. LLM 도구는 시작 전에 같은 환경 파일을 설정하며 셸에 주입한 공급자 설정이 있으면
앱의 일반 설정 우선순위가 적용된다. 실행 환경의 DB·공급자 설정을 함께 검토한다.

| 도구 | 기본 동작 | 반영 시 동작 |
|---|---|---|
| `preflight_cutover.py` (`verify_cutover_gate.py` 호환 명령) | 현재 schema.sql 구조 검사 + 읽기 전용 현황 | 쓰기 없음. NULL·legacy·보관 이력은 관찰값 |
| `enter_shadow_cohort.py` | 1명 진입 시뮬레이션 후 롤백 | `--yes`: 과거 상한 고정, ready 전환, 첫 실제 턴 작업 하나 |
| `reextract_memories.py` | 최대 3명 후보 표시, `--status` 현황 | `--apply`: 옛 기억을 숨기고 세대 증가·커서 초기화·첫 작업 등록 |
| `resume_reextract.py` | 커서가 밀린 유휴 사용자 후보 표시 | `--apply`: 현재 커서의 작업 하나, 유효한 dead는 replay 이력 연결 |
| `repair_memory_backlog.py` | 최대 10명 잔여 작업 후보 표시 | `--yes`: 미판정·공급자 삭제 작업 등록, 벡터·장부 없는 오래된 계획만 닫음 |
| `replay_dead_memory_jobs.py` | 현재 처리기의 unknown_job_type 후보 | `--execute`: 기존 개발 DB 제한 유지. 폐기된 이름은 변환하지 않음 |
| `verify_shadow_entry.py` | 잠금으로 보호한 진입 경로 검사 후 롤백 | 커밋하지 않음. 실제 최소 턴과 비교하므로 번호 공백 허용 |
| `backfill_user_schedules.py` | 스케줄 누락·시간대 재계산 후 페이지별 롤백 | `--yes`: 페이지별 커밋, 사용자별 savepoint, 종류별 수량 확인 |
| `backfill_interaction_contracts.py` | LLM 추출 후 개수 표시 | `--yes`: draft만 저장, 자동 publish 없음 |
| `run_memory_consumer.py` | 60초 동안 memory 큐 처리 | 즉시 실작업. `--seconds` 후 새 claim 중단, 실행 중 작업 마무리 |
| `backfill_memory_batches.sh` | 명시한 환경의 1묶음 등록 미리보기 | `--yes`: 제한된 묶음 등록·큐 처리, 실패 또는 시간 내 미완료 시 중단 |

재추출·재개·잔여 복구는 활성 삭제 장벽, 현재 개인정보 버전, 준비된 파이프라인을 확인하고
사용자별 짧은 트랜잭션으로 처리한다. 실행 중 기억 작업이나 잠금 경합은 중단 사유다.
대기 작업은 잠그되 실행 중 작업을 취소하지 않는다. 만료·비식별화된 payload나 이미 재처리한
terminal 작업을 임의 키로 우회하지 않는다. 사용자별 커밋이므로 중간 실패 이전의 성공은 남는다.
진입 시 최초 작업 키가 기존 작업과 충돌하면 해당 사용자의 ready 전환도 함께 롤백한다.

재추출 도중에는 선택한 사용자의 기존 기억이 회상에서 숨겨진다. `--rollback`은 원본 벡터와
보존 상태를 확인한 뒤 표시된 옛 기억을 되살리고, 기존 정책대로 커서를 현재 source까지 맞춰
재추출을 중단한다. 그동안의 새 결과와 미처리 구간을 검토해야 하며 전체 데이터 시점 복구는 아니다.

합의 추출은 미리보기에서도 외부 LLM 비용과 사용량 기록이 발생한다. 네트워크 호출 동안
DB 트랜잭션을 닫고, draft 저장 전 원문·개인정보 버전을 다시 검사한다. 수동 consumer는
등록한 사용자뿐 아니라 선택한 환경의 memory 큐 전체를 처리한다. 운영에서 이 도구를 실행하는
것은 별도 작업이며 배포 과정에는 포함하지 않는다.
수동 consumer의 dead 판정은 운영 큐 통계와 동일하게 성공한 replay의 모든 조상을 제외한다.
과거 dead 이력은 보존한다. 큐 감시가 실패해도 새 작업 획득을 멈추고 실행 중 작업을 마친 뒤
오류를 반환한다.

## 주간 운영자 일기 DB 전환

운영 머지 전에 [검토용 차이 SQL](../db/changes/weekly_operator_diaries.sql)을 해당 운영 DB에
적용하고 구조 검증을 완료해야 한다. main 머지는 자동 배포를 시작하므로 **머지 후 DB 적용은 늦다**.
개발 작업에서는 운영 DB를 변경하지 않는다. SQL은 이번 릴리스 인계용이며 자동 실행하지 않는다.

변경은 기존 원고에 `week_start_date`·`sequence_no`, 기존 결과에 `preset_ment_id` 추가뿐이다.
날짜별 원고·과거 일기를 변환하거나 복사하지 않는다. 기존 결과는 no_entry/NULL로 그대로 유효하다.
사용자·원고 중복 금지, 원고 주/순서 조합, 결과 상태 조합 및 원고 삭제 제한을 함께 추가한다.

### 적용 순서

1. **기존 개발 API·Supabase DB에서 먼저 검증한다.** `.env`의 개발 프로젝트를 도구의 대상 검사로
   확인하고 기존 schema contract와 실제 DB 구조가 일치하는지 검사한다. 검토한 차이 SQL 적용 후
   주간 동시 생성/rollback 테스트를 수행한다. 테스트는 별도 생성한 사용자·원고만 쓰고 종료 시
   정리한다. SSH·개인 AWS 계정·로컬 Docker/DB가 필요하지 않다.
   `MOLY_WEEKLY_TEST_ENV=dev uv run pytest -q tests/integration/test_weekly_diary.py`가 개발 DB
   검증 경로다. 테스트 임베딩 잡은 미래로 예약하여 개발 소비자의 외부 호출을 막는다.
2. 실제 대상 DB에서 `moly_life_ments`·`diary_generation_results`의 행 수/크기와 현재 제약을
   읽기 전용으로 확인한다. 신규 컬럼/제약 일부만 있는 상태라면 아래 SQL을 그대로 재실행하지 않는다.
   신규 UNIQUE 구축이 잠금 예산을 넘을 크기라면 별도 검토한 concurrent index 절차를 사용한다.
3. 전환 설정은 미설정으로 둔 상태에서 DB 차이를 먼저 적용한다. 기본 lock 2초, statement 60초를
   넘으면 rollback 후 재시도하며 제한을 임의로 늘리지 않는다. 구버전 DB verifier는 추가 CHECK/
   UNIQUE를 거부할 수 있으므로 DDL 후 구버전 자동 배포를 별도로 재실행하지 않는다.
4. 새 코드 전체(API/워커)를 배포하고 검증한다. 새 코드의 원고 ORM 조회는 신규 컬럼을 포함하므로
   전환 설정이 꺼져 있어도 **DDL보다 코드가 먼저 배포되면 안 된다**.
5. 구 워커 종료를 확인하고 주간 원고를 등록·검수한 뒤 app_config의 `diary_weekly_start_date`를
   미래 월요일 ISO 날짜 JSON 문자열로 설정한다. 가장 빠른 사용자 시간대의 첫 주간 생성 전에 준비한다.
   일요일 일기의 월요일 공개는 이전 주 활동일 기준으로 처리한다.
6. 주간 preset 결과를 만든 이후에는 전환일을 변경/삭제하거나 구 legacy 바이너리로 회귀하지 않는다.
   장애 시 주간 결과를 이해하는 버전으로 수정 배포하거나 일기 생성 작업만 중지한다. 이력·컬럼·
   제약을 삭제해서 복구하지 않는다. 원고 미등록은 정상 미발행이며 개인 LLM 오류와 구별한다.

DDL 직후 검증은 **이번 릴리스 후보 코드와 재생성된 schema_contract.json**으로 실행한다.
이번 릴리스의 계약은 변경 전 개발 DB가 기존 계약과 정확히 일치함을 확인한 뒤, 검토한 3컬럼·
제약·인덱스 변경만 생겼음을 검사하고 PostgreSQL 카탈로그에서 생성했다. CI에서는 별도의 빈
PostgreSQL 17에 canonical schema를 실행해 동일 계약이 재현되는지 독립 검사한다.
기존 main checkout의 검증 계약으로 실행하면 신규 제약을 차이로 보고하므로 검증용 후보 checkout을
운영 머지 전에 준비해야 한다.

아래 명령은 **해당 서버/환경에서 검토 후 실행할 절차**이며 문서 추가가 적용 완료를 뜻하지 않는다.

```bash
# 검토한 파일의 SHA256을 기록하고 동일 파일을 환경별로 사용한다.
sha256sum db/changes/weekly_operator_diaries.sql
uv run python -m db.apply db/changes/weekly_operator_diaries.sql --env dev
uv run python -m db.apply db/changes/weekly_operator_diaries.sql --env dev --commit --expected-sha256 <검토한-SHA256>
uv run python -m db.verify --env dev

# 운영 반영은 나중의 운영 머지 준비 단계에서만 실행한다.
uv run python -m db.apply db/changes/weekly_operator_diaries.sql --env prod
uv run python -m db.apply db/changes/weekly_operator_diaries.sql --env prod --commit --allow-prod --expected-sha256 <검토한-SHA256>
uv run python -m db.verify --env prod
```

전환 설정은 키 없음/JSON null일 때 날짜별 방식이다. 잘못된 날짜·비월요일·설정 조회 실패는
일기 생성을 중단하며 날짜별 방식으로 되돌리지 않는다. 워커는 한 틱에 한 번 읽는다.
`weekly_unavailable`은 활성 원고 0편, `weekly_exhausted`는 활성 원고를 모두 지급받은 상태다.
둘 다 빈 diary 없이 no_entry로 확정하며, 이후 원고 등록으로 그 날짜를 소급 생성하지 않는다.

### 개발용 수동 생성

주간 모드에서는 종료된 `target_date`와 `force=false`를 명시한다. 기존 요청 기본값은 유지하므로
날짜/force 생략 요청은 주간 모드에서 거부된다. `DIARY_FORCE_NOT_ALLOWED`(409)는 지급 이력
보호, `DIARY_POLICY_UNAVAILABLE`(503)는 설정 조회/검증 실패다. 현재/미래 활동일은 422다.
주간 일기 릴리스 당시 아침 푸시는 제외했다. 후속 구현·활성화 절차는 아래 아침 일기 푸시 절을 따른다.


## 아침 일기 푸시 — 2026-09-09 구현

### 발송 기준

기존 15분 틱이 사용자 현지 09:00~09:59에 처리한다. 지나간 아침 알림을 뒤늦게 보충하지 않는다.
`MORNING_PUSH_ENABLED` 기본은 `false`이며 이번 변경만으로 실제 발송을 켜지 않는다.

발송 순간의 조회 결과가 다음 조건을 모두 만족해야 한다.

- 사용자 알림 설정 `morning_diary`가 켜짐(행 없음은 기존 기본 on)
- 해당 사용자의 전날 활동일 일기 중 `shared_day` 또는 `capi_day`
- 현지 오늘 공개됐고 `published_at <= now`, `record_status=published`, 삭제되지 않음
- `first_read_at IS NULL`, 즉 조회 시점에 아직 읽지 않음
- 유효한 비어 있지 않은 기기 토큰 존재. 같은 토큰은 중복 제거

환영 일기·과거 미독 일기·미래 공개·삭제된 일기는 보내지 않는다. 개인 일기가 없고 주간 원고도 소진되어
미발행된 날에는 푸시도 없다. 조회 장애는 일기가 있다고 추측해 발송하지 않고 기존 워커 오류 경로로 처리한다.
대상 판정과 실제 외부 전송 사이에 사용자가 읽는 극히 짧은 경합은 있을 수 있다. FCM과 DB를 하나의 트랜잭션으로 묶지는 않는다.

### 중복·실패·기한

기존 `user_daily_stats.morning_notified_at`을 사용자×활동일 단위로 원자 선점한다. 날짜는 워커가 전달한
동일 `now`로 계산한다. 이 컬럼은 기기 수신 완료가 아니라 전송 시도 선점 기록이다.

일기 없음·이미 읽음·토큰 없음·알림 off·FCM 인증 준비 실패는 선점하지 않아 같은 09시 창에서 다시 평가할 수 있다.
선점은 커밋 후 네트워크를 호출하며 전송 중에는 DB 연결을 잡고 있지 않는다.
FCM 요청을 시작한 뒤에는 성공/부분 성공/응답 유실을 구분해 정확히 한 번의 기기 수신을 보장할 수 없으므로
선점을 해제하지 않는다. 기기별 네트워크 오류는 로그를 남기고 다른 기기는 계속 시도한다.
FCM 성공 건수는 서비스가 요청을 수락한 수이며 사용자가 실제로 봤다는 뜻은 아니다.

Android TTL과 APNs expiration을 현지 오전 10시까지로 설정한다. 서버도 기한이 지난 메시지는 전송하지 않는다.
이는 푸시 서비스의 대기 기한이며 이미 표시된 알림을 알림함에서 삭제하는 기능은 아니다.
[FCM 메시지 수명 공식 문서](https://firebase.google.com/docs/cloud-messaging/customize-messages/setting-message-lifespan)를 따른다.

### 문구·클라이언트 계약

| 언어 | 제목 | 본문 |
| --- | --- | --- |
| ko | 캐피 | 캐피의 새 일기가 도착했어 |
| en | Cappy | Cappy’s new diary is ready to read |
| ja | キャピー | キャピーの新しい日記が届いたよ |

FCM `message.data`는 `{"link":"diary","diary_id":"<발행된 일기 UUID>"}`다. 본문에 개인 일기 내용이나 대화 내용을 넣지 않는다.

구버전은 기존 `link=diary`로 목록을 연다. 새 Flutter 클라이언트는 ID가 유효하면 기존 일기 상세를 열고 뒤로 가면
목록으로 돌아온다. ID가 없거나 잘못됐으면 목록을 연다. 로그인/시작 중에는 기존 대기 링크 처리로 ID를 보존하며,
세션 종료 시 대기 링크를 버린다. 다른 계정의 일기나 삭제된 일기는 기존 서버 소유권 확인 및 상세 오류 화면으로 처리한다.
실제 기기에서 로그인 전 탭, Android 전경/백그라운드/종료, iOS 종료 상태 탭을 출시 전에 검증한다.

### 활성화와 운영 순서

1. backend의 대상 판정·payload와 Flutter 상세 이동 변경을 검수한다. 아침 푸시 자체의 DB 마이그레이션은 없다.
2. `moly-infra/deploy.sh`의 옵션 전달 변경을 반영한다. 환경별 SSM `morning-push-enabled`는 `true`/`false`만 허용하며 미설정은 false다.
3. 개발용 설정과 기존 개발 API로 테스트 전용 계정·기기를 검증한다. 자동 테스트에서는 FCM을 대체하고 실제 사용자에게 발송하지 않는다.
4. 신기능 전체 운영 배포에 필요한 DB 변경을 아래 목록과 실제 운영 스키마로 다시 대조하고 먼저 적용한다.
5. 호환 서버를 아침 푸시 off로 배포하고, 운영 주소·Firebase 환경·서명·버전/빌드를 확인한 클라이언트를 출시한다.
6. 구버전 목록 이동과 새 버전 상세 이동을 확인한 뒤 운영 `morning-push-enabled=true`를 명시하고 환경을 재배포한다.
7. 중단 시 해당 환경 값을 false로 바꾸고 재배포한다. 이미 수락된 푸시는 회수할 수 없으며 DB 발송 마커를 지워 재발송하지 않는다.

SSM 변경·배포·실제 FCM 전송은 이번 구현 중 실행하지 않았다. 외부 계정 접근은 프로젝트용으로 확인된 경로만 사용하며
로컬 기본 AWS 개인 계정은 사용하지 않는다.

### 신기능 전체 출시 전 환경 대조

2026-09-09 공개 `/health`와 원격 코드 및 **읽기 전용 DB 메타데이터**를 확인했다. 아래는 그 시점의 기록이며
실제 배포 직전에 다시 확인한다. 클라이언트 저장소 버전은 스토어에 실제 배포된 빌드의 증거가 아니다.

| 항목 | 개발 | 운영 | 필요한 작업 |
| --- | --- | --- | --- |
| API 배포 SHA | `c4e0e6ab` | `dfc0e0f8` | 최신 후보 확정 후 운영 반영 |
| `mood_entries` | 존재 | 존재 | 테이블 신규 생성 불필요, 계약 일치 재확인 |
| 주간 일기 3컬럼 | 존재 | 없음 | `db/changes/weekly_operator_diaries.sql` 검토·적용 |
| `user_topic_states`, `chat_topic_entries` | 존재 | 없음 | `db/changes/banner_topics.sql` 검토·적용 |
| `morning_notified_at` | 존재 | 존재 | 기존 컬럼 활용, 신규 DDL 없음 |
| `diary_weekly_start_date` | `2026-09-07` | 미설정 | 운영 시작 주와 원고 준비 후 명시적 전환 |
| Flutter 신기능 기준 | `origin/main b913da3`, 소스 `1.1.6+12` | 스토어 실배포 미확인 | 출시 커밋·실제 빌드 번호·운영 환경 설정 확정 |

운영 `main=2610f1b3`과 실제 API SHA가 다른 것은 이후 임시 일기 읽음 처리 워크플로 정리 때문이었다.
개발 추가 변경은 주간 운영자 일기, 운세 문구/선택, 대화 주제, 배너/음악 추천이다. 기분 기록 API·테이블을
운영에 아직 없는 기능으로 잘못 간주하여 다시 생성하지 않는다.

두 준비 SQL은 기존 dev canonical schema에서 가져온 운영 전환 자료다. 앱 배포 시 자동 실행하지 않는다.
`db.apply`의 기본 dry-run과 명시적 운영 승인·hash 검증 절차를 사용하며, 운영 적용은 별도 작업이다.
SQL만 실행하고 주간 전환 설정을 빠뜨리면 기존 날짜별 일기 정책이 유지된다. 원고를 등록하지 않은 채
주간 정책을 켜면 미발행될 수 있으므로 운영 원고와 전환일을 함께 준비한다.

대화 주제 SQL도 운영 main 머지 전에 적용한다. 적용 직후 릴리스 후보의 schema contract로 컬럼·제약·인덱스·
RLS/권한을 확인한다. `IF NOT EXISTS`는 기존 테이블의 구조를 보정하지 않으므로 일부만 존재하면 먼저 차이를 검토한다.
적용 중 실패하면 도구의 트랜잭션이 rollback한다. 적용 후 기능 장애는 해당 기능을 중지하거나 호환 수정 버전으로
복구하며 이미 사용된 주제 테이블을 삭제하지 않는다. 주간 일기 전환 이후의 바이너리 회귀 제한도 함께 따른다.

### 이번 변경의 검증

- 서버 전체 단위 회귀 `uv run pytest -q --ignore=tests/integration`: 2,199개 통과(기존 Starlette 경고 1건). Ruff 통과
- `MOLY_NOTIFICATION_TEST_ENV=dev uv run pytest -q tests/integration/test_morning_diary_push.py`: 15개 통과. 기존 개발 DB에서 발행 종류·미독·날짜·삭제·설정·동시 선점·실패 후 재발송 방지 확인
- 개발 DB 테스트는 테스트 전용 사용자만 생성하고 종료 후 auth/일기/통계 잔여 없음 확인. 실제 FCM은 대체
- Flutter 관련 테스트 123개 및 전체 테스트 3,928개 통과. 목적지 파싱, 종료/백그라운드·전경 payload, 로그인 대기, 상세/목록 복귀 검증. 포맷·analyze 통과
- 인프라 배포 스크립트 문법 및 옵션 기본값/허용값 검증 통과. 실제 배포 스크립트는 실행하지 않음
- 실제 FCM/APNs 기기 도착·스토어 업로드·운영 배포는 후속 검증이며 자동 테스트 성공으로 대신하지 않음
