# 운영 전환 런북

새 대화·기억 구조를 운영(main / prod DB)으로 옮기는 절차. **dev에서 전 단계를 마친 뒤에만** 연다.

> **실제 실행은 `docs/MIGRATION_CHECKLIST.md`를 보고 한다.** 그 문서에 26개 파일의 적용 순서와
> 단계별 확인 항목이 목록으로 있다. 이 문서는 왜 그렇게 하는지를 설명하는 배경 문서다.

측정은 2026-08-06 기준이고, 3장 이후의 숫자는 2026-08-07에 다시 확인했다.

---

## 1. 지금 어떤 상태인가

| | dev | prod |
|---|---|---|
| public 테이블 | 53 | 25 |
| 인덱스 | 154 | 65 |
| 제약 | 272 | 105 |
| 함수 / 트리거 | 129 / 6 | 96 / 4 |
| `schema_migrations` | 있음 | **없음** |
| 사용자 | 3 | 618 |
| 대화 메시지(normal) | 261 | 34,532 (= 17,266턴) |
| 일기 | 4 | 7,959 |
| legacy 기억 벡터(`vecs.memories`) | — | 9,251 |

**prod에만 있는 테이블은 0개다.** 전환에 DROP이 필요 없고, 되돌릴 때 잃을 것도 없다.

prod는 `20260729`까지 적용된 상태이고 `20260804` 이후가 전부 남아 있다.

**시작 전에 반드시 해결해야 하는 것이 둘 있다.**

**첫째, `schema_migrations` 표를 사람이 먼저 만들어야 한다.** `db/apply.py`는 파일을 실행한 뒤
매번 `public.schema_migrations`를 조회해 기록을 남긴다(`db/apply.py` 33행·42행). 그런데 그 표를
만드는 문장은 `20260804_zzz_conversational_recall.sql` 안에 있고, 이 파일은 적용 목록에서
**12번째**다. 즉 **앞의 11개 파일이 전부 실패한다.** dry-run도 같은 경로를 지나므로 미리보기조차
안 된다. 표를 만드는 파일도 명령도 저장소에 없으므로 별도로 준비해야 한다.

**둘째, `db/apply.py`가 prod 접속을 막는다.** 24행의 `assert_dev_target`이 개발 대상인지 확인하고
아니면 중단시킨다. `--allow-prod` 같은 통로를 먼저 추가해 머지해야 한다.

확장은 이렇다. prod에 `vector`·`pgcrypto`는 **이미 있고**, `pg_trgm`은 **없다**. 다만
`20260804_diary_search.sql` 5행과 `20260804_zzz_conversational_recall.sql` 8행이
`CREATE EXTENSION IF NOT EXISTS pg_trgm`으로 직접 설치하고, 접속 계정 `postgres`에 설치 권한이
있으므로 **따로 준비할 필요는 없다.**

---

## 2. 데이터가 새 제약을 견디는가 — 이미 확인했다

```bash
PYTHONPATH=. uv run python scripts/preflight_cutover.py --env prod
```

이 스크립트는 **읽기 전용**이라 언제 돌려도 안전하다. prod는 현재 적용 가능한 항목을 전부
통과한다: 일기 날짜 결측 0 · welcome 중복 0 · 하루 일기 중복 0 · 미지의 source 0 ·
타임존 결측 0 · 고아 메시지 0 · 고아 루틴완료 0 · 대화 짝 불일치 0.

전환 직후 한 번 더 돌린다. 그때는 `⏭`로 건너뛴 4개 항목(턴 좌표·기억 참조·커서·legacy 잔류)도
실제로 측정되며, 전부 0이어야 끝난 것이다.

---

## 3. 적용 순서

> ⚠️ **파일명 오름차순 그대로 적용하면 안 된다.** 예전에 이 문서는 "파일명이 곧 순서"라고
> 적어 두었지만 **사실이 아니다.** 아래 3.0절의 예외를 반드시 먼저 읽는다.

