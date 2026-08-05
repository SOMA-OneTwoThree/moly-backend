# DB migration runbook

## 기억 시스템 전환

기억 런타임 설계는 `docs/ARCHITECTURE.md` §5.2, 데이터 모델과 불변조건은 `docs/ERD.md` §7을
기준으로 한다. 기존 DB에는 아래 순서로 적용한다.

1. `20260804_async_jobs.sql`
2. `20260804_memory_normalization.sql`
3. `20260804_memory_cutover_guard.sql`
4. `20260804_relationship_profiles.sql`
5. `20260804_conversation_checkpoints.sql`
6. `20260804_checkpoint_generation.sql`
7. `20260804_chat_last_active.sql`
8. `20260804_diary_search.sql`
9. `20260804_memory_embeddings.sql`
10. `20260804_job_replay_lineage.sql`
11. `20260804_zz_memory_contract.sql` — 기존 normalized backfill/replay 완료 뒤 legacy snapshot 제거
12. `20260804_zzz_conversational_recall.sql` — 턴 CAS, episode/diary recall, exact suppression,
    welcome 프롤로그, reference/focus, privacy/retention 계약
13. `20260804_zzzz_conversational_recall_backfill.sql` — 기존 사용자 welcome·diary provenance와
    episode/diary recall projection/job 재구축
14. `20260804_zzzzz_conversational_recall_hardening.sql` — 삭제 장벽 제외, provenance 수렴,
    SHA-256/fenced vector 재색인과 bounded missing-vector repair 상태

## 기억·페르소나·관계 재설계 (docs/ARCHITECTURE-capi.md)

15장 전환 순서를 따른다. 아래는 additive 단계이며 legacy 테이블을 건드리지 않는다.

15. `20260805_ai_usage_ledger.sql` — (1단계) `ai_price_catalog`·`ai_usage_ledger`·`job_attempts`.
    구조 전환 **전에** 적용해 legacy 비용까지 같은 표면으로 잰다. catalog v1 시드 포함.
16. `20260805_privacy_epoch.sql` — (2단계 a) 삭제 장벽에 `active` state와 `epoch` 추가.
    **컬럼만 추가하고 행은 만들지 않는다** — 안전하게 먼저 적용 가능.
17. `20260805_privacy_active_backfill.sql` — (2단계 c) 🚨 **코드 배포 뒤에만 적용**.
19. `20260805_relationship_render.sql` — (5단계) relationship_events.turn_seq, locale render projection.
20. `20260805_mem0_v2_collection.sql` — v2 벡터 컬렉션(`vecs.moly_memories_v2`). **런타임이 만들지 않는다** —
    adapter는 이미 존재하는 컬렉션만 연다.

18. `20260805_memory_v2_tables.sql` — (4단계) v2 테이블 additive 생성. 아무것도 지우지 않는다:
    `memory_pipeline_states` · `mem0_ingest_candidates`(+sources) · `mem0_memory_registry`(+sources) ·
    `user_interaction_contracts`(+items) · `user_relationship_states` · `relationship_events` ·
    `legacy_recall_tombstones` · `provider_backoffs`, checkpoint v2 컬럼과 async_jobs routing 컬럼.

### ⚠️ 삭제 장벽 전환 순서 (다른 마이그레이션과 반대)

구 코드는 `privacy_subject_barriers`에 **행이 있으면 무조건 차단**으로 읽는다. status-aware
`authorize_job`/`ensure_subject_active`가 배포되기 전에 `active` 행을 깔면 **전 사용자의 대화와
잡이 즉시 막힌다.** 그래서 이 한 건만 "마이그레이션 먼저" 규칙의 예외다.

```
1. 20260805_privacy_epoch.sql 적용              (컬럼만 — 안전)
2. status-aware 코드 dev 배포 + /health 확인     ← 반드시 여기까지 끝난 뒤
3. 20260805_privacy_active_backfill.sql 적용    (active 행 + 신규 profile 트리거)
4. 검증 스크립트 연속 2회 통과 → privacy_barrier_mode=enforced
```

```bash
PYTHONPATH=. uv run python scripts/verify_privacy_barriers.py
```

`missing`(장벽 없는 profile)이 0이 아니면 `enforced`로 올리지 않는다 — 그 사용자들이 전부 막힌다.

각 파일은 먼저 기본 dry-run으로 실행하고, 성공한 동일 파일만 `--commit`으로 적용한다.

