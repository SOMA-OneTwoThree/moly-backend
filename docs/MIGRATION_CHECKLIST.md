# 운영 DB 마이그레이션 실행 목록

운영 서버(voice.moly.asia)의 데이터베이스를 새 구조로 옮기는 작업을 순서대로 적은 문서다.
위에서부터 하나씩 하면 된다. 배경 설명은 `docs/CUTOVER.md`에 있고, 이 문서는 **실행 목록**이다.

- 기준 시점: 2026-08-07. 아래 숫자는 전부 운영 DB에서 실제로 조회한 값이다.
- 운영 사용자 622명 · 대화 메시지 36,575건 · 일기 7,992건 · 웰컴 일기 612건.
- 적용 대상 파일은 **26개**다(`db/migrations/20260804*`~`20260806*` 27개 중
  `20260804_zz_memory_contract.sql` 제외 — dev에도 적용된 적이 없다).

## 전체 그림

| 단계 | 하는 일 | 서비스 영향 |
|---|---|---|
| 0단계 | 사전 준비물 만들기(코드·SQL) | 없음 |
| 1단계 | 마이그레이션 26개 적용(한 줄만 빼고) | 없음 — 구 코드가 계속 돈다 |
| 2단계 | `main` 머지 → 운영 배포 | 배포 시간만큼 |
| 3단계 | 호환 장치 제거·미뤄둔 한 줄 실행 | 거의 없음 |

**1단계를 다 끝내고 확인한 뒤에 2단계로 간다.** 1단계는 구 코드가 도는 중에 해도 안전하도록
설계했다.

---

## 0단계 — 사전 준비물

이 셋이 없으면 1단계를 **시작조차 못 한다.**

### 0-1. `schema_migrations` 표 만들기

- [ ] 운영에 `public.schema_migrations` 표를 만드는 SQL 파일을 작성한다.
- [ ] 이미 적용된 `20260729`까지의 파일 이름을 그 표에 미리 기록한다.

**왜 필요한가.** `db/apply.py`는 파일을 실행한 뒤 **매번** `public.schema_migrations`를 조회하고
기록을 남긴다(33행·42행). 그런데 그 표를 만드는 문장은 `20260804_zzz_conversational_recall.sql`
안에 있고, 이 파일은 적용 목록에서 11번째다. **그 앞의 10개가 전부 실패한다.** 미리보기(dry-run)도
같은 경로를 지나므로 미리 볼 수조차 없다. 표를 만드는 파일도 명령도 저장소에 없다.

### 0-2. `db/apply.py`에 운영 적용 통로 추가

- [ ] `--allow-prod` 같은 선택지를 추가하고 PR을 올려 **머지까지 끝낸다.**

**왜 필요한가.** `db/apply.py` 24행의 `assert_dev_target`이 개발 대상인지 확인하고 아니면
중단시킨다. 지금 상태로는 운영에 아무것도 적용할 수 없다.

### 0-3. 일기 호환 트리거 작성

- [ ] `diaries`에 `BEFORE INSERT ... FOR EACH ROW` 트리거를 만드는 SQL을 작성한다.

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
2. 이 트리거는 3단계에서 **반드시 지운다.** 남겨 두면 새 코드가 넣은 값을 덮어쓴다.

---

## 1단계 — 배포 전 마이그레이션 26개

### 실행 방법

```bash
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod            # 미리보기
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod --commit   # 반영
```

**한 번에 한 개씩, 미리보기를 먼저 본다.** 실패하면 그 자리에서 멈추고 원인을 본다.

### 시작 전 확인

- [ ] 0단계 세 가지가 전부 끝났다.
- [ ] 호환 트리거를 **먼저** 설치했다(11번 파일보다 앞).
- [ ] 읽기 전용 사전 점검을 돌려 통과했다.
      `PYTHONPATH=. uv run python scripts/preflight_cutover.py --env prod`
- [ ] 삭제될 일기 행 수를 기록했다. 아래 두 값이 **같아야** 한다. 다르면 **중단한다.**

