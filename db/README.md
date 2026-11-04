# DB 구조와 변경

`schema.sql`이 애플리케이션 DB 구조의 원본이다. 컬럼·기본값·CHECK·FK·인덱스·함수·트리거·RLS와
객체 권한을 함께 관리한다. `app/models/`는 애플리케이션 매핑이며 DB 전체 정의를 대체하지 않는다.

`schema_contract.json`은 빈 PostgreSQL 17에 `schema.sql`을 실행해 생성한 검증 산출물이다.
직접 편집하지 않는다. 스키마 SHA-256이 달라지면 검증이 실패하며, 구조를 다시 생성해 갱신한다.
과거 날짜별 마이그레이션·일회성 전환 SQL은 Git 이력에서 조회한다. 새 파일을 누적하지 않는다.

## 기존 환경 검증

```bash
uv run python -m db.verify --env dev
uv run python -m db.verify --env prod
```

읽기 전용 트랜잭션으로 구조만 조회한다. 컬럼 타입·NULL 허용·기본값, 검증된 제약, 유효한 인덱스,
함수 본문·소유자, 트리거, RLS, 객체 권한과 시퀀스 정의를 비교한다. 사용자 데이터나 연결 비밀값을
출력하지 않는다. 기본 검증은 혼합 버전 배포를 위해 새 테이블 등의 추가를 허용하지만 기존 객체의
제약·트리거·규칙·정책 추가와 권한 확대, 기본값 없는 필수 컬럼·UNIQUE 인덱스 추가는 거부한다.
FK 등 제약을 집행하는 내부 트리거도 참조하는 테이블·참조받는 테이블 양쪽의 활성 상태를 검사한다.
제약 정의와 validated 표시가 같아도 내부 트리거가 비활성화되어 있으면 실패한다.
이 검증이 모든 롤링 호환성을 증명하는 것은 아니므로 변경 SQL의 사용 경로도 검토한다.
`--strict`는 모든 추가 객체도 차이로 보고한다.

Supabase가 관리하는 Auth 테이블·역할·확장 내부 객체는 이 저장소의 소유 범위가 아니다.
애플리케이션이 Auth에 설치한 `on_auth_user_created` 트리거는 검증에 포함한다.

## 새 환경

Supabase의 `auth.users`, `anon`, `authenticated`, `service_role`과 PostgreSQL 17이 필요하다.
`vector`와 `pg_trgm` 확장은 public 스키마에 설치한다. 기존 애플리케이션 테이블이 하나라도 있으면
baseline 실행을 거부한다. **`schema.sql`은 기존 DB를 갱신하는 스크립트가 아니다.**

1. 빈 환경에 `schema.sql`을 적용한다.
2. 신규 환경의 상품·유효기간별 AI 단가를 `seed.sql`로 초기화한다. 기존 카탈로그에는 재실행하지 않는다.
3. 환경별 `app_config`, Storage와 인증·배포 설정을 운영 계약에 따라 준비한다.
4. 구조 검증과 가입·지급·탈퇴 동작을 확인한다.

캐피 자기일기는 `scripts/seed_capi_diaries.py`가 날짜별/주간 CSV를 구분해 관리한다.
주간 원고는 `week_start_date,sequence_no`로 식별하며 기존 날짜별 데이터는 보존한다.
날짜 없는 임시 일기 풀을 새 환경에 다시 생성하지 않는다.

현재 주간 일기 릴리스의 기존 DB 차이 SQL은 `changes/weekly_operator_diaries.sql`에 둔다.
자동 적용하지 않으며 운영 머지 전에 `docs/OPERATIONS.md`의 주간 일기 전환 절차를 따른다.
이는 이번 릴리스의 검토·인계 산출물이며 과거 마이그레이션 원장을 다시 만드는 것이 아니다.