```bash
uv run python db/apply.py db/migrations/<파일명>.sql
uv run python db/apply.py db/migrations/<파일명>.sql --commit
```

과거 대화 source backfill과 기억 잡 처리가 모두 끝난 뒤 검증한다.

```bash
uv run python scripts/backfill_normalized_memory.py --verify
uv run python scripts/replay_dead_memory_jobs.py
```

`20260804_zz_memory_contract.sql`은 미연결 user 메시지, 미해결 기억 잡, embedding 없는 active
fact, published 관계 프로필이 없는 사용자가 하나라도 있으면 전체를 중단한다. 이 게이트를 dry-run과
`--commit`으로 통과한 뒤에만 이전 `memory_text`, `memory_refreshed_at`, `memory_mode` 컬럼이 제거된다.
terminal dead 잡은 수정하거나 삭제하지 않고, 성공한 `replay_of` 자식으로 해소됐음을 증명한다.

---

## Appearance v2

### 새 DB (dev·CI)

`db/schema.sql` → `db/seed_and_triggers.sql` 순으로 적용하면 끝이다. 시드가 꾸미기 6종을
active로 넣으므로 가입·상점·장착이 바로 동작한다. 에셋 파일이 아직 버킷에 없으면 이미지
URL만 404이고 API 계약 자체는 정상이다.

### 기존 DB (staging·prod)

`20260713_appearance_v2_expand.sql`은 기존 API와 호환되는 additive 단계다. 먼저 적용해 둔다.

나머지는 유지보수 창에서 **연속으로** 실행한다. 시드 적용 시점부터 cutover 커밋까지는 가입이
중단된다 — `bootstrap_user`가 `slot='theme'`인 필수 상품 3종을 요구하는데 slot 전환은 cutover가
하기 때문이다.

1. 최종 에셋을 `{public_id}/v{asset_version}/…` 경로로 버킷에 올린다.
2. `scripts/verify_appearance_assets.py`로 매니페스트와 원격 이미지를 검증한다.
3. DB 백업 후 유지보수 창을 시작한다.
4. `db/seed_and_triggers.sql` 적용 — v2 `assets`·`asset_version`을 적재하고 6종을 active로 만든다.
   시드는 `slot`을 갱신하지 않는다(장착 행의 복합 FK 때문).
5. `20260713_appearance_v2_cutover.sql`을 dry-run한 뒤 적용 — `background` → `theme` 슬롯 전환,
   기존 사용자에게 `theme_default` 소급 지급·장착, 구형 구독 장착 행 삭제.
6. `moly-backend`와 `moly-auth` 새 버전을 함께 배포한다.
7. 신규 가입, 기본 지급, `/shop/products`, `/inventory`, 두 equipment 조회, 구매를 스모크 테스트한다.

cutover SQL은 최종 에셋이나 필수 기본 상품이 없으면 트랜잭션을 중단한다. 구형 assets를
새 필드로 임의 변환하거나 빈 카탈로그 상태로 진행하지 않는다.

### 에셋 교체

시드의 `ON CONFLICT`는 `asset_version`이 올라갈 때만 `assets`를 덮는다. 새 아트를 넣을 때는
`v{n+1}` 경로로 업로드하고 시드의 URL과 `asset_version`을 함께 올린다. 버전을 올리지 않고
URL만 바꾸면 iOS 캐시가 갱신되지 않는다.

### head 슬롯 분리 + rightside 자세 (`20260719_hat_glasses_rightside.sql`)

`head` 슬롯을 `hat`/`glasses`로 나눠 모자·안경 동시 착용을 허용하고, 착용 아이템에 새 자세
(`rightside`) upright 레이어를 더한다. 구버전 앱(서버가 버전을 식별할 수 없음)은 새 슬롯 값을
디코딩하지 못해 상점 응답 전체가 깨지므로, 새 서버는 레거시 경로에서 hat/glasses를 `head`로,
에셋을 구 자세로 투영하고 신버전은 `/v2/*` 경로를 쓴다.

새 DB(dev·CI): `db/schema.sql` → `db/seed_and_triggers.sql`이면 끝이다. 시드가 hat/glasses 슬롯과
`rightside` 키를 이미 담는다.

기존 DB(staging·prod): 유지보수 창에서 **연속으로** 실행한다. 구 서버는 `slot='glasses'`/`'hat'`을
만나면 `/shop/products`가 500이 된다(레거시 slot Literal 위반).

