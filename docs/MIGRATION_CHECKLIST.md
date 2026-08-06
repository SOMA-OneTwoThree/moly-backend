# 운영 DB 마이그레이션 실행 목록

운영 서버(voice.moly.asia)의 데이터베이스를 새 구조로 옮기는 작업을 순서대로 적은 문서다.
위에서부터 하나씩 하면 된다. 배경 설명은 `docs/CUTOVER.md`에 있고, 이 문서는 **실행 목록**이다.

- 기준 시점: 2026-08-07. 아래 숫자는 전부 운영 DB에서 실제로 조회한 값이다.
- ⚠️ **아래 숫자는 참고용이다. 매일 늘어나므로 실행 당일에 다시 센다.** 중단 기준은 절대값이
  아니라 **두 값이 같은지**로 판단한다(아래 "시작 전 확인" 참고).
- 2026-08-07 기준 — 운영 사용자 622명 · 대화 메시지 `normal` 35,378 + `greeting` 1,201 ·
  일기 8,573건(그중 웰컴 612, 본문 없는 `none` 4,078, 발행됨 4,495) · 구 기억 벡터 9,545건.
- 적용 대상 파일은 **26개**다(`db/migrations/20260804*`~`20260806*` 27개 중
  `20260804_zz_memory_contract.sql` 제외 — dev에도 적용된 적이 없다).
- dev의 적용 기록에 `20260805_push_personalization.sql`이 남아 있지만 **신경 쓰지 않아도 된다.**
  그 기능은 되돌려졌고(`2a58f56` Revert), 만들었던 표도 dev·운영 양쪽에 없다. 기록만 남은 것이다.

## 전체 그림

| 단계 | 하는 일 | 서비스 영향 |
|---|---|---|
| 0단계 | 사전 준비물 만들기(코드·SQL) | 없음 |
| 1단계 | 마이그레이션 26개 적용(두 줄만 빼고) | 기능은 계속 된다. 다만 **11번 파일이 도는 몇 초 동안 요청이 대기**한다 |
| 1.5단계 | 과거 대화에서 기억 미리 만들기 | 없음 — 새 표에만 쓴다 |
| 2단계 | `main` 머지 → 운영 배포 | 배포 시간만큼 |
| 3단계 | 호환 장치 제거·미뤄둔 두 줄 실행·빈 구간 정리 | 거의 없음 |

**1단계를 다 끝내고 확인한 뒤에 2단계로 간다.** 1단계는 구 코드가 도는 중에 해도 안전하도록
설계했다.

**GitHub Actions가 필요한 단계는 2단계뿐이다.** 0·1·1.5·3단계는 사람이 자기 컴퓨터에서
`db/apply.py`와 스크립트로 돌린다. 그래서 Actions가 멈춰 있어도 1단계와 1.5단계를 먼저 해 두고,
Actions가 돌아온 뒤에 2단계를 해도 된다. 원래 순서가 "마이그레이션 먼저, 배포 나중"이라
어긋나지 않는다.

---

## 0단계 — 사전 준비물

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

## 1단계 — 배포 전 마이그레이션 26개

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

### 1단계 확인

- [ ] 26개 전부 `schema_migrations`에 기록됐다.
- [ ] 사전 점검을 다시 돌려 통과한다.
- [ ] 구 코드가 도는 상태에서 **일기가 정상 생성되는지** 확인한다(호환 트리거 동작 확인).
- [ ] 대화가 정상인지 확인한다.
- [ ] 웰컴 일기가 **612에서 613으로 하나 늘었는지** 본다.
      `SELECT count(*) FROM diaries WHERE source='welcome';` → **613**

      12번 백필이 웰컴 없는 사용자에게 웰컴을 만든다. 운영에 웰컴이 없는 프로필은 10명이지만
      대상은 첫 사용자 발화가 있는 사람뿐이라 **1명만 늘어난다.** 안 늘었으면 12번이
      제대로 안 돈 것이다.

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

## 1.5단계 — 기억 미리 채우기 (배포 전, 서비스 영향 없음)

마이그레이션은 기억 표를 만들 뿐 **내용을 하나도 만들지 않는다.** 이 단계를 건너뛰고 배포하면
캐피는 과거 대화를 전혀 기억하지 못한 채로 시작한다.

배포 전에 미리 해 둘 수 있다. 기억 추출이 쓰는 표가 전부 새 표라서 **구 코드가 영향을 받지
않는다.** 확인 결과 쓰기 대상은 `memory_pipeline_states`·`mem0_memory_registry`·
`mem0_memory_sources`·`mem0_ingest_candidates`·`async_jobs`·`relationship_events`·
`diary_recall_documents`이고, 구 코드는 이 중 어느 것도 참조하지 않는다. `messages`와
`chat_contexts`는 읽기만 한다.

### 기존 기억은 살리지 않는다

| 기존 것 | 운영 현재 | 처리 |
|---|---|---|
| `vecs.memories` (구 벡터) | 9,281건 | **버린다.** 새 코드가 읽지 않는다(참조 0건 확인) |
| `chat_contexts.memory_text` | 247명분 | **버린다.** 새 코드가 읽지 않는다 |

