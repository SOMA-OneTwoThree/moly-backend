# DB 변경 관리

> 최종 확인: 2026-09-05. 대화·기억·꾸미기 전환은 dev와 prod에 끝났고, 오늘의 운세는 dev에만 적용했다.

## 기준

- 빈 DB의 기본 구조는 `db/schema.sql`이다.
- 이후 추가·변경 이력은 이 디렉터리의 날짜 접두사 SQL 파일이다.
- 현재 런타임 모델과 실제 DB가 맞는지는 `db/verify.py`로 확인한다.
- 적용 기록과 파일 SHA-256은 각 DB의 `public.schema_migrations`가 보관한다.
- 이미 적용된 파일은 수정하지 않는다. 변경이 필요하면 새 날짜 파일을 추가한다.

`db/schema.sql`에는 기본 도메인과 2026-08-04까지의 대화 기반 구조가 들어 있다. 장기 기억 v2,
사용자별 대화 약속, 관계 projection, 사용량 원장, 예정 시각 등 일부 구조는 2026-08-05 이후
마이그레이션에만 있다. 따라서 `schema.sql` 하나만 보고 운영 전체 구조라고 판단하지 않는다.
전체 테이블 설명은 `docs/ERD.md`, 대화·기억 흐름은 `docs/ARCHITECTURE-capi.md`가 기준이다.

## 현재 상태

- dev와 prod 모두 ORM 모델 테이블의 컬럼·nullable·RLS·민감 테이블 권한 검사를 통과한다.
- 장기 기억 사용자는 `mode='v2'`, `bootstrap_status='ready'`를 사용한다.
- 예전 정규화 기억 테이블과 `vecs.memories`는 제거됐고 현재 기억 컬렉션은
  `vecs.moly_memories_v2` 하나다.
- 운영 전환용 일회성 절차는 끝났다. 과거 SQL 파일은 현재 실행 지침이 아니라 적용 이력이다.

`schema_migrations` 행 수는 DB를 만든 방식에 따라 다를 수 있다. dev는 최신 baseline을 먼저 적용해
이전 파일이 기록표에 모두 없을 수 있고, prod는 실제 순차 적용과 일회성 보정 SQL까지 기록한다.
행 수 자체가 아니라 파일 checksum과 `db/verify.py` 결과, 필요한 테이블·제약의 존재를 확인한다.

## 새 마이그레이션 작성

1. `YYYYMMDD_<설명>.sql` 파일을 새로 만든다.
2. 재실행 가능하면 `IF EXISTS`·`IF NOT EXISTS`와 멱등 갱신을 사용한다.
3. 큰 데이터 변경은 무제한 한 트랜잭션으로 처리하지 않는다. 짧은 묶음, 잠금 범위, 제한 시간,
   재시작 지점을 설계한다.
4. 파괴적 변경은 먼저 읽기 경로를 새 구조로 옮기고 실제 사용이 없는지 확인한 뒤 별도 파일로 한다.
5. 모델·`db/schema.sql`·`docs/ERD.md` 중 영향을 받는 기준 파일을 같은 변경에서 갱신한다.
6. dev에서 dry-run, 실제 적용, 모델 검증 순서로 확인한다.

```bash
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일명>.sql
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일명>.sql --commit
PYTHONPATH=. uv run python db/verify.py
```

`db/apply.py`는 기본적으로 `.env`의 dev DB를 대상으로 실행하고 마지막에 rollback한다. `--commit`을
붙여야만 반영한다. prod 반영은 사용자가 명시적으로 승인한 작업에서만 다음처럼 실행한다.

```bash
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일명>.sql --env prod
PYTHONPATH=. uv run python db/apply.py db/migrations/<파일명>.sql --env prod --allow-prod --commit
PYTHONPATH=. uv run python db/verify.py --env prod
```

dry-run 성공만으로 운영 적용을 결정하지 않는다. 대상 DB, 예상 영향 행 수, 락, API와 워커의 혼합
버전 호환, 되돌리기 방법을 먼저 확인한다.

## 꾸미기 계약

레거시 상점 경로는 `theme|head|neck|body`, `/v2/shop`·`/v2/inventory` 경로는
`theme|hat|glasses|neck|body`를 반환한다. v2는 모자와 안경을 동시에 장착하고 rightside 레이어를
사용하는 현재 신계약이다. 이 구조를 만든 `20260713_appearance_v2_*`,
`20260719_hat_glasses_rightside.sql`, `20260720_products_v2_only.sql`은 이미 적용된 역사이며 다시
전환할 대상이 아니다.

새 꾸미기 에셋은 버전 경로와 `asset_version`을 함께 올리고 배포 전에 검증한다.

```bash
uv run python scripts/verify_appearance_assets.py /path/to/appearance.json
```

### 모자 7종 (`20260826_hats_seven.sql`)

버킷햇·캡모자·빵모자·두건·수건·수박 모자·토끼 모자는 각 1,000건초인 v2 전용 상품이다.
운영 DB에는 먼저 반영됐으며 파일 checksum은 운영 원장과 일치한다. 새 DB는
`db/seed_and_triggers.sql`, 기존 DB는 해당 마이그레이션으로 같은 카탈로그를 재현한다.

7종은 `rightside` 레이어만 있으므로 반드시 `is_v2_only=true`를 유지한다. 기존 DB에 적용할 때는
사전에 에셋 업로드를 확인하고, 적용 후 `/v2/shop/products`에는 보이고 레거시 `/shop/products`에는
노출되지 않는지 검증한다.

## 기억 계약

현재 기억 구조의 핵심 마이그레이션은 다음과 같다.

| 파일 | 현재 역할 |
|---|---|
| `20260805_mem0_v2_collection.sql` | `vecs.moly_memories_v2`와 벡터 인덱스 생성 |
| `20260805_memory_v2_tables.sql` | 파이프라인 상태, 후보·근거·수명 장부, 대화 약속·관계 상태 생성 |
| `20260805_relationship_render.sql` | 관계 이벤트와 언어별 렌더 projection |
| `20260806_drop_legacy_memory.sql` | 예전 정규화 기억 테이블 제거 |
| `20260806_drop_legacy_tombstones.sql` | 이관용 tombstone 제거 |
| `20260808_memory_category.sql` | 현재 기억 분류 보강 |
| `20260905_drop_empty_legacy_vecs_memories.sql` | 남아 있던 빈 `vecs.memories` 컬렉션을 검증 후 제거 |

런타임은 벡터 컬렉션이나 테이블을 자동 생성하지 않는다. 새 환경을 만들 때는 이 파일들이
`schema_migrations`에 기록됐다는 사실만 보지 말고 `db/verify.py`와 기억 컬렉션 존재를 함께 확인한다.

## 오늘의 운세 계약

| 파일 | 현재 역할 |
|---|---|
| `20260827_daily_fortune.sql` | dev에 최초 적용한 운세 프로필·당일 snapshot·광고 세션 이력 |
| `20260827_daily_fortune_v2.sql` | 입력을 생년월일·성별로 축소하고 결과 schema·당일 권한 제약을 확정 |
| `20260827_fortune_chat_context.sql` | 운세 대화 구간을 표시하는 `messages.kind` 값 추가 |

세 파일은 dev의 `schema_migrations`에 기록돼 있으므로 수정하지 않는다. 운영에는 적용하지 않았으며, 운영
전환은 `docs/DAILY-FORTUNE.md`의 승인·배포 조건을 통과한 뒤 별도로 진행한다.
