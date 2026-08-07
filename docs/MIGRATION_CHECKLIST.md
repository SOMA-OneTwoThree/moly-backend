# 운영 DB 마이그레이션 실행 목록

운영 서버(voice.moly.asia)의 데이터베이스를 새 구조로 옮기는 작업을 순서대로 적은 문서다.
위에서부터 하나씩 하면 된다. 배경 설명은 `docs/CUTOVER.md`에 있고, 이 문서는 **실행 목록**이다.

## 진행 상태 (2026-08-08 기준)

| 단계 | 상태 |
|---|---|
| 0단계 사전 준비물 | ✅ **완료** — 세 가지 다 만들고 시험까지 마침 |
| 1단계 마이그레이션 26개 | ✅ **완료** — 운영 표 26 → **52개**(dev와 동일) |
| 1.5단계 기억 백필 | ✅ **완료** — 574명 · 기억 16,035건 · 실패 0 · 전원 `shadow` |
| 1.6단계 shadow 계측 제거 | ✅ **완료** — PR #185 · 운영 대기 잡 19,078건 취소(30,976 → 11,898) |
| 1.7단계 잡 처리기 설정 | ✅ **완료** — `moly-infra` #19 머지(`51a407a`) · dev 배포로 확인 |
| 2단계 코드 배포 | ⏳ **대기** — 사용자 적은 새벽 권장 |
| 3단계 배포 후 | 예정 — 아래 순서대로 |

**1단계에는 삭제가 하나도 없다. 지금 멈추거나 되돌려도 잃을 데이터가 없다.**

**2026-08-07 Supabase Pro 전환됨.** Database Size 한도가 500 MB → **8 GB**가 되어 용량
제약이 사라졌다(현재 300 MB = 3.7%). 그래서 **기억 백필을 배포 전에 한다**(1.5단계).
배포 직후 값 하나로 켜면 되므로 기억 공백이 없다.

- 기준 시점: 2026-08-07. 아래 숫자는 전부 운영 DB에서 실제로 조회한 값이다.
- ⚠️ **아래 숫자는 참고용이다. 매일 늘어나므로 실행 당일에 다시 센다.** 중단 기준은 절대값이
  아니라 **두 값이 같은지**로 판단한다(아래 "시작 전 확인" 참고).
- 2026-08-07 기준 — 운영 사용자 622명 · 대화 메시지 `normal` 35,378 + `greeting` 1,201 ·
  일기 8,573건(그중 웰컴 612, 본문 없는 `none` 4,078, 발행됨 4,495) · 구 기억 벡터 9,545건.
- 적용 대상 파일은 **26개**다(`db/migrations/20260804*`~`20260806*` 27개 중
  `20260804_zz_memory_contract.sql` 제외 — dev에도 적용된 적이 없다).
- dev의 적용 기록에 `20260805_push_personalization.sql`이 남아 있지만 **신경 쓰지 않아도 된다.**
  그 기능은 되돌려졌고(`2a58f56` Revert), 만들었던 표도 dev·운영 양쪽에 없다. 기록만 남은 것이다.
- `20260807_drop_daily_digest_schedule.sql`·`20260807_backfill_last_active.sql`(푸시 관련)은
  **이 목록과 무관하게 별도 적용한다** — 둘 다 구코드에 무해(안 읽는 컬럼 백필 + 빈 테이블
  제약 조정)라 컷오버 순서에 넣지 않는다. 각 파일 머리말 참고.

## 전체 그림