```sql
SELECT count(*) FROM diaries WHERE source='none';                                  -- 3522
SELECT count(*) FROM diaries WHERE source='none' AND coalesce(length(content),0)=0; -- 3522
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
| 7 | `20260804_memory_cutover_guard.sql` | |
| 8 | `20260804_memory_normalization.sql` | **순서 바꿈** — 아래 9번이 `memory_facts`를 쓴다 |
| 9 | `20260804_memory_embeddings.sql` | **순서 바꿈** |
| 10 | `20260804_relationship_profiles.sql` | |
| 11 | `20260804_zzz_conversational_recall.sql` | **⚠ 118행 한 줄을 빼고 적용한다. 아래 참고** |
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
| 22 | `20260806_backfill_turn_seq.sql` | `messages` 36,575건을 갱신한다 |
| 23 | `20260806_drop_legacy_memory.sql` | 운영에 해당 표가 없어 **아무 일도 안 한다** |
| 24 | `20260806_drop_legacy_tombstones.sql` | 위와 같음 |
| 25 | `20260806_normalize_profile_language.sql` | 값이 바뀌는 사람 **0명** |
| 26 | `20260806_rls_gap.sql` | |

### 11번 파일에서 빼야 하는 한 줄

```sql
ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq;   -- 118행
```

- [ ] 이 한 줄을 뺀 사본을 만들어 적용한다.

**왜 빼는가.** 구 코드의 웰컴 일기 저장이
`on_conflict_do_nothing(index_elements=["user_id", "diary_date"])`를 쓴다. 이 제약을 지우면
데이터베이스가 맞는 인덱스를 찾지 못해 오류를 낸다. **이 판정은 실행 계획을 짤 때 일어나서
트리거가 돌기 전이다.** 그래서 호환 트리거로도 막을 수 없다. 3단계에서 지운다.

### 13번과 18번의 순서를 지켜야 하는 이유

13번(`hardening`) 28~37행은 `privacy_subject_barriers`에 행이 있는 사용자의 파생 데이터와
웰컴 일기를 지운다. 18번(`privacy_active_backfill`)이 그 표에 **622명분 행을 넣는다.**
18번을 먼저 돌리면 그 뒤에 만들어진 웰컴 일기가 지워질 수 있다. **13번을 먼저 한다.**

### 1단계 확인

- [ ] 26개 전부 `schema_migrations`에 기록됐다.
- [ ] 사전 점검을 다시 돌려 통과한다.
- [ ] 구 코드가 도는 상태에서 **일기가 정상 생성되는지** 확인한다(호환 트리거 동작 확인).
- [ ] 대화가 정상인지 확인한다.
- [ ] 웰컴 일기 수가 그대로인지 본다. `SELECT count(*) FROM diaries WHERE source='welcome';` → 612

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
- [ ] 미뤄 둔 한 줄을 실행한다.
      `ALTER TABLE public.diaries DROP CONSTRAINT IF EXISTS diaries_user_date_uq;`
- [ ] 일기 생성과 웰컴 일기 저장이 정상인지 확인한다.

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
| `source='none'` 행 수와 본문 빈 행 수가 다르다 | 본문이 있는 일기가 지워진다 |
| 웰컴 일기 수가 612보다 줄었다 | 13번·18번 순서가 어긋났다 |
| 미리보기에서 오류가 난다 | 순서나 사전 준비가 잘못됐다 |
| 구 코드의 일기 생성이 실패한다 | 호환 트리거가 제대로 동작하지 않는다 |

## 되돌리기

테이블 삭제가 없어서 코드를 이전 이미지로 되돌리는 것으로 대부분 해결된다. 신규 표는 남아 있어도
구 코드가 읽지 않는다.

**되돌릴 수 없는 것이 하나 있다.** 11번 파일의 `DELETE FROM diaries WHERE source='none'`가
3,522행을 지운다. 지우기 전에 `diary_generation_results`로 옮기고, 이 행들은 전부 본문 길이가
0이라 사용자가 보는 일기는 사라지지 않는다. 그래도 되돌아오지는 않는다.

되돌린다면 `diaries`에 붙은 CHECK 3종과 `display_date NOT NULL`도 같이 내려야 한다. 구 코드는
그 값들을 채우지 않기 때문이다(호환 트리거를 이미 지웠다면 더욱 그렇다).