`db/migrations/`의 `20260804*` → `20260805*` → `20260806*` 총 27개 중
`20260804_zz_memory_contract.sql`을 뺀 **26개**가 적용 대상이다(그 파일은 dev에도 적용된 적이
없다). 기본 흐름은 파일명 오름차순이되, 3.0절의 예외를 적용한 뒤 실행한다.

### 3.0 파일명 순서가 깨지는 곳

**`privacy_active_backfill` → `privacy_epoch` 순서를 뒤집어야 한다.** 알파벳으로는
`active`가 `epoch`보다 앞이라 `20260805_privacy_active_backfill.sql`이 먼저 오는데, 이 파일은
`privacy_subject_barriers`의 `epoch` 컬럼에 INSERT한다. 그 컬럼을 만드는 것은
`20260805_privacy_epoch.sql` 24행이다. **그대로 돌리면 컬럼이 없어서 실패한다.**
`privacy_epoch.sql`을 먼저 적용한다.

나머지 파일도 파일명이 아니라 **서로가 만든 표와 컬럼을 기준으로** 순서를 확인한 뒤 실행한다.

```bash
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod            # dry-run
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod --commit   # 반영
```

`db/apply.py`는 기본이 dry-run이고 `--env prod`를 명시해야만 운영으로 간다. **한 번에 하나씩,
dry-run으로 먼저 본다.**

### 3.1 `profiles`를 건드리는 파일 하나 — 미리 확인할 것

`20260806_normalize_profile_language.sql`은 **기존 사용자 행을 직접 고치는** 유일한 파일이다.
`profiles.language` 값을 `ko`·`en`·`ja` 셋 중 하나로 좁히고, 앞으로 들어올 값도 같은 규칙으로
바꾸는 DB 장치(트리거 `trg_normalize_profile_language`)를 만든다. 컬럼 기본값도 `ko`에서 `en`으로
바꾼다. 값을 되돌릴 수는 없으므로 적용 전에 분포를 찍어 둔다.

```sql
SELECT language, count(*) FROM public.profiles GROUP BY language ORDER BY 2 DESC;
```

**2026-08-07 운영 실측 — 값이 바뀌는 사람은 0명이다.**

| 지금 값 | 인원 | 적용 후 |
|---|---|---|
| `ko` | 447 | `ko` 그대로 |
| `ja` | 130 | `ja` 그대로 |
| `en` | 46 | `en` 그대로 |

운영 사용자 623명이 이미 셋 중 하나만 쓰고 있어서 이 파일은 기존 데이터를 바꾸지 않는다.
실제로 달라지는 것은 앞으로 들어올 값(트리거)과 컬럼 기본값뿐이다.

적용 뒤에는 위 조회를 다시 돌려 세 값만 남았는지 보고, `zh-Hant-TW` 같은 값을 한 번
넣어 `en`으로 저장되는지 확인한다.

### 순서를 뒤집으면 안 되는 두 곳

**삭제 장벽** — `20260805_privacy_epoch.sql`(컬럼만) → `20260805_privacy_active_backfill.sql`
순서만 지키면 된다(이유는 3.0절).

`privacy_active_backfill.sql` 머리말에는 "구 코드는 `privacy_subject_barriers`에 행이 있으면
무조건 차단으로 읽으므로 코드 배포 뒤에 적용하라"고 적혀 있다. **운영 전환에는 해당하지 않는다.**
2026-08-07 확인 결과 운영 코드(`origin/main`) 전체에 `privacy_subject_barriers`를 읽는 곳이
**한 군데도 없다.** 그 경고는 dev에서 표와 구 코드가 함께 있던 시기의 이야기다. 운영은 구 코드가
이 표의 존재 자체를 모르므로 행을 미리 깔아도 아무도 막히지 않고, 이후 배포되는 새 코드는
status를 보고 판단한다. **따라서 이 파일도 코드 배포보다 먼저 적용한다** —
[[migration-before-merge]] 규칙의 예외가 아니다.