둘 다 지우지 않고 그대로 둔다. 되돌릴 때 필요하고, 새 코드가 안 읽을 뿐이라 무해하다. 정리는
전환이 확실히 안정된 뒤에 한다.

새 기억은 **과거 대화에서 다시 만든다.** 22번 파일이 매긴 턴 좌표가 그 재료다.

### 절차

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

### 주의

- **여기서 만든 기억은 배포 전까지 아무도 보지 못한다.** 잘못돼도 지우고 다시 만들면 된다.
  이것이 배포 전에 하는 가장 큰 이점이다.
- 이 작업이 도는 동안에도 사용자는 평소대로 대화한다. 그 사이 생기는 새 대화는 턴 좌표가
  없으므로 3단계의 "빈 구간 메시지" 처리로 이어서 넣는다.
- `.env.prod`를 다루므로 접속 정보가 화면이나 기록에 남지 않게 한다.

---

## 2단계 — 코드 배포

- [ ] dev 브랜치를 `main`으로 머지한다. **머지는 사용자가 한다.**
- [ ] 배포가 끝날 때까지 기다린다.
- [ ] 헬스 점검 4종을 확인한다.
- [ ] 대화·일기 조회가 정상인지 본다.

---

## 3단계 — 배포 후 마무리

배포가 안정된 것을 확인한 뒤에 한다. 둘 다 순식간에 끝나므로 서비스 영향은 거의 없다.

- [ ] 호환 트리거와 그 함수를 지운다. **새 코드가 값을 직접 채우므로 남겨 두면 안 된다.**
- [ ] `diaries_user_date_uq` 제약을 지운다.
      `ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq;`

- [ ] **삭제하기 전에 처리 표시를 다시 복사한다. 이 줄을 빼먹으면 데이터가 사라진다.**

```sql
INSERT INTO public.diary_generation_results(user_id,target_date,status,created_at)
SELECT user_id,diary_date,'no_entry',COALESCE(created_at,now())
FROM public.diaries WHERE source='none'
ON CONFLICT (user_id,target_date) DO NOTHING;
```

**왜 다시 하는가.** 11번 파일의 같은 INSERT는 1단계에서 **한 번만** 돈다. 그 뒤 배포까지
구 코드가 계속 `source='none'` 행을 만든다(하루 약 500건). 그 행들은 복사되지 않은 상태이므로
아래 삭제로 그냥 사라진다. 새 코드는 `diary_generation_results`를 "그날 처리 끝냈다"는 표시로
읽으므로(`app/services/diary_generation.py` 46~52행), 표시가 없으면 **이미 처리한 날의 일기를
다시 만들려 한다.** 이 INSERT는 `ON CONFLICT DO NOTHING`이라 몇 번을 돌려도 안전하다.

- [ ] 두 값이 같은지 확인한 뒤 삭제한다. 다르면 **중단한다.**

```sql
SELECT count(*) FROM diaries WHERE source='none';
SELECT count(*) FROM diaries WHERE source='none' AND coalesce(length(content),0)=0;
DELETE FROM public.diaries WHERE source='none';
```

- [ ] **빈 구간에 만들어진 일기를 회상 검색에 넣는다.** 1단계의 12·13번 파일은 한 번만 돌고,
      새 코드는 자기가 만든 일기만 색인한다. 그 사이 구 코드가 만든 발행 일기는
      `diary_recall_documents`·`diary_claim_sources`에 들어가지 않아 **회상에서 영영 빠진다.**
      `20260804_zzzzz_conversational_recall_hardening.sql` 101~126행의 재구축 문장을 한 번 더
      돌린다. 그 문장은 수렴형이라 재실행해도 안전하다.
- [ ] 일기 생성과 웰컴 일기 저장이 정상인지 확인한다.
- [ ] **1단계와 배포 사이에 쌓인 메시지에 턴 좌표를 매긴다.** 아래를 읽고 할 것.

### 빈 구간 메시지의 턴 좌표

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

### 나중에 따로 (급하지 않음)

- [ ] `chat_contexts.memory_text` 정리(247명분). 새 구조가 읽지 않을 뿐이라 남겨 둬도 무해하다.
      되돌릴 가능성이 완전히 사라진 뒤에 지운다.
- [ ] `vecs.memories`의 legacy 기억 벡터 9,251건 처리 방침 결정.
- [ ] 고아 벡터 57건 정리.
- [ ] `20260804_zz_memory_contract.sql` 적용 여부 결정. 적용한다면 그 안의 확인 블록이
      이미 지워진 표를 참조하므로 **그 부분을 먼저 걷어내야 한다.**

---

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

3단계를 실행한 뒤에는 `DELETE FROM diaries WHERE source='none'`로 지운 행이 되돌아오지 않는다.
지우기 전에 `diary_generation_results`로 옮기고 이 행들은 전부 본문 길이가 0이라 사용자가 보는
일기는 사라지지 않지만, `diaries`로는 복구되지 않는다. **그래서 3단계는 배포가 확실히 안정된
뒤에 한다.**

되돌린다면 `diaries`에 붙은 CHECK 3종과 `display_date NOT NULL`도 같이 내려야 한다. 구 코드는
그 값들을 채우지 않기 때문이다(호환 트리거를 이미 지웠다면 더욱 그렇다).
