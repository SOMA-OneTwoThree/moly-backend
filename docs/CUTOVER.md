# 운영 전환 런북

새 대화·기억 구조를 운영(main / prod DB)으로 옮기는 절차. **dev에서 전 단계를 마친 뒤에만** 연다.

측정은 전부 2026-08-06 실측이다.

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

prod는 `20260729`까지 적용된 상태이고 `20260804` 이후가 전부 남아 있다 — 깔끔한 prefix라
순서대로 적용하면 된다. 다만 **적용 기록이 없으므로**(schema_migrations 부재) 1단계에서 그 표를
먼저 만들고 이미 적용된 것을 기록해 둔다. 안 그러면 다음 사람이 또 이 조사를 반복한다.

prod에는 `vecs` 스키마와 pgvector 0.8.0이 **이미 있다** — 확장 설치는 필요 없다.

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

`db/migrations/`의 `20260804*` → `20260805*` → `20260806*`를 **파일명 오름차순 그대로** 적용한다.
파일명이 곧 순서다(`zz`·`zzz` 접두는 같은 날짜 안에서 순서를 강제하려고 붙었다).

```bash
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod            # dry-run
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일> --env prod --commit   # 반영
```

`db/apply.py`는 기본이 dry-run이고 `--env prod`를 명시해야만 운영으로 간다. **한 번에 하나씩,
dry-run으로 먼저 본다.**

### 순서를 뒤집으면 안 되는 두 곳

**삭제 장벽** — `20260805_privacy_epoch.sql`(컬럼만, 안전) → **코드 배포** →
`20260805_privacy_active_backfill.sql`. 구 코드는 `privacy_subject_barriers`에 행이 있으면
무조건 차단으로 읽으므로, status-aware 코드보다 먼저 `active` 행을 깔면 **전 사용자의 대화가
즉시 막힌다.** 이 한 건만 "마이그레이션 먼저" 규칙의 예외다.

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
`chat_contexts.memory_text`(244명분)도 새 구조에서는 읽지 않는다 — 기억은 **대화에서 다시
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

**테이블 삭제가 없으므로 데이터 손실 경로가 없다.** 문제가 나면 코드를 이전 이미지로 되돌리는
것으로 대부분 해결된다 — 신규 테이블은 남아 있어도 구 코드가 읽지 않는다.

단, 기존 테이블에 대한 이 두 변경은 구 코드와 함께 쓸 수 없다:

- `diaries`에 붙는 CHECK 3종과 `display_date NOT NULL` — 구 코드는 `kind`·`record_status`를
  채우지 않으므로 **일기 생성이 실패한다.** 코드를 되돌린다면 이 제약도 같이 내려야 한다.
- `diaries_user_date_uq` → `diaries_one_daily_uq` 교체 — 되돌릴 때 옛 UNIQUE를 다시 만들려면
  그 사이 생긴 행이 중복이 아닌지 먼저 세야 한다.

`chat_contexts.memory_text`는 **지우지 않았다**(prod 244명분). 새 구조가 안 읽을 뿐이라,
되돌리면 그대로 살아난다. 전환이 확실히 안정된 뒤에 따로 정리한다.

---

## 7. 전환 후 확인

```bash
PYTHONPATH=. uv run python scripts/preflight_cutover.py --env prod   # 전 항목 0
PYTHONPATH=. uv run python scripts/verify_privacy_barriers.py --env prod
PYTHONPATH=. uv run python scripts/verify_prompt_cache.py
```

그리고 실제로 대화를 한 번 해 본다. 배선이 끝난 것과 동작하는 것은 다르다 — 이 프로젝트에서
그 차이로 놓친 결함이 반복해서 나왔다.