**턴 좌표** — `20260806_backfill_turn_seq.sql`은 `20260804_zzz_conversational_recall.sql`이
`messages.turn_seq` 컬럼을 만든 **뒤**에 와야 한다. 파일명 순서가 이미 그렇다.

### 왜 legacy 테이블을 만들었다 지우는가

`20260804`대 마이그레이션은 legacy 기억 테이블 13종을 만들고 `20260806_drop_legacy_memory.sql`이
그걸 지운다. 낭비로 보이지만 건너뛰지 마라 — 중간 마이그레이션들이 그 테이블을 참조하고,
어느 것이 순수 legacy 전용인지 골라내는 판단이 실수하기 쉽다. prod 규모에서 이 왕복의 비용은
**측정상 무시할 수 있다**(가장 무거운 백필 질의가 35,696행에 58ms, 일기 전체 스캔 2.4ms).

---

## 4. 소요와 비용

**SQL 마이그레이션은 부담이 아니다.** 위 측정대로 전 구간이 초 단위다.

실제 부담은 **과거 대화를 기억으로 만드는 배치**다. 사용자당 턴 단위로 LLM을 부른다.

dev `ai_usage_ledger` 실측 단가(호출당 micro USD):

| 용도 | 단가 |
|---|---|
| `memory_extract` | 459 |
| `memory_consolidate` | 1,620 |
| `tool_decide` | 1,660 |
| `context_summary` | 2,724 |
| `tool_final` | 4,292 |
| `contract_compile` | 5,986 |
| `diary_generate` | 18,702 |

- 채팅 1턴 ≈ `tool_decide` + `tool_final` ≈ **$0.006**
- 과거 백필 17,266턴 ≈ 추출 $7.9 + 판정(관측된 21% 비율) $5.9 ≈ **일회성 $14**
- 소요: `job_memory_concurrency=2` × 잡당 약 3초 ≈ **7시간**

7시간 동안 memory 큐의 슬롯 2개를 백필이 차지한다 — 같은 슬롯을 쓰는 실시간 기억 처리가 그만큼
밀린다. **야간에 시작하고**, 급하면 `job_memory_concurrency`를 한시적으로 올린다.

---

## 5. 사용자를 새 구조로 옮기기 — 여기가 진짜 관문

마이그레이션이 끝나도 사용자는 `mode='legacy'`다. 그리고 **legacy 읽기 경로는 이미 삭제됐다.**
즉 이 상태로 두면 전원이 기억 0으로 대화한다. `prod`의 legacy 벡터 9,251개와
`chat_contexts.memory_text`(2026-08-07 기준 247명분)도 새 구조에서는 읽지 않는다 — 기억은 **대화에서 다시
만들어진다.**

```bash
# 1) 먼저 한 명으로 확인한다
PYTHONPATH=. uv run python scripts/enter_shadow_cohort.py --env prod --limit 1        # dry-run
PYTHONPATH=. uv run python scripts/enter_shadow_cohort.py --env prod --limit 1 --yes

# 2) 그 사용자의 기억이 실제로 쌓이는지 본다(잡이 흐르는지)
PYTHONPATH=. uv run python scripts/verify_cutover_gate.py --env prod

# 3) 통과하면 배치로 넓힌다
PYTHONPATH=. uv run python scripts/enter_shadow_cohort.py --env prod --limit 50 --yes
```

진입 경로가 실제로 작동하는지는 **적용 전에** 확인할 수 있다:

```bash
PYTHONPATH=. uv run python scripts/verify_shadow_entry.py --env prod
```

이 스크립트는 트랜잭션을 열어 진짜 진입 경로를 태운 뒤 **항상 롤백한다.** DB는 변하지 않는다.
과거에 이 경로가 조용히 교착돼 있었고(진입은 성공하는데 backfill이 시작되지 않았다) 테스트
1,400개가 전부 통과했기 때문에, 이 확인 없이는 넘어가지 않는다.

