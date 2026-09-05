# 운영 DDL 런북 — psql 직결 실행 절차

- **작성**: 2026-08-28 (DB 최적화 로드맵 Phase 1 산출물 — `docs/DB_OPTIMIZATION_ROADMAP.md` v4)
- **왜 이 문서인가**: `db/apply.py`는 파일 전체를 하나의 트랜잭션으로 감싼다(apply.py:36-39). `CREATE/DROP INDEX CONCURRENTLY`·`VACUUM FULL`·`pg_repack`은 트랜잭션 안에서 실행 불가 — **반드시 psql 직결로 실행**하고, 실행분을 원장(`schema_migrations`)에 수동 기록한다.
- **대원칙**: 서비스 시간대에는 CONCURRENTLY 계열만 허용. 비CONCURRENTLY 인덱스 생성/드랍·VACUUM FULL은 새벽 점검 창(02:00~03:30 KST) + 컨슈머 정지 + 공지에서만(로드맵 금지 목록).

---

## 0. 연결 (실행 전 매번)

1. DSN 확보 — 운영 DSN은 SSM에만 있다(로컬 `.env`류 사본 금지):
   ```bash
   aws ssm get-parameter --name /moly/prod/supabase-db-connection-string \
     --with-decryption --region ap-northeast-2 --query Parameter.Value --output text
   ```
2. **포트 판별 (중요)**:
   - `…pooler.supabase.com:5432` = Supavisor **session 모드** → CONCURRENTLY 실행 **가능**.
   - `…pooler.supabase.com:6543` = **transaction 모드** → CONCURRENTLY **불가**. 이 경우 대시보드의 Direct connection(또는 session 5432) DSN을 별도 확보해 실행한다.
   - 앱이 어느 포트를 쓰는지는 로드맵 Phase 3 #8(#LISTEN/NOTIFY 배제)·#12(풀 상한)의 분기점이므로 위 명령 결과를 로드맵 실행 기록에 남길 것. *(2026-08-28 현재 미확정 — SSM 조회 권한이 사람에게만 있음)*
3. 공통 세션 설정 — 접속 직후 무조건:
   ```sql
   SET lock_timeout = '2s';            -- AE 락 대기로 뒤 쿼리 정체 방지(§9.6). 실패 시 잠시 후 재시도
   SET statement_timeout = 0;          -- CIC/repack은 오래 걸리는 게 정상
   SET application_name = 'ddl-runbook';
   ```
   `lock_timeout` 초과로 실패하면: 긴 트랜잭션 확인(`pg_stat_activity`) 후 재시도. **절대 lock_timeout을 늘려서 밀어붙이지 않는다.**

## 1. CREATE INDEX CONCURRENTLY

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS <이름> ON <테이블>(...) [WHERE ...];
```
- 실패/세션 단절 시 **INVALID 인덱스가 남는다** — §5의 탐지 쿼리로 확인 후 `DROP INDEX CONCURRENTLY <이름>;` → 재생성.
- 완료 후: `\d <테이블>`로 valid 확인 + 대상 쿼리 EXPLAIN 1회.

## 2. DROP INDEX CONCURRENTLY

```sql
DROP INDEX CONCURRENTLY <스키마>.<이름>;
```
- 실패 시 인덱스가 INVALID로 남을 수 있다(쿼리는 이미 안 쓰지만 쓰기 오버헤드는 남는 단계 있음) — **같은 DROP을 재실행**하면 완결.
- 롤백(재생성)이 필요한 대상은 로드맵 해당 항목의 롤백 절차를 따른다(예: HNSW 재생성은 `SET maintenance_work_mem='512MB'` 후 CIC — 14K행 기준 분 단위·무차단).

## 2a. 운세 `messages.kind` CHECK 확장

`20260827_fortune_chat_context.sql`은 기존 CHECK를 한 트랜잭션에서 삭제·재생성하며 검증하는 개발 이력이다.
운영 live `messages`에는 실행하지 않는다. 다음 세 파일을 `db/apply.py`로 각각 별도 실행한다.

1. `20260905_fortune_chat_kind_constraint_prepare.sql`: 새 CHECK를 `NOT VALID`로 추가.
2. `20260905_fortune_chat_kind_constraint_validate.sql`: 기존 행을 별도 트랜잭션에서 검증.
3. `20260905_fortune_chat_kind_constraint_swap.sql`: 검증된 CHECK를 기존 이름으로 교체.

`prepare`와 `swap`은 짧게 `ACCESS EXCLUSIVE`를 요구하지만 자체 `lock_timeout='2s'`가 있다. 실패하면
기존 제약과 서비스는 그대로이므로 timeout을 늘리지 말고 재시도한다. 세 파일 적용 전에는 운세 대화 코드를
켜지 않는다. 완료 뒤 아래 결과가 validated인 확장 제약인지 확인한다.

```sql
SELECT conname, convalidated, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid='public.messages'::regclass
  AND conname LIKE 'messages_kind_check%';