| 단계 | 하는 일 | 서비스 영향 | 상태 |
|---|---|---|---|
| 0단계 | 사전 준비물 만들기(코드·SQL) | 없음 | ✅ |
| 1단계 | 마이그레이션 26개 적용(두 줄만 빼고) | 11번 파일이 도는 **몇 초 동안 요청 대기** | ✅ |
| 1.5단계 | 기억 백필(`shadow`로 쌓아만 둔다) | **없음** | ✅ |
| 1.6단계 | shadow 계측 제거 + 쌓인 잡 취소 | **없음** | ✅ |
| 1.7단계 | 잡 처리기 설정(`moly-infra` #19 머지) | **없음** | ✅ |
| 2단계 | `main` 머지 → 운영 배포 | 배포 시간만큼 | ⏳ |
| **3-1** | 대화 정상 확인 | 없음 | |
| **3-2** | **필수 3개** — 제약 삭제·트리거 제거·빈 구간 좌표 | 거의 없음 | |
| **3-3** | `v2` 승격 — 기억을 응답에 쓰기 시작 | 없음 | |
| 3-4 | ~~기능 켜기~~ — **2단계에서 같이 켜진다. 할 일 없음** | (2단계에 포함) | |
| 3-5 | 선택 — 웰컴 1건·빈 행 정리·회상 재구축·구 벡터 삭제 | 없음 | |

**되돌리기는 3-1까지만 된다.** 3-2를 하면 구 코드가 일기를 못 넣는다. 3-3은 되돌릴 수 있다
(`mode`를 `shadow`로 내리면 기억만 꺼진다).

**1단계를 다 끝내고 확인한 뒤에 2단계로 간다.** 1단계는 구 코드가 도는 중에 해도 안전하도록
설계했다.

**GitHub Actions가 필요한 단계는 2단계뿐이다.** 0·1·1.5·3단계는 사람이 자기 컴퓨터에서
`db/apply.py`와 스크립트로 돌린다. 그래서 Actions가 멈춰 있어도 1단계와 1.5단계를 먼저 해 두고,
Actions가 돌아온 뒤에 2단계를 해도 된다. 원래 순서가 "마이그레이션 먼저, 배포 나중"이라
어긋나지 않는다.

---

## 0단계 — 사전 준비물 ✅ 완료

이 셋이 없으면 1단계를 **시작조차 못 한다.**

### 0-1. `schema_migrations` 표 만들기

**파일은 만들어져 있다** — `db/migrations/20260807_schema_migrations_bootstrap.sql`.
표를 만들고 `20260729`까지 이미 적용된 18개 파일을 기록한다.

- [ ] 미리보기로 확인한다.
      `PYTHONPATH=. uv run python db/apply.py db/migrations/20260807_schema_migrations_bootstrap.sql --env prod`
- [ ] 반영한다. **26개보다 먼저 이것부터 한다.**
      `PYTHONPATH=. uv run python db/apply.py db/migrations/20260807_schema_migrations_bootstrap.sql --env prod --allow-prod --commit`

**왜 필요한가.** `db/apply.py`는 파일을 실행한 뒤 **매번** `public.schema_migrations`를 조회하고
기록을 남긴다(33행·42행). 그런데 그 표를 만드는 문장은 `20260804_zzz_conversational_recall.sql`
안에 있고, 이 파일은 적용 목록에서 11번째다. **그 앞의 10개가 전부 실패한다.** 미리보기(dry-run)도
같은 경로를 지나므로 미리 볼 수조차 없다. 그래서 표를 먼저 만드는 파일을 따로 두었다.

### 0-2. `db/apply.py`에 운영 적용 통로 추가

**수정은 되어 있다** — `db/apply.py`에 `--allow-prod`를 추가했다. `--commit`과 함께 줄 때만
dev 대상 확인을 건너뛴다. 미리보기는 예전처럼 그냥 된다.

- [ ] 이 변경이 담긴 브랜치를 **받아 둔 상태에서** 마이그레이션을 실행한다.

**배포할 필요가 없다.** `db/apply.py`는 서버에서 도는 코드가 아니라 **사람이 자기 컴퓨터에서
돌리는 도구**다. 앱 코드를 하나도 가져다 쓰지 않고(`asyncpg`와 `db/envfile`뿐, `envfile`은
표준 라이브러리만), 워크플로나 Dockerfile에서 부르는 곳도 없다. 그래서 어느 브랜치를 받아
놨든 상관없고 `main` 머지도, GitHub Actions도 필요 없다.

**왜 필요한가.** `db/apply.py` 23~24행은 `--commit`을 줬을 때만 `assert_dev_target`을 부른다.
그 함수(`db/envfile.py` 92~98행)가 운영이면 중단시킨다. 즉 **미리보기는 지금도 되고, 실제
반영만 막혀 있다.**

### 0-3. 일기 호환 트리거 작성 — 11번 파일 사본 안에 넣는다

**SQL은 만들어져 있다** — `db/cutover/diaries_legacy_compat_trigger.sql`.

- [ ] 그 파일 내용을 11번 파일 사본의 아래 위치에 **끼워 넣는다.** 따로 적용하면 안 된다.

dev에서 실제로 시험해 확인했다(트랜잭션 안에서 넣고 되돌림).

| 구 코드가 넣은 것 | `kind` | `activity_date` | `record_status` |
|---|---|---|---|
| `source='llm'` | `shared_day` | 채워짐 | `published` |
| `source='preset'` | `capi_day` | 채워짐 | `published` |
| `source='none'` | 비움 | 비움 | `processed` |
| `source='welcome'` | `welcome` | 비움 | `published` |

`author`는 컬럼 기본값 `capi`로 자동으로 들어갔고, 새 코드가 값을 직접 준 경우에는
트리거가 **덮어쓰지 않았다.**

**넣는 위치.** `20260804_zzz_conversational_recall.sql` 사본에서
`ALTER TABLE public.diaries ADD COLUMN ...` 블록(89~102행)이 **끝난 뒤**,
`ALTER COLUMN display_date SET NOT NULL`(117행)이 **오기 전**이다.

**왜 이 위치인가.** 트리거 함수는 `NEW.kind`·`NEW.display_date` 같은 컬럼을 읽는다.
PostgreSQL은 트리거를 만들 때 그 컬럼이 있는지 **검사하지 않는다.** 그래서 11번보다 먼저 깔면
트리거 생성은 성공하고, 그 순간부터 **모든 일기 삽입이 실패한다.** 개발 DB에서 실제로 재현해
확인했다(오류: 없는 컬럼 참조).

반대로 11번을 다 적용한 뒤에 깔아도 안 된다. 그 사이에 `display_date NOT NULL`과 CHECK 3종이
트리거 없이 존재하는 구간이 생겨 구 코드의 일기 생성이 실패한다.

`db/apply.py`는 파일 하나를 하나의 트랜잭션으로 실행한다(20~22행·27~29행). 그래서 **파일 안에
끼워 넣어야만** 위험 구간이 0이 된다.

**왜 필요한가.** 1단계에서 `diaries`에 새 컬럼과 제약이 붙는데, 구 코드는 그 컬럼들을 모른다.
구 코드의 일기 모델에는 `kind`·`record_status`·`display_date`·`activity_date`가 **아예 없다.**
트리거가 대신 채워 줘야 구 코드의 일기 생성이 계속 돈다.

트리거가 채워야 할 것:

| 컬럼 | 채울 값 |
|---|---|
| `display_date` | `diary_date` 값 그대로 (비어 있을 때만) |
| `kind` | `source`가 `welcome`→`welcome`, `llm`→`shared_day`, `preset`→`capi_day`, `none`→비움 |
| `activity_date` | `kind`가 `shared_day`·`capi_day`면 `diary_date` 값 |
| `record_status` | `source`가 `none`이면 `processed`, 아니면 기본값 `published` 그대로 |

`author`는 컬럼 기본값이 `capi`라 따로 안 채워도 된다.

**주의 두 가지.**

1. **`RETURN NULL`을 쓰면 안 된다.** 새 코드의 일기 저장은 `RETURNING id`를 쓰기 때문에,
   트리거가 행을 버리면 오류가 난다. 값만 채우고 `RETURN NEW`로 끝낸다.
2. **네 컬럼 모두 비어 있을 때만 채운다.** 배포 뒤 3단계까지는 새 코드가 넣는 값에도 이
   트리거가 돈다. 지금은 새 코드의 값과 매핑이 같아서 문제가 없지만, 조건을 걸어 두면
   나중에 매핑이 달라져도 안전하다.
3. 이 트리거는 3단계에서 **반드시 지운다.**

---

## 1단계 — 마이그레이션 26개 ✅ 완료 (2026-08-07)

### 실행 방법

```bash
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod            # 미리보기
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod --commit   # 반영
```

**한 번에 한 개씩, 미리보기를 먼저 본다.** 실패하면 그 자리에서 멈추고 원인을 본다.

**언제 하는가.** 11번 파일은 한 번의 트랜잭션 안에서 `diaries`·`messages`·`profiles`·
`chat_contexts`·`idempotency_keys` 등 여러 표에 `ALTER TABLE`을 실행한다. 그동안 그 표들에
대한 조회까지 대기한다. 데이터가 크지 않아 몇 초로 끝나지만 0은 아니다. **사용자가 가장 적은
새벽에 한다.** 다른 파일은 대기 시간이 사실상 없다.

### 시작 전 확인

- [ ] 0단계 세 가지가 전부 끝났다.
- [ ] 호환 트리거를 **11번 파일 사본 안에** 끼워 넣었다(0-3 참고). 별도로 미리 적용하지 않았다.
- [ ] 읽기 전용 사전 점검을 돌려 통과했다.
      `PYTHONPATH=. uv run python scripts/preflight_cutover.py --env prod`
- [ ] 삭제될 일기 행 수를 기록했다. 아래 두 값이 **같아야** 한다. 다르면 **중단한다.**

```sql
SELECT count(*) FROM diaries WHERE source='none';                                  -- 두 값이
SELECT count(*) FROM diaries WHERE source='none' AND coalesce(length(content),0)=0; -- 같아야 한다
```

### 적용 순서

**파일명 오름차순이 아니다.** 아래 순서는 파일끼리의 의존 관계를 기계로 검사해서 정한 것이다.
바뀐 곳은 3군데이고 표에 표시했다.

| # | 파일 | 비고 |
|---|---|---|
| 1 | `20260804_async_jobs.sql` | 새 표 |
| 2 | `20260804_chat_last_active.sql` | `chat_contexts` 컬럼 추가 |
| 3 | `20260804_conversation_checkpoints.sql` | **순서 바꿈** — 아래 4번이 이 표를 쓴다 |
| 4 | `20260804_checkpoint_generation.sql` | **순서 바꿈** |
| 5 | `20260804_diary_search.sql` | `pg_trgm` 확장을 직접 설치한다 |
| 6 | `20260804_job_replay_lineage.sql` | |
| 7 | `20260804_memory_normalization.sql` | **순서 바꿈** — 8번이 `memory_facts`를, 9번이 `memory_mode`를 쓴다 |
| 8 | `20260804_memory_embeddings.sql` | **순서 바꿈** |
| 9 | `20260804_memory_cutover_guard.sql` | **순서 바꿈 — 반드시 7번 뒤. 아래 설명을 읽을 것** |
| 10 | `20260804_relationship_profiles.sql` | |
| 11 | `20260804_zzz_conversational_recall.sql` | **⚠ 118행·141행 두 줄을 빼고, 호환 트리거를 끼워 넣은 사본으로 적용한다. 아래 참고** |
| 12 | `20260804_zzzz_conversational_recall_backfill.sql` | |
| 13 | `20260804_zzzzz_conversational_recall_hardening.sql` | **⚠ 반드시 18번보다 먼저** |
| 14 | `20260805_ai_usage_ledger.sql` | |
| 15 | `20260805_mem0_v2_collection.sql` | |
| 16 | `20260805_memory_v2_tables.sql` | |
| 17 | `20260805_privacy_epoch.sql` | **순서 바꿈** — 18번이 쓰는 `epoch` 컬럼을 만든다 |
| 18 | `20260805_privacy_active_backfill.sql` | **순서 바꿈**. 머리말의 "배포 뒤" 경고는 무시한다 |
| 19 | `20260805_relationship_render.sql` | |
| 20 | `20260805_shadow_prompt_traces.sql` | |
| 21 | `20260805_user_schedules.sql` | |
| 22 | `20260806_backfill_turn_seq.sql` | `kind='normal'`인 `messages`에만 턴 좌표를 매긴다(`greeting`은 대상이 아니다) |
| 23 | `20260806_drop_legacy_memory.sql` | 표 13개를 지운다. **앞 단계가 만든 것들이라 반드시 실행해야 한다** |
| 24 | `20260806_drop_legacy_tombstones.sql` | `legacy_recall_tombstones`를 지운다(16번이 만든 것) |
| 25 | `20260806_normalize_profile_language.sql` | 값이 바뀌는 사람 **0명** |
| 26 | `20260806_rls_gap.sql` | |

### 9번을 7번보다 먼저 하면 대화가 전부 멈춘다

`20260804_memory_cutover_guard.sql`은 `chat_contexts`에 `BEFORE INSERT OR UPDATE` 트리거를
설치한다. 그 트리거 함수 `guard_normalized_memory_snapshot`은 `NEW.memory_mode`와
`OLD.memory_mode`를 읽는다(파일 23~33행). **그 컬럼은 `20260804_memory_normalization.sql`
181행이 만든다.**

파일명 오름차순으로는 `memory_cutover_guard`가 `memory_normalization`보다 먼저다. 그대로 돌리면
컬럼이 없는 상태에서 트리거가 설치되고, **그 사이에 `chat_contexts`에 쓰기가 일어나면 오류가
난다.** 구 코드는 다음 두 곳에서 이 표에 쓴다.

| 위치 | 언제 |
|---|---|
| `app/services/chat.py` 255·259행 | 대화 중 기억 스냅샷 저장 |
| `app/services/diary_generation.py` 291행 | 일기 생성 워커 |

대화 경로가 포함되므로 **사용자가 말을 걸기만 해도 터진다.** 두 파일 사이 간격이 몇 초라도
위험하다. `memory_cutover_guard.sql` 자체 머리말에도 "memory_normalization이 memory_mode
컬럼을 만든 다음에 적용한다"고 적혀 있다.

> 🚨 **`20260804_zzz_conversational_recall.sql` 원본을 운영에 적용하면 안 된다.**
> 미뤄둔 두 줄이 실행되어 본문 없는 일기 수천 건이 지워지고, 되돌릴 수 있는 상태가 깨진다.
> `db/apply.py`는 기록표를 보고 건너뛰지 않고 **항상 실행한다.** 기록이 있어도 막아 주지 않는다.
> 운영에는 `db/cutover/prepared/11_zzz_conversational_recall_PROD.sql`만 적용한다.
>
> 2026-08-07 적용 시 기록표에 사본 이름으로 남아, 파일 목록과 비교하면 원본이 미적용처럼
> 보였다. 그래서 원본 이름으로도 기록을 넣어 두었다(두 줄이 빠진 채 적용됐다는 뜻이다).

### 11번 파일에서 빼야 하는 두 줄

```sql
ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq;   -- 118행
```

```sql
DELETE FROM public.diaries WHERE source='none';                              -- 141행
```

- [ ] 이 두 줄을 뺀 사본을 만들어 적용한다(호환 트리거는 그 사본 안에 넣는다. 0-3 참고).

**141행을 왜 빼는가.** 이 줄이 본문 없는 일기를 전부 지운다(2026-08-07 기준 4,078행). 되돌릴 수 없는 유일한 작업이다. 3단계로
미루면 **1단계에 삭제가 하나도 없어져서, 배포를 되돌릴 때 잃을 것이 0이 된다.** 미루는 비용은
없다. 바로 위 137행의 `diary_generation_results` 옮기기는 **그대로 둔다** — 새 코드가 보는
것은 그쪽이다.

지우지 않고 남겨도 제약에 걸리지 않는다. 104행 갱신이 이 행들의 `record_status`를
`processed`로, `display_date`를 채워 주기 때문에 CHECK를 통과하고, `processed`는 사용자에게
보이지 않는다.

**118행을 왜 빼는가.** 구 코드의 웰컴 일기 저장이
`on_conflict_do_nothing(index_elements=["user_id", "diary_date"])`를 쓴다. 이 제약을 지우면
데이터베이스가 맞는 인덱스를 찾지 못해 오류를 낸다. **이 판정은 실행 계획을 짤 때 일어나서
트리거가 돌기 전이다.** 그래서 호환 트리거로도 막을 수 없다. 3단계에서 지운다.

### 13번과 18번의 순서를 지켜야 하는 이유

13번(`hardening`) 28~37행은 `privacy_subject_barriers`에 행이 있는 사용자의 파생 데이터와
웰컴 일기를 지운다. 18번(`privacy_active_backfill`)이 그 표에 **622명분 행을 넣는다.**
18번을 먼저 돌리면 그 뒤에 만들어진 웰컴 일기가 지워질 수 있다. **13번을 먼저 한다.**

### 23·24번을 건너뛰면 안 된다

운영에는 지금 이 14개 표가 **없다.** 그래서 "지울 게 없으니 건너뛰어도 된다"고 생각하기 쉽지만
틀렸다. **앞 단계가 이 표들을 만든다.**

| 만드는 파일 | 표 |
|---|---|
| 7번 `memory_normalization` | `memory_facts`·`memory_evidence`·`memory_insights`·`memory_insight_sources`·`memory_forget_markers`·`memory_source_turns`·`memory_source_turn_messages`·`memory_source_closures` |
| 10번 `relationship_profiles` | `relationship_profiles`·`relationship_profile_sources` |
| 11번 `zzz` | `memory_episodic_messages`·`memory_recall_suppressions`·`memory_suppression_operations` |
| 16번 `memory_v2_tables` | `legacy_recall_tombstones` |

건너뛰면 운영에 쓰지 않는 표 14개가 남아 개발과 구조가 어긋난다. **반드시 실행한다.**

### 기존 데이터를 지우거나 바꾸는 곳 전부

빠진 것이 없도록 전수로 적는다. 실제로 사라지는 것은 **첫 줄 하나뿐**이다.

**1단계에는 지워지는 것이 하나도 없다.** 유일한 삭제(11번 141행)를 3단계로 미뤘기 때문이다.

| 파일 | 무엇을 | 운영에서 몇 행 |
|---|---|---|
| 11번 74~76행 | 고아 `routine_completions` 삭제 | 0행 |
| 11번 237행 | `memory_evidence` 정리 | 0행 (7번이 방금 만든 빈 표) |
| 13번 28~37행 | 장벽 있는 사용자의 파생 데이터·웰컴 일기 삭제 | 0행 (이 시점에 장벽 행이 없다) |
| 11번 104행 | `diaries`의 새 컬럼 채우기 | 일기 전량 — 값 추가일 뿐 기존 값은 그대로 |
| 22번 | `messages` 턴 좌표 매기기 | `normal` 전량 — 빈 컬럼을 채우는 것 |
| 25번 | `profiles.language` 정규화 | **값이 바뀌는 사람 0명** |

### 1단계 확인 — 2026-08-07 실제 결과

| 확인 항목 | 결과 |
|---|---|
| public 표 | 26 → **52개** (dev와 동일) |
| 회상 문서 | **4,495 = 발행 일기 4,495** (13·18 순서 정상) |
| 좌표 없는 메시지 | **0** |
| 삭제 장벽 | 622 / 622명 |
| 언어 값 | ko 446 · ja 130 · en 46 |
| `source='none'` | 4,080행 **유지**(3단계로 미룸) |
| `diaries_user_date_uq` | **유지**(3단계로 미룸) |
| 호환 트리거 | 설치됨 · 운영에서 정상 동작 확인 |
| 구 코드 일기·대화·메시지 저장 | 전부 정상 |

**도중에 교착이 한 번 났다.** 이쪽이 `profiles`를 쥐고 `idempotency_keys`를 기다리는 동안
대화 요청이 반대로 기다렸다. 파일 하나가 트랜잭션 하나라 통째로 되돌아가 피해가 없었고,
필요한 표 7개를 처음에 한꺼번에 잠그도록 고쳐 재시도는 2초에 끝났다. 그 선점 잠금은
`db/cutover/prepared/11_zzz_conversational_recall_PROD.sql`에 들어 있다.

### 확인 항목 (다음에 다시 할 때)

- [ ] 26개 전부 `schema_migrations`에 기록됐다.
- [ ] 사전 점검을 다시 돌려 통과한다.
- [ ] 구 코드가 도는 상태에서 **일기가 정상 생성되는지** 확인한다(호환 트리거 동작 확인).
- [ ] 대화가 정상인지 확인한다.
- [ ] 웰컴 일기 수를 기록해 둔다. **1단계에서는 늘지 않는 것이 정상이다.**
      `SELECT count(*) FROM diaries WHERE source='welcome';` → **613**

      12번 백필은 웰컴 없는 사용자에게 웰컴을 만든다. 대상은 첫 사용자 발화가 있는 1명뿐인데,
      **그 1명은 1단계에서 안 생긴다.** 웰컴이 들어갈 날짜(관계 시작일)에 이미 개인일기가
      있고, 3단계로 미룬 `diaries_user_date_uq`가 살아 있어 `ON CONFLICT DO NOTHING`으로
      건너뛰기 때문이다. 2026-08-07 운영에서 실제로 그랬다(612 그대로).

      **피해는 없다.** 기존 일기는 그대로이고, 3단계에서 제약을 지운 뒤 채우면 된다.

      이 1명은 배포 전까지 웰컴 일기 제목 자리에 본문이 통째로 보인다. 12번은 제목을
      `title` 컬럼에 따로 넣는데 구 코드는 본문을 빈 줄로 갈라 제목을 뽑기 때문이다.
      배포하면 정상으로 보인다. 1명이고 곧 해소되므로 따로 손대지 않는다.

- [ ] 13번과 18번의 순서가 지켜졌는지 본다.
      `SELECT count(*) FROM diary_recall_documents;` → **발행된 일기 수와 비슷해야 한다**
      (`SELECT count(*) FROM diaries WHERE published_at IS NOT NULL`). **0이면 순서가 어긋난 것이다.**

      웰컴 일기 수로는 이 문제를 못 잡는다. 13번의 웰컴 삭제는
      `d.created_at >= b.created_at` 조건이라 기존 웰컴에는 안 걸린다. 실제 피해는 13번
      28~30행의 삭제 3개다. 이건 조건이 없어서 장벽 행이 있는 사용자 전원의 검색 문서를
      지우고, 뒤따르는 재구축은 전부 장벽 있는 사용자를 빼기 때문에 **0건이 된다.**

---

## 1.5단계 — 기억 백필 ✅ 완료 (574명 · 16,035건 · 실패 0)

마이그레이션은 기억 표를 만들 뿐 **내용을 하나도 만들지 않는다.** 이 단계를 안 하면
캐피는 과거 대화를 전혀 기억하지 못한다.

Pro 전환으로 용량 제약이 사라져 **배포 전에 미리 채운다**(약 275~300 MB, 8 GB 한도의 3.5%).
등록은 `shadow` 상태로 한다 — 기억을 쌓기만 하고 **응답에는 쓰지 않는다**. 구 코드는 이 표들을
아예 모르므로 운영에 영향이 없다.

기억 추출이 쓰는 표는 전부 새 표라 다른 기능에 영향이 없다. 확인 결과 쓰기 대상은 `memory_pipeline_states`·`mem0_memory_registry`·
`mem0_memory_sources`·`mem0_ingest_candidates`·`async_jobs`·`relationship_events`·
`diary_recall_documents`이고, 구 코드는 이 중 어느 것도 참조하지 않는다. `messages`와
`chat_contexts`는 읽기만 한다.

#### 기존 기억은 살리지 않는다

| 기존 것 | 운영 현재 | 처리 |
|---|---|---|
| `vecs.memories` (구 벡터) | 9,281건 | **버린다.** 새 코드가 읽지 않는다(참조 0건 확인) |
| `chat_contexts.memory_text` | 247명분 | **버린다.** 새 코드가 읽지 않는다 |

둘 다 지우지 않고 그대로 둔다. 되돌릴 때 필요하고, 새 코드가 안 읽을 뿐이라 무해하다. 정리는
전환이 확실히 안정된 뒤에 한다.

새 기억은 **과거 대화에서 다시 만든다.** 22번 파일이 매긴 턴 좌표가 그 재료다.

#### 절차

- [ ] 새 코드(`dev` 브랜치)를 운영 데이터베이스를 향해 **따로 실행할 자리**를 준비한다.
      운영 서버에 배포하는 것이 아니다. 스크립트를 로컬이나 별도 기기에서 `.env.prod`로 돌린다.
- [ ] 사용자를 기억 파이프라인에 등록한다. 먼저 미리보기로 확인한다.
      `PYTHONPATH=. uv run python scripts/enter_shadow_cohort.py --limit 1`
- [ ] 소수(1~5명)로 먼저 돌려 결과를 눈으로 본다. 기억 내용이 이상하면 지우고 다시 한다.
      **배포 전이라 사용자에게는 아무것도 보이지 않는다.** 얼마든지 고쳐 쓸 수 있다.
- [ ] 전체로 넓힌다. 과거 대화 약 17,687턴 기준 **비용 약 $14, 시간 약 7.4시간**이다.
- [ ] 진행 상황을 확인한다.
      `SELECT count(*) FROM mem0_memory_registry;`
      `SELECT count(*) FROM memory_pipeline_states;`

#### 주의

- **여기서 만든 기억은 배포 전까지 아무도 보지 못한다.** 잘못돼도 지우고 다시 만들면 된다.
  이것이 배포 전에 하는 가장 큰 이점이다.
- 이 작업이 도는 동안에도 사용자는 평소대로 대화한다. 그 사이 생기는 새 대화는 턴 좌표가
  없으므로 3단계의 "빈 구간 메시지" 처리로 이어서 넣는다.
- `.env.prod`를 다루므로 접속 정보가 화면이나 기록에 남지 않게 한다.

---

---

## 1.7단계 — 잡 처리기 설정 ✅ 완료

대화 한 턴이 끝나면 서버는 뒷일(기억 만들기, 일기 색인, 약속·관계 갱신)을 `async_jobs` 표에
적어만 두고 응답을 먼저 보낸다. 그 표를 읽어 실제로 처리하는 상주 프로세스가 잡 처리기
(`python -m worker.consumer`)다. **운영에는 이게 없었다.** 없으면 배포해도 새 기억이 안 생긴다.

`moly-infra` #19가 이걸 켠다. 기능 플래그 커밋과 같은 PR이다.

- [x] **`moly-infra` #19를 main으로 머지**(2026-08-08, `51a407a`). 이 머지만으로는 배포가 안 일어난다
- [x] `moly-backend` dev 배포로 새 `deploy.sh` 확인 — 로그에 `consumer 핸들러 등록 확인`(10종)과
      `async job consumer running`. dev·운영 모두 `/root/moly-infra`의 main을 pull한다
- [x] 쌓인 계측 전용 잡 19,078건 취소(아래)

### 운영 EC2 두 대에 다 띄우는데 중복이 안 나는 이유

15분 워커(`tick`)는 "지금 04시니까 대상 유저 전체에게 일기 써"를 **스스로 판단**한다. 두 대가
돌면 둘 다 같은 유저 목록을 훑어 일기가 두 번 생성된다. 그래서 `/etc/moly-worker-host` 마커로
한 대만 돌린다.

잡 처리기는 다르다. 표에서 **행을 집어온다.**

```sql
SELECT id FROM async_jobs WHERE queue=:queue AND state='ready' ...
FOR UPDATE SKIP LOCKED LIMIT :batch_size
```

`FOR UPDATE`가 그 행을 잠그고, `SKIP LOCKED`가 "남이 잠근 행은 기다리지 말고 건너뛰라"는 뜻이다.
#1이 1·2번을 집으면 #2는 3·4번을 집는다. 나눠 갖는 것이라 몇 대를 띄워도 중복이 0이다.

딱 하나 두 번 실행될 수 있는 경우가 있다. #1이 처리 중에 멈춰 처리 권한 시간이 지나면 회수기가
그 행을 다시 `ready`로 돌리고 #2가 집는다. 이때 LLM 호출은 두 번 나갈 수 있다. 하지만 **결과가
두 번 저장되지는 않는다** — 집어갈 때 발급한 1회용 번호(`lease_token`)가 맞을 때만 확정하므로
늦게 돌아온 #1은 0행 갱신으로 물러난다. 이건 한 대만 띄워도 재기동하면 똑같이 생기는 일이다.

### 구 이미지 안전장치

`moly-backend` main에는 **`worker/consumer.py`가 아예 없다.** 그래서 롤백으로 구 이미지가
배포되면 처리기 컨테이너가 즉시 죽고, 배포 게이트가 막혀 **롤백 자체가 실패한다.**
새 `deploy.sh`는 기동 전에 모듈이 있는지 보고, 없으면 건너뛰고 배포를 계속한다.

### 쌓여 있는 잡 30,976건

배포 후 처리기가 켜지면 이게 전부 돌기 시작한다. 기억 백필을 하면서 생긴 것들이다.

| 종류 | 건수 | 큐 | 외부 호출 | 값어치 |
|---|---:|---|---|---|
| `shadow_prompt_trace` | 17,690 | maintenance | 없음 | 계측 전용 |
| `diary_recall_embed` | 8,988 | content | 임베딩 | 필요 — 없으면 일기 유사도 검색이 안 됨 |
| `mem0_provider_delete` | 1,522 | maintenance | 벡터 삭제 | 필요 — v2 중복 정리 (구 `vecs.memories`는 안 건드림) |
| `shadow_checkpoint` | 1,388 | content | **LLM** | 계측 전용 |
| `relationship_project` | 694 | maintenance | 없음 | 필요 |
| `contract_compile` | 694 | content | **LLM** | 필요 |

- [x] **2026-08-08 취소 완료** — `shadow_prompt_trace` 17,690 + `shadow_checkpoint` 1,388 =
      **19,078건**을 `cancelled`로 내렸다. 대기 잡 30,976 → **11,898건**.
      취소한 이유는 비용이 아니라 결함이다 — 3-4절 위의 설명과 PR #185 참고.

실행한 문장(기록용):

```sql
UPDATE async_jobs SET state='cancelled', result_code='shadow_removed_at_cutover', finished_at=now()
WHERE state='ready' AND job_type IN ('shadow_prompt_trace','shadow_checkpoint');
```

**남은 대기 11,898건은 전부 필요한 것이다.** 배포 후 잡 처리기가 비운다.

---

## 2단계 — 코드 배포

- [ ] **`moly-infra` PR #19가 먼저 머지돼 있어야 한다.** 순서가 바뀌면 기능 플래그가 꺼진 채
      뜨고 잡 처리기도 안 떠서 재배포해야 한다.
- [ ] dev 브랜치를 `main`으로 머지한다. **머지는 사용자가 한다.**
- [ ] 배포가 끝날 때까지 기다린다.
- [ ] 헬스 점검 4종을 확인한다.
- [ ] 배포 로그에 `async job consumer running`이 찍혔는지 본다.
- [ ] 대화·일기 조회가 정상인지 본다.

---

## 3단계 — 배포 후

### 3-1. 대화 정상 확인 — 되돌릴 수 있는 마지막 구간

- [ ] 대화·일기 조회·가입·재화가 평소대로 도는지 본다.
- [ ] 이때 **기억은 아직 안 나온다**. 전원 `shadow`라 쌓기만 하고 응답에 안 쓴다(3-3에서 켠다).

**되돌리려면 여기서 해야 한다.** 코드를 이전 이미지로 되돌리면 끝난다. 미뤄둔 것들
(`diaries_user_date_uq`·빈 행·호환 트리거·구 벡터)이 그대로 남아 있어 구 코드가 바로 돈다.
새 코드도 `diary_date`를 채우므로 그 사이 만들어진 일기도 구 코드가 찾는다.

### 3-2. 필수 3개 — 이걸 하면 되돌리기가 끊긴다

**하나라도 안 하면 새 코드가 제대로 못 돈다.** 반대로 하고 나면 구 코드로 못 돌아간다.

#### 3-2-1. `diaries_user_date_uq` 삭제

```sql
ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq;
```

**안 하면 신규 가입자가 첫 개인일기를 못 받는다.** 새 코드는 웰컴 일기를 **첫 대화일 그대로**
놓는데(`diary.py` `_welcome_date`), 구 코드는 하루 앞에 놨다. 그래서 새 코드에서는 웰컴과
그날 개인일기의 `diary_date`가 같아져 이 제약에 걸린다. 일반 일기 저장은 `session.add()`라
`ON CONFLICT`가 없어 **오류가 그대로 터진다.**

2026-08-07 운영에서 재현 확인:
`웰컴(첫 대화일) 저장 성공 → 같은 날짜 개인일기 실패: duplicate key ... diaries_user_date_uq`

#### 3-2-2. 호환 트리거·함수 제거

```sql
DROP TRIGGER IF EXISTS diaries_legacy_compat_tg ON public.diaries;
DROP FUNCTION IF EXISTS public.diaries_legacy_compat();
```

구 코드 대신 새 컬럼을 채워 주던 장치다. 새 코드는 스스로 채우므로 필요 없다. 남겨두면 매번
헛돌고 나중에 매핑이 달라질 때 문제를 가린다. 3-2-1을 한 뒤라 어차피 구 코드로 못 돌아간다.

#### 3-2-3. 빈 구간 메시지에 턴 좌표 매기기

배포 전까지 구 코드가 만든 메시지는 `turn_seq`가 비어 있다. 기억 파이프라인이
`turn_seq IS NOT NULL`로 거르므로(`memory_pipeline.py` 181행) **그 기간 대화가 영영 기억에
안 들어간다.** 아래 상세를 읽고 실행한다.

### 3-3. `v2` 승격 — 기억을 응답에 쓰기 시작

- [ ] `PYTHONPATH=. uv run python scripts/verify_cutover_gate.py --env prod` 통과 확인
- [ ] `db/cutover/promote_memory_v2.sql`의 미리보기 SELECT로 대상 확인
- [ ] 같은 파일의 UPDATE 실행 → 기억이 프롬프트에 들어가기 시작

`serves_v2`는 `mode='v2'`일 때만 참이다(`memory_pipeline.py` 55~57행). 승격 경로가 코드에
없어서 SQL로 직접 바꾼다.

**되돌리기** — 코드를 되돌리지 않고 기억만 끌 수 있다. 같은 파일 맨 아래의
`mode='shadow'` 되돌리기를 쓴다. 쌓인 기억은 그대로 두고 응답에서만 뺀다.

### 3-4. 기능 켜기 — **할 일이 없다. 2단계에서 이미 켜진다**

`moly-infra` PR #19(2026-08-08 머지)가 아래 다섯 개를 `deploy.sh`의 공통 블록으로 옮겼다.
**운영 배포와 동시에 켜진다.** 배포 후에 따로 켜는 절차는 없다.

| 설정 | 배포 후 운영 | 비고 |
|---|---|---|
| `AGENT_ENABLED` (도구 호출) | **켜짐** | `AGENT_CANARY_PCT=100` |
| `CONTEXT_CHECKPOINT_ENABLED` (대화 요약) | **켜짐** | |
| `CURRENT_TURN_CONTEXT_ENABLED` | **켜짐** | |
| `CURRENT_CONTEXT_LAST_ACTIVE_ENABLED` | **켜짐** | |
| `MORNING_PUSH_ENABLED` (아침 푸시) | 꺼짐 | **켜지 않는다**(SOMA-338 제품 결정) |

전부 환경 변수라 **끄려면 재배포가 필요하다.** DB로 즉시 끄는 스위치가 아니다.

- [ ] 배포 후 응답 시간과 비용을 본다. 현재 턴 컨텍스트가 프롬프트 캐시 적중률(약 65%)에
      영향을 줄 수 있으므로 그 값을 함께 본다.
- [ ] 이상하면 `moly-infra`에서 해당 값을 되돌리고 재배포한다.

**dev에서 계속 켜둔 채 검증했다** — 2026-08-08 dev 배포본으로 대화 5건, 기억 회상·도구
호출·대화 요약 모두 정상 확인.


### 3-5. 선택 — 안정된 뒤 아무 때나

급하지 않다. 안 해도 새 코드는 정상으로 돈다.

- [ ] **못 만든 웰컴 1건 채우기** — 3-2-1 뒤라야 들어간다. 12번 파일의 웰컴 INSERT를 한 번 더
      돌린다. `SELECT count(*) FROM diaries WHERE kind='welcome';` 가 1 늘어야 한다.
- [ ] **처리 표시 재복사 → 빈 행 삭제** — 이 순서를 지킨다. 새 코드의 중복 검사는
      `activity_date`·`kind`로 보는데 빈 행은 그 둘이 비어 있어 검사에 안 걸린다. 즉 남아 있어도
      해가 없다.
- [ ] **회상 문서 재구축** — 빈 구간에 만들어진 일기를 검색에 넣는다.
      `20260804_zzzzz_conversational_recall_hardening.sql` 101~126행을 한 번 더 돌린다.
- [ ] **구 기억 벡터 삭제** — `DROP TABLE vecs.memories;` 158 MB. Pro 한도 8 GB 중이라
      **몇 주 두어도 된다.** 지우면 구 코드로 되돌려도 기억이 없다.

#### 처리 표시 재복사 (빈 행 삭제 전 필수)

```sql
INSERT INTO public.diary_generation_results(user_id,target_date,status,created_at)
SELECT user_id,diary_date,'no_entry',COALESCE(created_at,now())
FROM public.diaries WHERE source='none'
ON CONFLICT (user_id,target_date) DO NOTHING;

-- 두 값이 같은지 확인한 뒤 삭제한다. 다르면 중단한다.
SELECT count(*) FROM diaries WHERE source='none';
SELECT count(*) FROM diaries WHERE source='none' AND coalesce(length(content),0)=0;
DELETE FROM public.diaries WHERE source='none';
```

#### 3-2-3 상세 — 빈 구간 메시지의 턴 좌표

1단계가 끝난 뒤부터 배포까지 사이에 구 코드가 만든 메시지는 `turn_seq`가 비어 있다. 기억
파이프라인은 `app/services/memory_pipeline.py` 181행의 `AND m.turn_seq IS NOT NULL`로 거르므로,
**좌표가 없는 메시지는 영원히 장기 기억에 들어가지 않는다.**

- [ ] 배포 후 남은 개수를 센다.
      `SELECT count(*) FROM messages WHERE kind='normal' AND turn_seq IS NULL;`
- [ ] 0이 아니면 좌표를 매긴다.

**⚠ 22번 파일을 다시 돌리면 안 된다.** 그 파일은 좌표 없는 메시지를 **과거 대화로 보고**
1번부터 번호를 매기면서 기존 번호를 전부 위로 밀어 올린다. 빈 구간 메시지는 가장 최근
대화인데 가장 앞 번호를 받게 되어 **시간 순서가 뒤집힌다.** 참조하는 표 9개와 커서까지 같이
밀리므로 되돌리기도 어렵다.

**올바른 방법은 기존 최대 번호 뒤에 이어 붙이는 것이다.** 사용자별로 `max(turn_seq)`를 구하고,
좌표 없는 메시지를 `id` 순서대로 그 뒤에 매긴다. 사용자 발화가 턴을 열고 뒤따르는 캐피 응답이
같은 턴을 닫는 규칙(`turn_position` 1=사용자, 2=캐피)은 22번 파일과 같다.

**1단계와 배포를 같은 날 끝내면** 이 작업이 거의 필요 없다. 몇 시간이면 대상이 수십 건이다.
며칠이 걸리면 수백 건이 되고, 그만큼의 대화가 기억에서 빠진다.


## 중단 기준

아래 중 하나라도 나오면 **그 자리에서 멈추고 보고한다.**

| 신호 | 뜻 |
|---|---|
| `source='none'` 행 수와 본문 빈 행 수가 다르다 | 본문이 있는 일기가 지워진다(3단계 직전 확인) |
| `diary_recall_documents`가 0이다 | 13번·18번 순서가 어긋났다 |
| 웰컴 일기가 613으로 늘지 않았다 | 12번 백필이 제대로 안 돌았다 |
| 미리보기에서 오류가 난다 | 순서나 사전 준비가 잘못됐다 |
| 구 코드의 일기 생성이 실패한다 | 호환 트리거가 제대로 동작하지 않는다 |

## 되돌리기

**3단계 전이라면 잃을 것이 없다.** 1단계와 1.5단계에는 삭제가 하나도 없다. 코드를 이전
이미지로 되돌리면 그만이다. 신규 표와 새로 만든 기억은 남아 있어도 구 코드가 읽지 않는다.

되돌릴 때 잡 처리기는 따로 손댈 것이 없다. 구 이미지에는 `worker/consumer.py`가 없어서
새 `deploy.sh`가 기동을 건너뛰고, 이미 떠 있던 처리기 컨테이너도 같이 내린다. 큐에 쌓인 잡은
그대로 남아 다시 배포할 때 처리된다.

3단계를 실행한 뒤에는 `DELETE FROM diaries WHERE source='none'`로 지운 행이 되돌아오지 않는다.
지우기 전에 `diary_generation_results`로 옮기고 이 행들은 전부 본문 길이가 0이라 사용자가 보는
일기는 사라지지 않지만, `diaries`로는 복구되지 않는다. **그래서 3단계는 배포가 확실히 안정된
뒤에 한다.**

되돌린다면 `diaries`에 붙은 CHECK 3종과 `display_date NOT NULL`도 같이 내려야 한다. 구 코드는
그 값들을 채우지 않기 때문이다(호환 트리거를 이미 지웠다면 더욱 그렇다).