탐색 탭 운영 승격 SQL은 `changes/explore_prod.sql` 한 파일이다. 생각 연습·제출/보상·아티클
진행·원장 type 합집합·아티클 테이블과 원고·오늘의 한마디 365개를 한 트랜잭션으로 적용한다. 시작 날짜·충돌 가드를 포함하므로 운영 문서의 승인된 KST 적용일과 SHA를 먼저 확인한다. 기존 dev용 분리 SQL을
운영에 중복 실행하지 않는다. 대상·SHA·적용·검증·복구 순서는
[운영 문서](../docs/OPERATIONS.md#둘러보기-아티클-db-전환)를 따른다. 구독 게이트 자체는 DB 변경이 없다.

일기 검색 벡터 공유의 기존 환경 차이 SQL은 `changes/diary_shared_embeddings.sql`이다.
공용 테이블과 nullable 참조만 추가하며 데이터 전환과 설정 변경은 별도다. 승인된 동일 원고의
실제 같은 벡터만 연결/제거하고, 개인 일기·다른 벡터 변형은 보존한다. 계획 hash에 묶인 전환·
역복사 도구는 `scripts/diary_shared_embeddings.py`, 순서와 롤백 경계는
[운영 문서](../docs/OPERATIONS.md#공용-일기-검색-벡터-공유)를 따른다.

## 구조 변경 절차

1. 관련 커밋과 서비스 사용 경로를 확인한 뒤 `schema.sql`을 수정한다. NULL·기본값·권한 변경도
   정책 변경이므로 단순 정리로 처리하지 않는다.
2. 허용된 검증 환경에서 baseline과 검증 계약을 재생성하고 테스트한다. 기존 개발 DB로만
   검증하라는 작업에서는 로컬 DB를 새로 띄우지 않는다. 아래 제한된 개발 메타데이터 갱신 절차를 쓴다.
3. 기존 DB에 필요한 차이만 담은 SQL을 리뷰 산출물로 준비한다. 대상·예상 행 수·잠금·혼합 버전
   호환·되돌리기 방법을 함께 검토한다. SQL 파일을 마이그레이션 디렉터리에 누적하지 않는다.
4. 검토한 SQL을 허용된 환경에 적용하고 검증한다. 개발 작업 완료가 운영 적용 승인을 뜻하지 않는다.
   운영 반영은 별도 작업이며 자동 배포는 DB를 변경하지 않는다.

```bash
uv run python -m db.apply /path/to/reviewed.sql --env dev
uv run python -m db.apply /path/to/reviewed.sql --env dev --commit --expected-sha256 <검토한-SHA256>
uv run python -m db.verify --env dev
```

`apply`는 기본 실행 후 rollback이다. SQL 내부의 COMMIT/END도 외부 트랜잭션을 종료할 수 없게
실행한다. 시퀀스 증가와 외부 호출처럼 PostgreSQL rollback 대상이 아닌 효과는 복구되지 않으므로
검토 없이 live 데이터 쓰기를 시험하는 용도로 쓰지 않는다. 쓰기는 dev 프로젝트 ref를 확인하며, prod 반영에는
`--env prod --commit --allow-prod`가 모두 필요하다. 운영 반영은 검토·승인한 SQL만 실행한다.
대상 보호 검사는 URL의 사용자 이름에서 프로젝트 ref를 읽는다. 연결 대상을 덮어쓸 수 있는
query parameter나 fragment가 있는 주소는 dev 쓰기 대상으로 승인하지 않는다.
`--expected-sha256`은 검토 이후 파일이 바뀌었는지 확인한다. 기존 `schema_migrations` 기록은
보존하지만 새 변경을 이 원장에 누적하거나 배포 성공 조건으로 사용하지 않는다.

기본 트랜잭션 제한은 lock 2초, statement 60초, idle 60초다. `--statement-timeout-ms`는
양의 정수이며 기본값은 60000이다. 제한은 검토 SQL을 감싼 바깥 DO가 시작되기 전에 설정한다.
SQL 파일 안에서 SET LOCAL만 해 이미 시작한 DO의 전체 제한을 줄였다고 가정하지 않는다.
lock을 얻지 못하면 제한을 늘리지
않고 rollback 후 한가한 시점에 재시도한다. `CREATE/DROP INDEX CONCURRENTLY`와 `VACUUM`은
트랜잭션 실행기에 넣지 않는다. 별도 검토한 절차로 실행하고 `indisvalid`·`indisready`와 구조
계약을 사후 확인한다. `IF NOT EXISTS`만으로 깨진 인덱스가 정상이라는 결론을 내리지 않는다.

### 제한된 개발 메타데이터 갱신

`--refresh-from-dev`는 이번 `db/changes/crisis_guardrails.sql`의 추가 컬럼 7개·CHECK 3개만
허용한다. 변경 전 `schema.sql`과 기존 계약의 해시, 검토한 SQL 해시, 허용 개발 프로젝트 ref를
모두 확인한다. 개발 DB를 읽기 전용으로 비교하고 관계없는 구조 차이가 있으면 파일을 갱신하지 않는다.
SQL을 먼저 검토·적용한 뒤 이 모드를 실행하고 `db.verify --env dev`로 다시 확인한다.

```bash
uv run python -m db.schema_contract --refresh-from-dev --env dev \
  --expected-project-ref wywzjslvxwttxkecbyis \
  --reviewed-sql db/changes/crisis_guardrails.sql \
  --expected-sha256 <검토한-SHA256> --baseline-schema <변경-전-schema.sql>
uv run python -m db.verify --env dev
```

다른 릴리스의 차이를 자동으로 허용하지 않는다. 새 구조 변경에서는 허용 목록과 검증을 별도로 검토한다.

## 로컬 재생성과 검증

`db/testing/supabase.sql`은 폐기 가능한 로컬 테스트 DB용 최소 Auth 의존성이다. Supabase에
실행하지 않는다. 연결 대상은 loopback의 `moly_schema_*` 이름이어야 생성 모드가 동작한다.
아래 명령의 연결 환경변수는 로컬 테스트 DB만 가리켜야 한다.

```bash
psql -X -v ON_ERROR_STOP=1 -f db/testing/supabase.sql
uv run python -m db.schema_contract --generate
psql -X -v ON_ERROR_STOP=1 -f db/seed.sql
uv run python -m db.schema_contract --strict
uv run pytest -q tests/integration/test_schema_bootstrap_local.py
```

`psql`은 `PGHOST/PGPORT/PGUSER/PGDATABASE`, 검증 도구는 `SUPABASE_DB_CONNECTION_STRING`,
테스트는 `MOLY_SCHEMA_TEST_DSN`을 사용한다. 모두 같은 폐기용 DB를 지정한다.
`--check-generated`는 또 다른 빈 DB에 스키마를 생성해 커밋된 계약과 일치하는지 검증한다.

## 구독 변경의 main 계약 보존

개발 DB와 main에는 서로 다른 기능이 있으므로 개발 catalog 전체를 운영 계약에 복사하지 않는다.
이번 구독 변경은 검토한 main `schema.sql`과 기존 계약을 보존하고, 개발 DB에서 확인한 nullable
JSONB 두 컬럼만 합친다. 로컬 DB를 새로 만들지 않는 제한된 생성 경로다.

```bash
uv run python -m db.schema_contract --refresh-subscription-from-dev --env dev \
  --expected-project-ref <확인한-개발-project-ref> \
  --reviewed-sql db/changes/subscription_refund_access.sql \
  --expected-sha256 <검토한-SQL-SHA256> --baseline-schema <변경-전-main-schema.sql>
```

생성기는 baseline checksum, SQL 경로·checksum, 정확히 두 컬럼만 바뀐 소스, 실제 개발 컬럼의
타입·NULL·기본값을 확인한다. main의 나머지 테이블·제약·정책·권한은 유지한다. CI는 전체 소스를
폐기용 CI DB에 재생성해 최종 계약을 독립적으로 확인한다. 운영 적용 순서와 보정·복구는
[운영 문서](../docs/OPERATIONS.md#구독-환불-권한-전환)를 따른다.

## 가드레일·맥락의 무중단 전체 전환

이번 후보는 운영 main `64c133d97c0c61022600c3b9074aa72fa92a113b`의 스키마/계약을
보존하고 검토한 guard/context 객체만 더한다. 개발 DB 전체 catalog를 복사하지 않는다.
생성 후보와 실제 DB 검증을 구분하며, 다음 순수 derivation은 DB에 연결하지 않는다.

```bash
uv run python -m db.schema_contract --derive-guard-context-candidate \
  --baseline-schema "$MAIN64_SCHEMA" --baseline-contract "$MAIN64_CONTRACT" \
  --crisis-sha256 c587c3524e186a8a031c69d5e6769851819ed2a13c4990958f5ebe5466c3da13 \
  --pressure-sha256 e4c58f65889444430868e676e6d37702733bd30ff0c407c95686937e58145bb6 \
  --online-sha256 a7e50156064ceefd40cb9ed491d34c860f61c6a119f53e76c005b44d88bb6cf2
```

입력은 고정된 main64 원본 파일이어야 한다. 도구는 baseline·SQL checksum과 정확한 추가
객체만 있는 source를 확인하고 계약 후보를 생성한다. main64의 구독·루틴·일상 연습·권한 등
다른 객체는 보존한다. 생성 성공을 운영 DB 일치나 적용 완료로 해석하지 않는다.

- `changes/guard_context_release.sql`은 최종 DDL의 **검토 참조**다. 이를 운영에 한 TX로
  실행하면 metadata의 ACCESS EXCLUSIVE 잠금을 기존 데이터 검증 scan 끝까지 보유할 수 있다.
  실제 최초 도입은 `guard_context_release_metadata.sql` → 네
  `guard_context_release_validate_*.sql` 각각 → `guard_context_release_swap.sql` 순서의
  별도 TX를 사용한다. metadata/swap은 `--statement-timeout-ms 750`과 NOWAIT 잠금,
  각 검증은 60000을 사용한다. 검증의 SHARE UPDATE EXCLUSIVE는 일반 DML과 호환된다.
  최종 구조는 기존 보존 기능 컬럼 8개와 메시지 종류를 확장하고, 준비 테이블 두 개와 checkpoint
  준비 ID, 전역 active 포인터의 JSON null 초기 행을 추가한다. 기존 포인터는 덮어쓰지 않는다.
  기존 published window unique만 NULL 준비 영역으로 한정하고, 준비 ID별 unique를 추가한다.
  같은 원문 경계의 기존 공개행을 보존하면서 한 준비 ID 안의 중복 게시는 계속 막는다.
- `changes/guard_context_online.sql`은 위 metadata 실행 내용에 포함되므로 중복 실행하지 않는다.
  이 구조를 이미 적용한 개발 DB의 proof 필드 보완에만 별도
  `changes/guard_context_online_proof.sql`을 사용한다. addon과 최종 CREATE의 catalog가
  같아야 하며, 실제 적용은 root가 허용한 개발 DB에서 별도로 검증한다. 게시 index의 좁은
  보완에는 `changes/guard_context_online_indexes.sql`을 사용한다. 최초 도입 metadata에는
  두 addon의 최종 구조가 모두 들어 있으므로 운영에서 addon을 중복 실행하지 않는다.
- 공개 대화·일반 worker·결제 webhook은 계속 운영한다. additive DDL을 짧은 잠금 제한으로
  분리 적용하고 모든 CHECK 검증·이름 교체 후 최종 계약을 확인한다. 그다음 호환 API/worker를
  배포하고 기존 두 기능 OFF/active 포인터 null 상태에서 준비한다.
  잠금 실패는 rollback 후 재시도하며 서비스 중단이나 강제 커서 변경으로 우회하지 않는다.
- 준비 CLI의 `plan`/`start`는 코드 SHA와 전체 원문 경계를 고정한다. 지정된 준비 ID의 전용
  worker와 `catch-up`/`audit`로 계약 처리 증명·요약 근거·현재 tail을 확인한다. 준비 계약은
  `retention_prepared_contracts`, 준비 요약은 준비 ID의 ready 행에 저장한다. 공개 계약과
  공개 요약을 준비 상태로 바꾸지 않는다. 처리 목표·원장·준비 상태를 사용자별 기능 gate로 쓰지 않는다.
- `activate`만 검증 자료 게시와 registry active, 전역 포인터·설정을 한 트랜잭션에서 바꾼다.
  NOWAIT 경합이나 1초 제한 초과 시 전체 rollback하고 기존 공개 경로를 유지한다. 준비 목록별
  공개나 A/B 전환은 없다. 기존 요청은 캡처한 이전 자료로 끝내고 이후 새 요청은 새 포인터를 쓴다.
- 준비를 취소할 때는 해당 ID의 `abort`와 전용 worker 드레인을 사용한다. 활성화 뒤에는
  `changes/guard_context_release_disable.sql`로 두 쓰기 기능을 끄되 active 포인터와
  새 형식의 reader는 유지한다. building worker는 전역 disable만으로 중지되지 않는다.
  구버전 main64 이미지로 맹목 복귀하거나 새 컬럼·CHECK·자료를 제거하지 않는다.

일부 DDL 단계가 실패하면 이미 commit된 단계는 보존한다. 실제 catalog의 컬럼/제약 정의와
validated 상태를 확인한 뒤 남은 파일만 이어 간다. metadata나 swap을 재실행하면서 존재 여부를
임의로 무시하지 않는다. metadata 전이면 전체 재시도, metadata 후면 미완료 검증부터, swap
완료면 최종 계약 검증으로 진행한다. 새 구조를 지워 처음으로 되돌리지 않는다.

이전 점검 방식의 직접 prepare/activate SQL은 제거했다. 전역 flag나 active 포인터를 수동으로
켜서 source/audit 검사를 우회하지 않는다. 실제 동시성·전체 규모 승격 시간·운영 API 확인은
검사 산출물로 증명해야 하며 구조 계약 생성만으로 완료됐다고 하지 않는다. 명령·대상 보호·
적용·검증·복구 순서는 [운영 문서](../docs/OPERATIONS.md#가드레일대화-맥락의-무중단-전체-전환-2026-11-04)를 따른다.

## 반드시 보존하는 정책

- `products.price_hay=NULL`은 비매품이다. 0은 건초 원장 제약과 충돌하므로 허용하지 않는다
  (`d18e82b`). 건초 상품의 `public_id`·`asset_version`은 현재 prod 제약을 따른다.
- `name_i18n`의 SQL NULL은 번역 fallback 대상이다. JSON `null`과 다르며 기존 유저 루틴을
  이름만 보고 번역 데이터로 덮어쓰지 않는다 (`5856382`, `c272dfe`).
- 결제금액·가격표의 NULL은 미확인 또는 비해당 값이다. 0원으로 대체하지 않는다
  (`30579b0`, `1f9bc80`).
- 가입 기본 언어는 `en`, 지원 지역 태그는 `ko/en/ja`로 정규화한다. 온보딩이 보낸 한국어는
  `ko`로 저장한다 (`eedddd4`). 가입 선물은 기본 집·선글라스이며 운동 테마는 비활성이다
  (`5c6fe14`).
- 삭제 장벽은 프로필 삭제 후에도 남는다. FK CASCADE를 추가하지 않는다. 기억 벡터는
  `delete_user_memories` RPC로 정리하며 재실행 시 0건 반환이 정상이다 (`d9637fe`).
- 비용·재처리·계약의 nullable 참조, 삭제 장벽의 active 상태에서 비어 있는 `operation_id`와
  미완료 작업의 커서는 빈 값을 채워 넣는 대상으로 보지 않는다.
