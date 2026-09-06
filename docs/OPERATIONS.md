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