1. `rightside` upright 레이어를 `{public_id}/v{asset_version}/rightside/upright.png` 경로로 버킷에 올린다.
2. `scripts/verify_appearance_assets.py`로 매니페스트와 원격 이미지를 검증한다(파일 업로드 전에는
   `--skip-fetch`로 DTO·URL 버전만 검증). 각 상품을 v2·레거시 두 계약으로 검증한다.
3. DB 백업 후 유지보수 창을 시작한다.
4. `db/seed_and_triggers.sql` 적용 — 이미 slot을 갱신하지 않으므로(복합 FK) hat/glasses 전환은
   마이그레이션이 하고, 시드는 새 DB용 리터럴과 bootstrap_user 갱신을 제공한다.
5. `20260719_hat_glasses_rightside.sql`을 dry-run한 뒤 적용 — head → hat/glasses 슬롯 전환,
   장착 행 이전, `rightside` 자산 패치, bootstrap_user 갱신. `asset_version`은 올리지 않아 구 자세 URL은
   불변이라 구버전 앱 캐시가 유지된다.
6. `moly-backend`와 `moly-auth` 새 버전을 함께 배포한다. `moly-auth`의 레거시 `/me`는
   hat/glasses를 단일 `head_id`로 투영해야 한다.
7. 레거시 `/me`·`/shop/products`·`/inventory`·두 equipment 조회에 hat/glasses·`rightside`가
   노출되지 않는지, `/v2/*` 4종이 새 슬롯과 rightside upright를 반환하는지 스모크 테스트한다.

---

## 프로덕션 적용 — 순서와 위험 (2026-08-05 실측)

**현재 prod에는 이 작업이 하나도 반영돼 있지 않다.** 실측 스키마 차이:

| | 테이블 수 |
|---|---|
| prod | 25 |
| dev | 67 |
| **dev에만 있음** | **42** |
| prod에만 있음 | 0 |

prod에서 **사라지는 테이블은 없다**. 전부 추가다.

`schema_migrations`는 dev에만 있고 12건만 기록돼 있다(추적 테이블보다 먼저 적용된 것들은
기록이 없다). prod에는 추적 테이블 자체가 없으므로 **41개 파일을 순서대로 적용**해야 한다.

### ⚠️ 유저 데이터를 건드리는 구문 (실측 확인 완료)

| 마이그레이션 | 구문 | prod 영향 | 판정 |
|---|---|---|---|
| `20260713_appearance_v2_cutover` | `DELETE FROM user_items WHERE source='subscription'` | **0행** | 안전 |
| `20260804_zzz_conversational_recall` | `DELETE FROM diaries WHERE source='none'` | 2,946행 | 안전 — 같은 transaction에서 `diary_generation_results`로 먼저 옮긴다(표현 변경이지 손실 아님) |
| `20260804_zz_memory_contract` | `ALTER TABLE chat_contexts DROP COLUMN memory_text …` | **245행에 내용 있음** | 🔴 **위험** |

### 🔴 `chat_contexts.memory_text` DROP — 선행 조건

이 컬럼은 캐피가 그 사용자에 대해 들고 있던 **기억 스냅샷**이다. prod 517명 중 **245명**에
내용이 있다. 새 기억이 채워지기 **전에** 이 마이그레이션을 돌리면 그 245명은 캐피가
자기를 잊은 상태가 된다.

적용 순서를 반드시 지킨다:

1. 나머지 마이그레이션을 먼저 적용해 새 기억 테이블을 만든다
2. **backfill을 돌려 새 구조에 기억을 채운다** (`scripts/backfill_normalized_memory.py` 등)
3. 채워진 것을 **행 수로 검증**한다 — `memory_text`가 있던 245명이 새 구조에도 있는지
4. 그 확인이 끝난 뒤에만 `20260804_zz_memory_contract.sql`을 적용한다

3번을 건너뛰면 되돌릴 수 없다. DROP된 컬럼은 복구 대상이 백업뿐이다.

### 적용 명령

```
python db/apply.py db/migrations/<파일> --env prod            # dry-run (기본)
python db/apply.py db/migrations/<파일> --env prod --commit   # 실반영
```

**배포는 머지에 붙어 있으므로 마이그레이션이 항상 먼저다.** 순서가 바뀌면 새 코드가 없는
테이블을 읽어 프로덕션 대화가 죽는다(과거 사고 있음).