---

## 6. 되돌리기

테이블 삭제는 없다. 문제가 나면 코드를 이전 이미지로 되돌리는 것으로 대부분 해결된다 —
신규 테이블은 남아 있어도 구 코드가 읽지 않는다.

**다만 "데이터 손실 경로가 없다"고 말하면 안 된다. 행 삭제가 한 곳 있다.**
`20260804_zzz_conversational_recall.sql` 141행의 `DELETE FROM diaries WHERE source='none'`가
**운영 3,522행**을 지운다(전체 일기 7,992행의 44%). 2026-08-07 확인 결과 이 3,522행은
**전부 본문 길이가 0**이고, 삭제 전에 `diary_generation_results`로 옮겨지므로 사용자가 보는
일기가 사라지지는 않는다. 그래도 되돌릴 수 없는 삭제이므로 적용 전에 행 수를 찍어 둔다.

```sql
SELECT count(*) FROM diaries WHERE source='none';
SELECT count(*) FROM diaries WHERE source='none' AND coalesce(length(content),0)=0;
```

두 값이 같아야 한다. 다르면 **본문이 있는 일기가 지워진다는 뜻이므로 중단한다.**

단, 기존 테이블에 대한 이 두 변경은 구 코드와 함께 쓸 수 없다:

- `diaries`에 붙는 CHECK 3종과 `display_date NOT NULL` — 구 코드는 `kind`·`record_status`를
  채우지 않으므로 **일기 생성이 실패한다.** 코드를 되돌린다면 이 제약도 같이 내려야 한다.
- `diaries_user_date_uq` → `diaries_one_daily_uq` 교체 — 되돌릴 때 옛 UNIQUE를 다시 만들려면
  그 사이 생긴 행이 중복이 아닌지 먼저 세야 한다.

`chat_contexts.memory_text`는 **지우지 않는다**(2026-08-07 기준 운영 247명분). 새 구조가 안
읽을 뿐이라 되돌리면 그대로 살아난다. 전환이 확실히 안정된 뒤에 따로 정리한다.

---

## 6.5 기억을 옮기기 전에 — 주인 없는 벡터 확인

과거 대화를 기억으로 만들기 전에 한 번 센다. 탈퇴한 사용자의 벡터가 남아 있는지 보는 것이다.

```sql
SELECT count(*) FROM vecs.memories v
WHERE NOT EXISTS (
  SELECT 1 FROM public.profiles p
  WHERE p.id = (v.metadata->>'user_id')::uuid
);
```

**0이면 그냥 넘어간다.** 0이 아니면 그 행들은 **옮기지 말고 지운다.** 이미 지워졌어야 할
데이터이고, 옮기면 없는 사용자의 기억이 새 구조로 따라 들어간다.

왜 의심하는지는 `DEV_STATUS.md`의 "탈퇴한 사용자의 기억 벡터"에 적어 뒀다. 요약하면 탈퇴를
처리하는 moly-auth가 `public` 스키마에서 지우려 하는데 벡터는 `vecs` 스키마에 있고, 실패해도
경고 로그만 남고 넘어간다. 여기서 개수가 0이 아니면 그 의심이 맞은 것이다.

숫자가 나오면 앞으로 같은 일이 안 생기도록 삭제 경로를 정하는 것까지 해야 끝이다.

## 7. 전환 후 확인

```bash
PYTHONPATH=. uv run python scripts/preflight_cutover.py --env prod   # 전 항목 0
PYTHONPATH=. uv run python scripts/verify_privacy_barriers.py --env prod
PYTHONPATH=. uv run python scripts/verify_prompt_cache.py
```

그리고 실제로 대화를 한 번 해 본다. 배선이 끝난 것과 동작하는 것은 다르다 — 이 프로젝트에서
그 차이로 놓친 결함이 반복해서 나왔다.