```

## 3. pg_repack (`vecs.moly_memories_v2` 등)

사전:
- `CREATE EXTENSION IF NOT EXISTS pg_repack;` — 서버 가용 버전 **1.5.2**(2026-08-28 확인, 미설치 상태였음).
- 클라이언트 CLI 버전이 서버 확장과 **정확히 일치**해야 한다(1.5.2).
- 대상 테이블 PK 필수(moly_memories_v2_pkey 확인 완료). 디스크 여유 ≈ 대상 테이블 2배.

실행:
```bash
pg_repack -h <direct-host> -p 5432 -U postgres -d postgres \
  -t vecs.moly_memories_v2 -k --no-kill-backend
```
- **`--no-kill-backend` 필수** — 기본 동작은 60초 대기 후 경합 백엔드를 cancel→terminate(라이브 쿼리 킬)한다(§9.6).
- `-k`(--no-superuser-check): Supabase는 superuser 불가.

실패 시 정리:
- 잔존물: 대상 테이블의 `z_repack` 트리거 + `repack.log_*` 테이블(방치 시 모든 DML이 로그에 계속 적재 = 재팽창).
- 정리: `DROP EXTENSION pg_repack CASCADE;` → 필요 시 재설치 후 재시도. 원본 테이블은 무손상(설계상 마지막 swap 전까지 원본 불변).

## 4. VACUUM FULL (fallback — 점검 창 전용)

- 새벽 02:00~03:30 KST + 컨슈머 정지(`docker compose stop consumer` 상당) + 공지.
- 실측 기준 소요 1~3분급(485MB 시절 산정 90분은 과대 — §9.6). 창은 여유 있게 유지.
- ACCESS EXCLUSIVE — 창 밖 실행 절대 금지(금지 목록).

## 5. INVALID 인덱스 탐지 (스코프 한정 필수)

```sql
SELECT n.nspname, c.relname, i.indisvalid
FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE NOT i.indisvalid AND n.nspname IN ('public','vecs');
```
- **`storage` 스키마에 Supabase 내부 INVALID 인덱스 1건이 원래 존재**(`idx_objects_bucket_id_name_lower`) — 정리 대상 아님. 스코프를 public/vecs로 한정하는 이유.

## 6. 원장(schema_migrations) 기록

### 6a. psql 직결로 실행한 DDL
실행 SQL을 `db/migrations/<YYYYMMDD>_<설명>.sql`로 저장(재현 가능하게 — CONCURRENTLY 포함 그대로, 헤더에 "psql 직결 전용, apply.py 금지" 명기) 후:
```sql
INSERT INTO public.schema_migrations(migration_name, checksum_sha256)
VALUES ('<파일명>', '<shasum -a 256 결과>');
```

### 6b. 이미 원장에 있는 파일을 편집한 경우 (Phase 0에서 기실행)
```sql
UPDATE public.schema_migrations SET checksum_sha256='<새 해시>'
 WHERE migration_name='<파일명>' AND checksum_sha256='<구 해시>';  -- CAS: 구 해시 조건 필수
```
- prod와 dev **양쪽 모두** 갱신해야 한다. 갱신 문은 로드맵 "실행 기록"에 남긴다.
- ⚠️ `promote_memory_v2.sql` 등 `db/` 직하 운영 스크립트는 apply.py의 checksum 가드를 우회한다 — 반드시 이 런북 경로(기록 포함)로만 실행.

운세 활성화 전에는 `db/verify.py --env prod`의 출력뿐 아니라 **종료 코드 0**을 확인한다. infra의
`scripts/verify_fortune_schema.py`도 컨테이너 교체 전에 같은 DB를 읽기 전용으로 검사한다. 세 운세
테이블·RLS·권한, 건초 광고 세션의 만료 컬럼·CHECK·전체 만료 인덱스, 운세 대화 CHECK·부분 인덱스와 필수
migration 이름·checksum 중 하나라도 다르면 배포를 중단한다.

## 7. 사전 확인 상태 (2026-08-28)

| 항목 | 상태 |
|---|---|
| `schema_migrations` prod 존재 | ✅ 확인 (checksum 검증 로직 apply.py:42-49 실재) |
| pg_repack 가용성 | ✅ 1.5.2 가용, 미설치 (사용 직전 CREATE EXTENSION) |
| prod DSN 포트(5432 세션/6543 트랜잭션) | ⛔ 미확정 — §0-1 명령으로 확인 후 기록 |
| psql 직결 인증 | ⛔ 로컬에 유효한 운영 DSN 없음 (.env의 사본은 비밀번호 만료) — SSM에서 확보 |

## 8. #18 — schema.sql 재생성 (직결 DSN 확보 후)

```bash
pg_dump "<direct-dsn>" --schema-only --no-owner --no-privileges \
  -N storage -N graphql* -N realtime -N supabase_* -N extensions -N pgbouncer -N vault -N auth \
  > db/schema_dump_$(date +%Y%m%d).sql
```
- 현 `db/schema.sql`은 테이블 16개 누락(마이그레이션에만 존재) — 덤프 기반으로 재구성하고, 수기 주석(불변식 설명)은 병합 보존한다.
- cutover 잔재 정리(§6.4~6.5)를 같은 PR에서.
- 완료 기준: 신규 DB에 `schema.sql` 단독 적용 = prod와 `pg_dump --schema-only` диф 0 (RLS·트리거 포함).
